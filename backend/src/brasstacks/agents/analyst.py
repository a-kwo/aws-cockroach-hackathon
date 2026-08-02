"""The Analyst — the agent that turns accumulated memory into a priced move.

This is where the vector index earns its place. The Analyst does not look at
"tonight's data"; it searches everything ever observed about this business, which
means a review from six weeks ago can drive tonight's recommendation.

Two design decisions here are load-bearing:

**It asks several concrete questions, never one abstract one.** Measured against
the seeded corpus, "Is there unmet demand we are not serving?" returned a top
similarity of 0.238 and surfaced the wrong observations, while "Should this
restaurant open for lunch? Is there midday demand nearby?" returned 0.583 and the
right cluster. Embedding models match on concrete language. One open-ended
strategic query is a design bug.

**The model may only cite what retrieval returned.** Observation ids go into the
prompt and citations are validated against that exact set, so a find's evidence
trail cannot contain anything the search did not surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from brasstacks.analyst_trace import encode_analyst_trace
from brasstacks.competitors import CompetitorScout, describe_competitors
from brasstacks.finds import InvalidFindError, parse_find
from brasstacks.providers import Embedder, ProviderError, Reasoner
from brasstacks.repository import EvidenceRef, Repository, Retrieved

#: Concrete hypotheses, one per line of business inquiry. Each is phrased the way
#: a customer or competitor would describe the situation, because that is what
#: the corpus actually contains.
ANALYST_QUERIES = (
    "Which dishes do customers praise most, and are they underpriced compared to nearby restaurants?",
    "Customers complain about waiting a long time for a table, or leaving without eating",
    "Is there demand at times or days this restaurant is not currently open, such as lunch?",
    "What are nearby competing restaurants charging, and what have they changed recently?",
    "What do the lowest-rated reviews complain about, and is it something the owner can fix?",
    "What local trends or nearby developments could change demand in the next few months?",
)

DEFAULT_PER_QUERY_LIMIT = 6

#: How many prior finds to show the Analyst so it does not repeat itself.
#: `recent_finds` sorts in-play moves ahead of proposals, so this needs to be
#: large enough to hold everything running *plus* a useful tail of recent
#: proposals. At 12 the accepted moves filled the window on their own and fresh
#: proposals stopped being visible; the repeats then came from the other side.
RECENT_FINDS_SHOWN = 20

FIND_SCHEMA = {
    "type": "object",
    "properties": {
        "emoji": {"type": "string"},
        "title": {"type": "string"},
        "rationale": {"type": "string"},
        "move": {"type": "string"},
        "predicted_daily_cents": {"type": "integer"},
        "confidence": {"type": "number"},
        "verify_after_days": {"type": "integer"},
        "evidence_observation_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "rationale", "move", "predicted_daily_cents",
                 "confidence", "verify_after_days", "evidence_observation_ids"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the Analyst for Brass Tacks, which finds revenue \
opportunities for small businesses and is then held to account for them.

You will be given what is known about one business and a set of observations \
retrieved from its memory. Propose exactly one move.

Hard requirements:
- Money is INTEGER CENTS PER DAY. $23/day is 2300. Never dollars, never a month.
- Predict conservatively. Every prediction is stored and checked against real \
outcomes later, and misses are published permanently. An inflated number becomes \
a public miss.
- Cite only observation ids from the list you are given, and only ones that \
genuinely support the move. Citing something unrelated is worse than citing less.
- Prefer a move the owner can actually execute. Do not propose fixing things \
outside their control, such as public parking supply.
- Respect the owner's rules exactly as written.
- verify_after_days is how long until the effect could be measured. Use 7 to 30 \
for operational changes, longer for anything seasonal.
- Do NOT propose something already on the recent-finds list. If the obvious move \
is taken, find the next best one. A move the owner already rejected is the worst \
thing to propose again."""


@dataclass(frozen=True)
class AnalystResult:
    run_id: str
    find_id: str | None
    retrieved: int
    queries: tuple[str, ...]
    raw_retrieved: int = 0
    query_hits: tuple[int, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class RetrievalReceipt:
    """What the vector layer returned before and after deduplication."""

    hits: tuple[Retrieved, ...]
    query_hits: tuple[int, ...]
    raw_hits: int


def _retrieve_with_receipt(
    repo: Repository,
    embedder: Embedder,
    business_id: str,
    queries: Sequence[str],
    per_query_limit: int,
) -> RetrievalReceipt:
    """Union concrete searches and retain the counts needed for an audit trail.

    ``query_hits`` tells an operator what each market question returned.
    ``raw_hits`` counts repeated matches across all questions. ``hits`` is the
    deduplicated set that actually enters the prompt.
    """
    best: dict[str, Retrieved] = {}
    query_hits: list[int] = []
    vectors = embedder.embed(list(queries))
    for vector in vectors:
        matches = list(repo.search_observations(
            business_id, vector, limit=per_query_limit
        ))
        query_hits.append(len(matches))
        for hit in matches:
            existing = best.get(hit.observation_id)
            if existing is None or hit.similarity > existing.similarity:
                best[hit.observation_id] = hit

    ordered = sorted(best.values(), key=lambda r: r.similarity, reverse=True)
    # Re-rank so `rank` reflects position in the merged set, not in one query.
    merged = tuple(
        Retrieved(
            observation_id=r.observation_id, content=r.content, kind=r.kind,
            similarity=r.similarity, rank=rank, observed_at=r.observed_at,
            source_name=r.source_name, subject=r.subject,
        )
        for rank, r in enumerate(ordered)
    )
    return RetrievalReceipt(
        hits=merged, query_hits=tuple(query_hits), raw_hits=sum(query_hits)
    )


def _retrieve(repo: Repository, embedder: Embedder, business_id: str,
              queries: Sequence[str], per_query_limit: int) -> list[Retrieved]:
    """Compatibility wrapper returning only the deduplicated observations."""
    return list(_retrieve_with_receipt(
        repo, embedder, business_id, queries, per_query_limit
    ).hits)


def build_prompt(*, business: dict | None, facts: Sequence[str],
                 rules: Sequence, retrieved: Sequence[Retrieved],
                 today: date, recent_finds: Sequence = (),
                 competitors: Sequence = ()) -> str:
    name = (business or {}).get("name", "this business")
    city = (business or {}).get("city")
    goal = (business or {}).get("goal_monthly_cents")

    lines = [f"Business: {name}" + (f", {city}" if city else "")]
    if goal:
        lines.append(f"Stated goal: +${goal / 100:,.0f} per month")
    lines.append(f"Today's date: {today.isoformat()}")

    lines.append("\nWhat the owner has told us:")
    lines.extend(f"- {fact}" for fact in facts) if facts else lines.append("- (nothing yet)")

    lines.append("\nThe owner's rules, which you must respect:")
    if rules:
        for rule in rules:
            cap = f" (cap ${rule.cap_cents / 100:,.0f})" if rule.cap_cents else ""
            lines.append(f"- {rule.rule}{cap}")
    else:
        lines.append("- (none set)")

    # Without this the Analyst re-proposes the same move night after night: it
    # remembers the business's observations but not its own recommendations.
    if recent_finds:
        lines.append("\nAlready proposed — do not repeat any of these:")
        for found in recent_finds:
            lines.append(
                f"- [{found.status}] {found.title} "
                f"(+{found.predicted_daily_cents}c/day)"
            )

    # Live, and deliberately not part of memory: Google's terms forbid storing
    # Places content, so this is a snapshot of tonight rather than something the
    # Analyst can cite. Kept clearly separate from the retrieved observations
    # below, which are citable.
    if competitors:
        lines.append("")
        lines.append(describe_competitors(list(competitors)))
        lines.append(
            "These are context only — you cannot cite them as evidence, "
            "because they are not stored observations."
        )

    lines.append(
        f"\nObservations retrieved from memory ({len(retrieved)}), most relevant "
        "first. Cite by id:"
    )
    for hit in retrieved:
        stamp = hit.observed_at.date().isoformat() if hit.observed_at else "unknown"
        subject = f" about {hit.subject}" if hit.subject else ""
        lines.append(
            f"- id={hit.observation_id} [{hit.kind}{subject}, {stamp}, "
            f"similarity {hit.similarity:.2f}] {hit.content}"
        )

    lines.append("\nPropose one move, as JSON matching the schema.")
    return "\n".join(lines)


def _token_receipt(reasoner: Reasoner) -> dict[str, int | None]:
    """Return recorded model usage without coupling agents to one provider."""
    usage = getattr(reasoner, "last_usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }


def _run_note(
    human: str,
    *,
    receipt: RetrievalReceipt,
    cited_hits: int,
    find_id: str | None,
    queries: Sequence[str],
    per_query_limit: int,
) -> str:
    """Pair a readable log sentence with a structured operator receipt."""
    return "\n".join((
        human,
        encode_analyst_trace(
            query_hits=receipt.query_hits,
            raw_hits=receipt.raw_hits,
            unique_hits=len(receipt.hits),
            cited_hits=cited_hits,
            find_id=find_id,
            queries=queries,
            per_query_limit=per_query_limit,
        ),
    ))


def run_analyst(
    *,
    repo: Repository,
    embedder: Embedder,
    reasoner: Reasoner,
    business_id: str,
    today: date,
    queries: Sequence[str] = ANALYST_QUERIES,
    per_query_limit: int = DEFAULT_PER_QUERY_LIMIT,
    model_id: str | None = None,
    competitors: Sequence = (),
) -> AnalystResult:
    run_id = repo.start_run(business_id, agent="analyst", model_id=model_id)

    retrieval = _retrieve_with_receipt(
        repo, embedder, business_id, queries, per_query_limit
    )
    retrieved = list(retrieval.hits)

    # Handed over by Radar rather than fetched here. Radar is the only agent
    # that touches the outside world; the Analyst reasons over what it is given.
    competitors = list(competitors)

    if not retrieved:
        # Nothing to reason over. Calling the model anyway would be inviting it
        # to invent a recommendation with no evidence behind it.
        repo.finish_run(
            run_id,
            status="ok",
            note=_run_note(
                "memory is empty; no find proposed",
                receipt=retrieval,
                cited_hits=0,
                find_id=None,
                queries=queries,
                per_query_limit=per_query_limit,
            ),
        )
        return AnalystResult(
            run_id=run_id,
            find_id=None,
            retrieved=0,
            queries=tuple(queries),
            raw_retrieved=retrieval.raw_hits,
            query_hits=retrieval.query_hits,
        )

    prompt = build_prompt(
        business=repo.get_business(business_id) if hasattr(repo, "get_business") else None,
        facts=repo.get_business_facts(business_id),
        rules=repo.get_owner_rules(business_id),
        retrieved=retrieved,
        today=today,
        recent_finds=repo.recent_finds(business_id, limit=RECENT_FINDS_SHOWN),
        competitors=competitors,
    )

    similarity_by_id = {r.observation_id: r.similarity for r in retrieved}

    try:
        payload = reasoner.complete_json(system=SYSTEM_PROMPT, user=prompt,
                                         schema=FIND_SCHEMA)
        find = parse_find(payload, today=today,
                          known_observation_ids=similarity_by_id.keys())
    except (ProviderError, InvalidFindError) as e:
        error = f"{type(e).__name__}: {e}"
        repo.finish_run(
            run_id,
            status="failed",
            error=error,
            note=_run_note(
                f"{len(retrieved)} observations retrieved; no find stored",
                receipt=retrieval,
                cited_hits=0,
                find_id=None,
                queries=queries,
                per_query_limit=per_query_limit,
            ),
            **_token_receipt(reasoner),
        )
        return AnalystResult(
            run_id=run_id,
            find_id=None,
            retrieved=len(retrieved),
            queries=tuple(queries),
            raw_retrieved=retrieval.raw_hits,
            query_hits=retrieval.query_hits,
            error=error,
        )

    find_id = repo.insert_find_with_evidence(
        business_id,
        emoji=find.emoji,
        title=find.title,
        rationale=find.rationale,
        move=find.move,
        predicted_daily_cents=find.predicted_daily_cents,
        confidence=find.confidence,
        verify_after=find.verify_after,
        # A fresh find is a proposal, not a decision. The owner holds the leash,
        # and only an accepted find is ever judged.
        status="proposed",
        run_id=run_id,
        evidence=[
            EvidenceRef(observation_id, similarity_by_id[observation_id])
            for observation_id in find.evidence_observation_ids
        ],
    )

    repo.finish_run(
        run_id,
        status="ok",
        note=_run_note(
            f"{len(retrieved)} retrieved; proposed {find.title!r} at "
            f"+{find.predicted_daily_cents}c/day, verify after "
            f"{find.verify_after.isoformat()}",
            receipt=retrieval,
            cited_hits=len(find.evidence_observation_ids),
            find_id=find_id,
            queries=queries,
            per_query_limit=per_query_limit,
        ),
        **_token_receipt(reasoner),
    )
    return AnalystResult(
        run_id=run_id,
        find_id=find_id,
        retrieved=len(retrieved),
        queries=tuple(queries),
        raw_retrieved=retrieval.raw_hits,
        query_hits=retrieval.query_hits,
    )
