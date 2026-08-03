"""The Maker — the agent that actually does the work.

Every other agent produces a judgement. This one produces a thing: the draft the
find promised, written out in full, stored where the owner can open it.

Its scope is deliberately one artifact type. The value is in the loop being
complete — observed, reasoned, decided, *done*, measured — not in the variety of
what it can make.

Two rules it does not get to break:

* **It drafts; it never sends.** `PRODUCT.md` commits to the owner holding the
  leash. An artifact is a draft by construction, and nothing here has a delivery
  path to a customer.
* **A failed upload must not lose the draft.** The text is the deliverable and
  S3 is where a copy lives. Writing the row without a location is honest; losing
  a good draft because a bucket was briefly unavailable is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from brasstacks.artifacts import ArtifactStore, ArtifactStoreError
from brasstacks.providers import ProviderError, Reasoner
from brasstacks.repository import FindSummary, Repository, RepositoryError
from brasstacks.tasks import maker_artifact_idempotency_key

#: The one artifact type this ships, per the scope cut in CLAUDE.md.
ARTIFACT_KIND = "review_reply"

#: How much of the draft the UI shows inline. Long enough for the owner to
#: recognise the voice, short enough not to need a scrollbar in a list.
PREVIEW_CHARS = 240

#: How far back to look for an accepted find with nothing drafted for it.
FINDS_SCANNED = 20

MAKER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

MAKER_SYSTEM_PROMPT = """You are the Maker for Brass Tacks. The owner has \
accepted a recommendation and you now write the thing it promised, ready for \
her to review.

Hard requirements:
- You produce a DRAFT. Never send, publish, or post anything, and never write \
as though it has already been sent. She reads it and decides.
- Write in her voice, to her customers. Plain, warm, specific. No marketing \
language, no exclamation marks, no invented awards or claims.
- Never invent facts about the business — no dishes, prices, staff names, or \
events that were not given to you. If you need a detail you do not have, leave \
an obvious [square bracket] for her to fill in.
- If the move covers several items, write every one of them out in full. A \
draft she has to finish herself is not done-for-you.
- `title` names the deliverable in a few words, as it will appear in a list. \
`body` is the deliverable itself, in Markdown."""


@dataclass(frozen=True)
class MakerResult:
    run_id: str
    artifact_id: str | None = None
    location: str | None = None
    error: str | None = None
    reused: bool = False
    cancelled: bool = False

    @property
    def note(self) -> str:
        if self.cancelled:
            return "draft discarded because the owner reopened the recommendation"
        if self.error:
            return f"no artifact produced — {self.error}"
        if not self.artifact_id:
            return "nothing accepted and undrafted"
        if self.reused:
            return "existing idempotent draft reused; zero model tokens spent"
        if not self.location:
            return "draft saved in CockroachDB; the S3 copy could not be uploaded"
        return f"draft saved to {self.location}"


def next_undrafted_find(repo: Repository, business_id: str, *,
                        limit: int = FINDS_SCANNED) -> FindSummary | None:
    """The oldest accepted find that has nothing drafted for it yet.

    Only `accepted` — drafting for a proposal the owner has not decided on
    spends model budget on work she may never want, and quietly presumes an
    answer she has not given.
    """
    accepted = [
        f for f in repo.recent_finds(business_id, limit=limit)
        if f.status == "accepted"
    ]
    # Oldest first: the one she has been waiting on longest.
    for find in sorted(accepted, key=lambda f: f.created_at):
        # A previous decision cycle may have produced a draft that was later
        # superseded when the owner reopened the recommendation. Only a current
        # artifact satisfies the new decision cycle.
        current_artifacts = [
            artifact for artifact in repo.get_artifacts(find.find_id)
            if artifact.superseded_at is None
        ]
        if not current_artifacts:
            return find
    return None


def build_prompt(find: FindSummary, *, business: dict | None = None) -> str:
    name = (business or {}).get("name", "this business")
    return "\n".join([
        f"Business: {name}",
        "",
        f"The move she accepted: {find.title}",
        "",
        "What was promised, in the Analyst's words:",
        find.move,
        "",
        "Write that deliverable now, as a draft for her review.",
    ])


def _token_receipt(reasoner: Reasoner) -> dict[str, int | None]:
    """Return recorded model usage without coupling agents to one provider."""
    usage = getattr(reasoner, "last_usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }


def run_maker(
    *,
    repo: Repository,
    reasoner: Reasoner,
    store: ArtifactStore,
    business_id: str,
    find: FindSummary | None,
    model_id: str | None = None,
    task_id: str | None = None,
    artifact_idempotency_key: str | None = None,
    can_continue: Callable[[], bool] | None = None,
) -> MakerResult:
    """Draft the deliverable for one accepted find."""
    run_id = repo.start_run(business_id, agent="maker", model_id=model_id)

    # No accepted, undrafted find is the common case on most nights, not a
    # failure. Marking the run failed would make the audit trail cry wolf.
    if find is None:
        result = MakerResult(run_id=run_id)
        repo.finish_run(run_id, status="ok", note=result.note)
        return result

    idempotency_key = artifact_idempotency_key
    if idempotency_key is None and task_id is not None:
        idempotency_key = maker_artifact_idempotency_key(
            task_id, kind=ARTIFACT_KIND
        )
    if idempotency_key:
        existing = repo.get_artifact_by_idempotency_key(idempotency_key)
        if existing is not None:
            result = MakerResult(
                run_id=run_id, artifact_id=existing.artifact_id,
                location=(
                    f"s3://{existing.s3_bucket}/{existing.s3_key}"
                    if existing.s3_bucket and existing.s3_key else None
                ),
                reused=True,
            )
            repo.finish_run(
                run_id, status="ok", note=result.note,
                input_tokens=0, output_tokens=0,
            )
            return result

    business = repo.get_business(business_id) if hasattr(repo, "get_business") else None

    try:
        payload = reasoner.complete_json(
            system=MAKER_SYSTEM_PROMPT,
            user=build_prompt(find, business=business),
            schema=MAKER_SCHEMA,
        )
        title = str(payload["title"]).strip()
        body = str(payload["body"]).strip()
        if not title or not body:
            raise ProviderError("the model returned an empty draft")
    except (ProviderError, KeyError, TypeError) as e:
        error = f"{type(e).__name__}: {e}"
        result = MakerResult(run_id=run_id, error=error)
        repo.finish_run(run_id, status="failed", error=error, note=result.note,
                        **_token_receipt(reasoner))
        return result

    # The owner may reopen the recommendation while the model is generating.
    # The generated text is not an external side effect, so discard it before
    # S3 or CockroachDB writes instead of allowing a stale draft to appear in
    # the new decision cycle. This guard is deliberately checked *after* the
    # model call as well as by insert_artifact's transactional task/cycle check.
    if can_continue is not None and not can_continue():
        result = MakerResult(run_id=run_id, cancelled=True)
        repo.finish_run(
            run_id,
            status="ok",
            note=result.note,
            **_token_receipt(reasoner),
        )
        return result

    # Upload first so the row can record where it landed — but a failure here
    # is survivable, and the draft is kept either way.
    location = None
    try:
        object_prefix = f"tasks/{task_id}" if task_id else f"finds/{find.find_id}"
        where = store.put(key=f"{object_prefix}/{ARTIFACT_KIND}.md", body=body)
        location = where
    except ArtifactStoreError:
        pass

    try:
        artifact_id = repo.insert_artifact(
            find_id=find.find_id,
            kind=ARTIFACT_KIND,
            title=title,
            preview=_preview(body),
            s3_bucket=location.bucket if location else None,
            s3_key=location.key if location else None,
            run_id=run_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
            body=body,
        )
    except RepositoryError as e:
        error = f"{type(e).__name__}: {e}"
        result = MakerResult(run_id=run_id, error=error)
        repo.finish_run(run_id, status="failed", error=error, note=result.note,
                        **_token_receipt(reasoner))
        return result

    result = MakerResult(
        run_id=run_id,
        artifact_id=artifact_id,
        location=f"s3://{location.bucket}/{location.key}" if location else None,
    )
    repo.finish_run(run_id, status="ok", note=result.note,
                    **_token_receipt(reasoner))
    return result


def _preview(body: str) -> str:
    """The opening of the draft itself — never a summary of it.

    The owner approves what she can see, so a preview that paraphrased would be
    asking her to sign off on something she has not read.
    """
    flat = " ".join(body.split())
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[:PREVIEW_CHARS].rstrip() + "…"
