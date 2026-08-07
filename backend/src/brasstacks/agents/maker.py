"""The Maker — the agent that turns an approved move into owner-ready work.

Maker produces a versioned review package, not an unstructured wall of text.
The complete working draft remains durable, while structured fields tell the UI
and email layer exactly what the owner needs to know or decide next.

Maker never publishes or sends to customers. External actions are performed by
constrained tools after deterministic approval checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from brasstacks.agent_runs import closing_run
from brasstacks.artifact_usage import artifact_use_context
from brasstacks.artifacts import ArtifactStore, ArtifactStoreError
from brasstacks.providers import ProviderError, Reasoner
from brasstacks.repository import FindSummary, Repository, RepositoryError, StoredArtifact
from brasstacks.tasks import maker_artifact_idempotency_key

# Kept for backward compatibility with existing artifacts. The actual deliverable
# type is stored in artifact.metadata["artifact_type"].
ARTIFACT_KIND = "review_reply"
PREVIEW_CHARS = 240
FINDS_SCANNED = 20
MAX_OWNER_QUESTIONS = 3
MAX_SECTIONS = 5
MAX_SUMMARY_CHARS = 220
ACTION_MANIFEST_VERSION = 1

ARTIFACT_TYPES = (
    "customer_email",
    "google_business_post",
    "review_reply",
    "menu_update",
    "offer_package",
    "staff_checklist",
    "operating_plan",
    "general_draft",
)

MAKER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "summary": {"type": "string"},
        "review_state": {
            "type": "string",
            "enum": ["ready_for_review", "needs_owner_input"],
        },
        "owner_action": {"type": "string"},
        "owner_questions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "artifact_type": {
            "type": "string",
            "enum": list(ARTIFACT_TYPES),
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["title", "content"],
                "additionalProperties": False,
            },
        },
    },
    # title/body remain the compatibility floor for older providers and tests.
    "required": ["title", "body"],
    "additionalProperties": False,
}

MAKER_SYSTEM_PROMPT = """You are Maker inside Brass Tacks. The owner approved a
business recommendation. Turn it into the smallest professional review package
that moves the work forward.

You are writing for a busy local-business owner, not an internal analyst.

Hard requirements:
- Produce a DRAFT only. Never send or publish it, and never claim it was published, posted, sent to customers,
  or made live.
- Be precise, calm, and easy to scan. No marketing jargon, exclamation marks,
  database ids, retrieval scores, or internal agent language.
- Do not bury the next decision in a long document.
- If essential owner facts are missing, set review_state to needs_owner_input,
  ask at most three required questions, and stop. Each question must ask for one
  decision or value, name the expected answer format in parentheses, and be
  answerable in one line. Do not generate a usable-looking draft or placeholder
  sections until those answers arrive.
- For needs_owner_input, owner_action must say: "Answer the numbered questions in one reply."
  Never hide a required input in summary, body, or a section.
- If enough facts exist, set review_state to ready_for_review and create no more
  than five useful sections. Each section must have one clear purpose.
- summary: one complete sentence, usually under 180 characters.
- owner_action: one direct sentence describing exactly what the owner should do
  next.
- body: the complete working draft in readable Markdown. It may be detailed,
  but do not repeat the same instruction in multiple sections.
- artifact_type: choose the closest allowed type based on where the finished draft will actually be used.
- If the destination is unclear and it changes the content, ask one required destination question instead of guessing.
- Never invent products, prices, hours, staff names, claims, or channels. Ask for
  a missing decision rather than guessing.
- When revising an existing draft, preserve correct facts and change only what
  the owner requested. Return a complete replacement version, not a patch.
"""


@dataclass(frozen=True)
class MakerResult:
    run_id: str
    artifact_id: str | None = None
    location: str | None = None
    error: str | None = None
    reused: bool = False
    cancelled: bool = False
    summary: str | None = None
    review_state: str | None = None
    revision: int = 1

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
        suffix = f" · revision {self.revision}"
        if not self.location:
            return "draft saved in CockroachDB; the S3 copy could not be uploaded" + suffix
        return f"draft saved to {self.location}{suffix}"


def next_undrafted_find(repo: Repository, business_id: str, *,
                        limit: int = FINDS_SCANNED) -> FindSummary | None:
    accepted = [
        f for f in repo.recent_finds(business_id, limit=limit)
        if f.status == "accepted"
    ]
    for find in sorted(accepted, key=lambda f: f.created_at):
        current_artifacts = [
            artifact for artifact in repo.get_artifacts(find.find_id)
            if artifact.superseded_at is None
        ]
        if not current_artifacts:
            return find
    return None


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,;:—-") + "…"


def _owner_messages(repo: Repository, business_id: str, find_id: str) -> list[str]:
    try:
        rows = repo.recent_chat_messages(business_id, limit=8, find_id=find_id)
    except Exception:
        return []
    return [
        _compact(row.content, 360)
        for row in rows
        if getattr(row, "role", "") == "user" and str(row.content or "").strip()
    ][:4]


def build_prompt(
    find: FindSummary,
    *,
    business: dict | None = None,
    facts: Sequence[str] = (),
    rules: Sequence[Any] = (),
    owner_messages: Sequence[str] = (),
    revision_instruction: str | None = None,
    previous_artifact: StoredArtifact | None = None,
) -> str:
    business = business or {}
    lines = [
        "BUSINESS",
        f"Name: {business.get('name') or 'this business'}",
        f"Category: {business.get('category') or 'not recorded'}",
        f"Market: {business.get('city') or 'not recorded'}"
        + (f", {business.get('region')}" if business.get("region") else ""),
        "",
        "APPROVED RECOMMENDATION",
        f"Title: {find.title}",
        f"Action promised: {find.move}",
    ]
    if facts:
        lines.extend(["", "RELEVANT BUSINESS FACTS"])
        lines.extend(f"- {_compact(fact, 280)}" for fact in list(facts)[:8])
    if rules:
        lines.extend(["", "OWNER GUARDRAILS"])
        lines.extend(
            f"- {_compact(getattr(rule, 'rule', rule), 280)}"
            for rule in list(rules)[:6]
        )
    if owner_messages:
        lines.extend(["", "RECENT OWNER INPUT FOR THIS RECOMMENDATION"])
        lines.extend(f"- {_compact(message, 360)}" for message in owner_messages[:4])
    if revision_instruction and previous_artifact is not None:
        previous_questions = []
        if isinstance(previous_artifact.metadata, Mapping):
            raw_questions = previous_artifact.metadata.get("owner_questions")
            if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (str, bytes)):
                previous_questions = [
                    _compact(question, 220)
                    for question in raw_questions
                    if str(question or "").strip()
                ][:MAX_OWNER_QUESTIONS]
        lines.extend([
            "",
            "REVISION REQUEST",
            _compact(revision_instruction, 900),
        ])
        if previous_questions:
            lines.extend(["", "REQUIRED QUESTIONS FROM THE CURRENT REVISION"])
            lines.extend(
                f"{index}. {question}"
                for index, question in enumerate(previous_questions, start=1)
            )
        lines.extend([
            "",
            "CURRENT DRAFT TO REVISE",
            previous_artifact.body or previous_artifact.preview or "",
        ])
    lines.extend([
        "",
        "OUTPUT",
        "Return one structured owner-review package using the supplied JSON schema.",
    ])
    return "\n".join(lines)


def _token_receipt(reasoner: Reasoner) -> dict[str, int | None]:
    usage = getattr(reasoner, "last_usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }


def _questions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = _compact(item, 220)
        if text and text not in output:
            output.append(text)
        if len(output) >= MAX_OWNER_QUESTIONS:
            break
    return output


def _sections(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        title = _compact(item.get("title"), 80)
        content = str(item.get("content") or "").strip()
        purpose = _compact(item.get("purpose"), 180)
        if not title or not content:
            continue
        output.append({"title": title, "purpose": purpose, "content": content})
        if len(output) >= MAX_SECTIONS:
            break
    return output


def _action_manifest(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the only executable action Maker may propose today.

    The model chooses an artifact type; the server translates that bounded
    value into a deterministic manifest.  No account, token, destination or
    arbitrary URL is accepted from model output.
    """
    if (
        payload.get("artifact_type") == "google_business_post"
        and payload.get("review_state") == "ready_for_review"
    ):
        return {
            "version": ACTION_MANIFEST_VERSION,
            "action_type": "google_business.publish_post",
            "state": "ready_for_approval",
            "requires_owner_confirmation": True,
            "content_source": "artifact.body",
        }
    return None


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    title = _compact(payload.get("title"), 100)
    body = str(payload.get("body") or "").strip()
    if not title or not body:
        raise ProviderError("the model returned an empty draft")
    questions = _questions(payload.get("owner_questions"))
    review_state = str(payload.get("review_state") or "").strip()
    if review_state not in {"ready_for_review", "needs_owner_input"}:
        review_state = "needs_owner_input" if questions else "ready_for_review"
    if review_state == "needs_owner_input" and not questions:
        questions = [
            "What exact missing detail should Maker use? "
            "(reply with the value or decision)"
        ]
    summary = _compact(payload.get("summary") or _preview(body), MAX_SUMMARY_CHARS)
    if review_state == "needs_owner_input":
        count = len(questions)
        owner_action = (
            f"Answer the {count} required {'item' if count == 1 else 'items'} "
            "below in one reply. Maker will use your reply to create the next revision."
        )
    else:
        owner_action = _compact(
            payload.get("owner_action")
            or "Review the draft and tell Maker what to change, or copy it when ready.",
            240,
        )
    artifact_type = str(payload.get("artifact_type") or "general_draft").strip()
    if artifact_type not in ARTIFACT_TYPES:
        artifact_type = "general_draft"
    sections = _sections(payload.get("sections"))
    if review_state == "needs_owner_input":
        # A blocked artifact must look blocked. Keeping model-generated sections
        # here made a partial draft appear ready even while Maker was asking for
        # essential facts. The body remains durable for revision context, but the
        # owner-facing package exposes only the exact questions required next.
        sections = []
    elif not sections:
        sections = [{"title": "Complete draft", "purpose": "Ready for owner review", "content": body}]
    return {
        "title": title,
        "body": body,
        "summary": summary,
        "owner_action": owner_action,
        "review_state": review_state,
        "owner_questions": questions,
        "artifact_type": artifact_type,
        "sections": sections,
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
    revision: int = 1,
    revision_instruction: str | None = None,
    previous_artifact: StoredArtifact | None = None,
) -> MakerResult:
    """Create or revise one owner-review package for an approved find."""
    run_id = repo.start_run(business_id, agent="maker", model_id=model_id)
    with closing_run(repo, run_id):
        return _maker_draft(
            run_id=run_id,
            repo=repo,
            reasoner=reasoner,
            store=store,
            business_id=business_id,
            find=find,
            task_id=task_id,
            artifact_idempotency_key=artifact_idempotency_key,
            can_continue=can_continue,
            revision=revision,
            revision_instruction=revision_instruction,
            previous_artifact=previous_artifact,
        )


def _maker_draft(
    *,
    run_id: str,
    repo: Repository,
    reasoner: Reasoner,
    store: ArtifactStore,
    business_id: str,
    find: FindSummary | None,
    task_id: str | None,
    artifact_idempotency_key: str | None,
    can_continue: Callable[[], bool] | None,
    revision: int = 1,
    revision_instruction: str | None = None,
    previous_artifact: StoredArtifact | None = None,
) -> MakerResult:
    # No accepted, undrafted find is the common case on most nights, not a
    # failure. Marking the run failed would make the audit trail cry wolf.


    if find is None:
        result = MakerResult(run_id=run_id)
        repo.finish_run(run_id, status="ok", note=result.note)
        return result

    revision = max(1, int(revision))
    idempotency_key = artifact_idempotency_key
    if idempotency_key is None and task_id is not None:
        idempotency_key = maker_artifact_idempotency_key(
            task_id, kind=ARTIFACT_KIND, version=revision
        )
    if idempotency_key:
        existing = repo.get_artifact_by_idempotency_key(idempotency_key)
        if existing is not None:
            result = MakerResult(
                run_id=run_id,
                artifact_id=existing.artifact_id,
                location=(
                    f"s3://{existing.s3_bucket}/{existing.s3_key}"
                    if existing.s3_bucket and existing.s3_key else None
                ),
                reused=True,
                summary=existing.summary,
                review_state=existing.review_state,
                revision=existing.revision,
            )
            repo.finish_run(
                run_id, status="ok", note=result.note,
                input_tokens=0, output_tokens=0,
            )
            return result

    business = repo.get_business(business_id) if hasattr(repo, "get_business") else None
    try:
        facts = repo.get_business_facts(business_id)
    except Exception:
        facts = []
    try:
        rules = repo.get_owner_rules(business_id)
    except Exception:
        rules = []
    owner_messages = _owner_messages(repo, business_id, find.find_id)

    try:
        raw = reasoner.complete_json(
            system=MAKER_SYSTEM_PROMPT,
            user=build_prompt(
                find,
                business=business,
                facts=facts,
                rules=rules,
                owner_messages=owner_messages,
                revision_instruction=revision_instruction,
                previous_artifact=previous_artifact,
            ),
            schema=MAKER_SCHEMA,
        )
        payload = _normalize_payload(raw)
    except (ProviderError, KeyError, TypeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = MakerResult(run_id=run_id, error=error, revision=revision)
        repo.finish_run(
            run_id, status="failed", error=error, note=result.note,
            **_token_receipt(reasoner),
        )
        return result

    if can_continue is not None and not can_continue():
        result = MakerResult(run_id=run_id, cancelled=True, revision=revision)
        repo.finish_run(
            run_id, status="ok", note=result.note, **_token_receipt(reasoner)
        )
        return result

    location = None
    try:
        object_prefix = f"tasks/{task_id}" if task_id else f"finds/{find.find_id}"
        filename = ARTIFACT_KIND if revision == 1 else f"{ARTIFACT_KIND}-v{revision}"
        location = store.put(key=f"{object_prefix}/{filename}.md", body=payload["body"])
    except ArtifactStoreError:
        pass

    metadata = {
        "artifact_type": payload["artifact_type"],
        "use_context": artifact_use_context(payload["artifact_type"]),
        "owner_questions": payload["owner_questions"],
        "sections": payload["sections"],
        "revision_instruction": _compact(revision_instruction, 900) if revision_instruction else None,
        "action_manifest": _action_manifest(payload),
    }
    try:
        artifact_id = repo.insert_artifact(
            find_id=find.find_id,
            kind=ARTIFACT_KIND,
            title=payload["title"],
            preview=_preview(payload["body"]),
            s3_bucket=location.bucket if location else None,
            s3_key=location.key if location else None,
            run_id=run_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
            body=payload["body"],
            summary=payload["summary"],
            owner_action=payload["owner_action"],
            review_state=payload["review_state"],
            metadata=metadata,
            revision=revision,
            parent_artifact_id=(previous_artifact.artifact_id if previous_artifact else None),
        )
    except RepositoryError as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = MakerResult(run_id=run_id, error=error, revision=revision)
        repo.finish_run(
            run_id, status="failed", error=error, note=result.note,
            **_token_receipt(reasoner),
        )
        return result

    result = MakerResult(
        run_id=run_id,
        artifact_id=artifact_id,
        location=f"s3://{location.bucket}/{location.key}" if location else None,
        summary=payload["summary"],
        review_state=payload["review_state"],
        revision=revision,
    )
    repo.finish_run(
        run_id, status="ok", note=result.note, **_token_receipt(reasoner)
    )
    return result


def _preview(body: str) -> str:
    flat = " ".join(body.split())
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[:PREVIEW_CHARS].rstrip() + "…"
