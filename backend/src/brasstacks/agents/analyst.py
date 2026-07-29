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
    error: str | None = None


def _retrieve(repo: Repository, embedder: Embedder, business_id: str,
              queries: Sequence[str], per_query_limit: int) -> list[Retrieved]:
    """Union of several concrete searches, best similarity per observation.

    The same review is often relevant to more than one hypothesis. Keeping it once
    at its highest score avoids both wasting context and over-weighting it in the
    model's view.
    """
    best: dict[str, Retrieved] = {}
    vectors = embedder.embed(list(queries))
    for vector in vectors:
        for hit in repo.search_observations(business_id, vector,
                                            limit=per_query_limit):
            existing = best.get(hit.observation_id)
            if existing is None or hit.similarity > existing.similarity:
                best[hit.observation_id] = hit

    ordered = sorted(best.values(), key=lambda r: r.similarity, reverse=True)
    # Re-rank so `rank` reflects position in the merged set, not in one query.
    return [
        Retrieved(
            observation_id=r.observation_id, content=r.content, kind=r.kind,
            similarity=r.similarity, rank=rank, observed_at=r.observed_at,
            source_name=r.source_name, subject=r.subject,
        )
        for rank, r in enumerate(ordered)
    ]


def build_prompt(*, business: dict | None, facts: Sequence[str],
                 rules: Sequence, retrieved: Sequence[Retrieved],
                 today: date, recent_finds: Sequence = ()) -> str:
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
) -> AnalystResult:
    run_id = repo.start_run(business_id, agent="analyst", model_id=model_id)

    retrieved = _retrieve(repo, embedder, business_id, queries, per_query_limit)

    if not retrieved:
        # Nothing to reason over. Calling the model anyway would be inviting it
        # to invent a recommendation with no evidence behind it.
        repo.finish_run(run_id, status="ok",
                        note="memory is empty; no find proposed")
        return AnalystResult(run_id=run_id, find_id=None, retrieved=0,
                             queries=tuple(queries))

    prompt = build_prompt(
        business=repo.get_business(business_id) if hasattr(repo, "get_business") else None,
        facts=repo.get_business_facts(business_id),
        rules=repo.get_owner_rules(business_id),
        retrieved=retrieved,
        today=today,
        recent_finds=repo.recent_finds(business_id, limit=RECENT_FINDS_SHOWN),
    )

    similarity_by_id = {r.observation_id: r.similarity for r in retrieved}

    try:
        payload = reasoner.complete_json(system=SYSTEM_PROMPT, user=prompt,
                                         schema=FIND_SCHEMA)
        find = parse_find(payload, today=today,
                          known_observation_ids=similarity_by_id.keys())
    except (ProviderError, InvalidFindError) as e:
        error = f"{type(e).__name__}: {e}"
        repo.finish_run(run_id, status="failed", error=error,
                        note=f"{len(retrieved)} observations retrieved")
        return AnalystResult(run_id=run_id, find_id=None, retrieved=len(retrieved),
                             queries=tuple(queries), error=error)

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
        run_id, status="ok",
        note=(f"{len(retrieved)} retrieved; proposed {find.title!r} at "
              f"+{find.predicted_daily_cents}c/day, verify after "
              f"{find.verify_after.isoformat()}"),
    )
    return AnalystResult(run_id=run_id, find_id=find_id,
                         retrieved=len(retrieved), queries=tuple(queries))
