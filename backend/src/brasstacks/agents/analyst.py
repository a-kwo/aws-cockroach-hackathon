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
from brasstacks.repository import ChatMessage, EvidenceRef, Repository, Retrieved

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

#: A bounded continuity window from Ask. Complete conversation history remains
#: in CockroachDB; only the newest owner messages enter a nightly prompt.
RECENT_OWNER_MESSAGES_SHOWN = 4

#: How many moves a night may propose. Three because the deck shows a small
#: number well and because the Analyst's later suggestions get noticeably
#: thinner — asking for ten would buy seven restatements of the first.
MAX_FINDS_PER_NIGHT = 3

_FIND_PROPERTIES = {
    "emoji": {"type": "string"},
    "title": {"type": "string"},
    "summary": {"type": "string"},
    "rationale": {"type": "string"},
    "move": {"type": "string"},
    "predicted_daily_cents": {"type": "integer"},
    "confidence": {"type": "number"},
    "verify_after_days": {"type": "integer"},
    "evidence_observation_ids": {"type": "array", "items": {"type": "string"}},
}

FIND_SCHEMA = {
    "type": "object",
    "properties": dict(_FIND_PROPERTIES),
    "required": ["title", "summary", "rationale", "move",
                 "predicted_daily_cents", "confidence", "verify_after_days",
                 "evidence_observation_ids"],
    "additionalProperties": False,
}

#: A night proposes several moves, so the model returns a list. Kept as an
#: object with one key rather than a bare array: some providers will not accept
#: an array at the top level of a response schema.
#:
#: No `minItems`/`maxItems`. The Anthropic structured-output endpoint rejects
#: them outright — "For 'array' type, property 'maxItems' is not supported" —
#: and the 400 costs a whole night, because the Analyst only hits it after
#: retrieval has already run and been paid for. The count is bounded by the
#: prompt and enforced by slicing in code, where it cannot break a request.
#: Pinned by test_agents.TestFindsSchemaIsAcceptedByTheProvider.
FINDS_SCHEMA = {
    "type": "object",
    "properties": {
        "finds": {
            "type": "array",
            "items": FIND_SCHEMA,
        },
    },
    "required": ["finds"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the Analyst for Brass Tacks. Your single goal is \
to turn each owner's accumulated market evidence and owner conversation memory \
into the highest-value, measurable, executable growth moves delivered through \
the For You feed — and then let Meter hold those predictions to account.

You work closely with Ask. Ask stores the owner's questions, priorities, \
constraints and preferences in CockroachDB. Treat those owner messages as \
authoritative context about what this owner wants and can execute, while \
treating customer, competitor and demand observations as the market evidence \
that supports a move.

You will be given what is known about one business and a compact set of rows \
retrieved from its memory. Propose THREE distinct moves, best first.

Three is the expectation, not a ceiling to aim under. The owner sees a deck and \
one card is a thin morning. Look across different parts of the business — what \
they sell and at what price, how they are found, what the listings say, what the \
room and the hours allow, what nearby rivals leave uncontested — and you will \
almost always find three.

Each move must stand on its own. Three angles on the same idea is one move, not \
three. Return fewer only when the memory genuinely supports fewer, and never pad \
to reach three.

Writing, which matters as much as the reasoning here. The owner reads these on \
a card, between other jobs:
- `title`: under 60 characters. What to do, not why.
- `summary`: ONE sentence, under 180 characters, in plain language. It is the \
compact fallback for notifications and receipts. Lead with the opportunity.
- `rationale`: your complete owner-facing argument. It is shown consistently in \
the scrollable For You card on desktop and mobile. Reference observation ids \
here, never in the title or summary.
- `move`: the steps to take. Write them as separate short sentences, one action \
each, so they can be shown as a checklist. No numbered lists inside a paragraph.

Hard requirements:
- Money is INTEGER CENTS PER DAY. $23/day is 2300. Never dollars, never a month.
- Predict conservatively. Every prediction is stored and checked against real \
outcomes later, and misses are published permanently. An inflated number becomes \
a public miss.
- Cite only observation ids from the list you are given, and only ones that \
genuinely support the move. Citing something unrelated is worse than citing less.
- Prefer a move the owner can actually execute. Do not propose fixing things \
outside their control, such as public parking supply.
- Respect the owner's rules exactly as written. Owner chat is also a strong \
signal of priorities and constraints, but it is not external proof of demand; \
combine it with market observations before making a revenue claim.
- verify_after_days is how long until the effect could be measured. Use 7 to 30 \
for operational changes, longer for anything seasonal.
- Do NOT propose something already on the recent-finds list. If the obvious move \
is taken, find the next best one. A move the owner already rejected is the worst \
thing to propose again."""


@dataclass(frozen=True)
class AnalystResult:
    run_id: str
    #: The first find of the night. Kept alongside `find_ids` because a night
    #: that proposes three moves still has one headline, and every caller that
    #: predates multiple finds asks for exactly this.
    find_id: str | None
    retrieved: int
    queries: tuple[str, ...]
    raw_retrieved: int = 0
    query_hits: tuple[int, ...] = ()
    error: str | None = None
    #: Every find stored tonight, in the order the Analyst ranked them.
    find_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalReceipt:
    """What the vector layer returned before and after deduplication."""

    hits: tuple[Retrieved, ...]
    query_hits: tuple[int, ...]
    raw_hits: int
    # Reused for one owner-memory search. This adds no embedding model call:
    # the centroid is derived from the six market-query vectors already paid for.
    query_vectors: tuple[tuple[float, ...], ...] = ()


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
        hits=merged,
        query_hits=tuple(query_hits),
        raw_hits=sum(query_hits),
        query_vectors=tuple(tuple(float(value) for value in vector) for vector in vectors),
    )




def _owner_memory_context(
    repo: Repository,
    business_id: str,
    query_vectors: Sequence[Sequence[float]],
) -> list[ChatMessage]:
    """Retrieve a bounded owner-context slice without another embedding call.

    Ask stores complete conversation history. The Analyst reuses the centroid of
    its six existing market-query vectors to select semantically relevant owner
    statements, then fills any remaining room with the newest owner messages.
    Market evidence retrieval remains separate, so an owner's belief can shape
    feasibility without becoming proof of demand.
    """
    if not hasattr(repo, "recent_chat_messages"):
        return []

    semantic: list[ChatMessage] = []
    if query_vectors:
        width = len(query_vectors[0])
        if width and all(len(vector) == width for vector in query_vectors):
            centroid = [
                sum(float(vector[index]) for vector in query_vectors) / len(query_vectors)
                for index in range(width)
            ]
            try:
                semantic = list(repo.search_chat_messages(
                    business_id, centroid, limit=RECENT_OWNER_MESSAGES_SHOWN
                ))
            except (AttributeError, NotImplementedError):
                semantic = []

    recent = [
        message for message in repo.recent_chat_messages(
            business_id, limit=RECENT_OWNER_MESSAGES_SHOWN * 2
        )
        if message.role == "user"
    ]

    selected: list[ChatMessage] = []
    seen: set[str] = set()
    # Relevance first, then newest continuity. The prompt remains strictly
    # bounded even if the owner has years of stored conversation.
    for message in [*semantic, *reversed(recent)]:
        if message.message_id in seen:
            continue
        seen.add(message.message_id)
        selected.append(message)
        if len(selected) >= RECENT_OWNER_MESSAGES_SHOWN:
            break
    return selected

def _retrieve(repo: Repository, embedder: Embedder, business_id: str,
              queries: Sequence[str], per_query_limit: int) -> list[Retrieved]:
    """Compatibility wrapper returning only the deduplicated observations."""
    return list(_retrieve_with_receipt(
        repo, embedder, business_id, queries, per_query_limit
    ).hits)


def build_prompt(*, business: dict | None, facts: Sequence[str],
                 rules: Sequence, retrieved: Sequence[Retrieved],
                 today: date, recent_finds: Sequence = (),
                 competitors: Sequence = (),
                 owner_messages: Sequence = ()) -> str:
    name = (business or {}).get("name", "this business")
    city = (business or {}).get("city")
    goal = (business or {}).get("goal_monthly_cents")

    lines = [f"Business: {name}" + (f", {city}" if city else "")]
    if goal:
        lines.append(f"Stated goal: +${goal / 100:,.0f} per month")
    lines.append(f"Today's date: {today.isoformat()}")

    lines.append("\nWhat the owner has told us:")
    lines.extend(f"- {fact}" for fact in facts) if facts else lines.append("- (nothing yet)")

    if owner_messages:
        lines.append("\nRecent owner conversation memory from Ask:")
        for message in owner_messages:
            content = " ".join(str(message.content).split())
            if len(content) > 240:
                content = content[:239].rstrip() + "…"
            lines.append(f"- {content}")
        lines.append(
            "Use these as owner priorities and constraints. Do not treat them "
            "as external customer or market evidence."
        )

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

    lines.append(
        "\nPropose three distinct moves, best first, as JSON matching the schema. "
        "Return fewer only when the evidence genuinely cannot support three."
    )
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
    owner_memory_ids: Sequence[str] = (),
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
            owner_memory_ids=owner_memory_ids,
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

    owner_memory_messages = _owner_memory_context(
        repo, business_id, retrieval.query_vectors
    )

    prompt = build_prompt(
        business=repo.get_business(business_id) if hasattr(repo, "get_business") else None,
        facts=repo.get_business_facts(business_id),
        rules=repo.get_owner_rules(business_id),
        retrieved=retrieved,
        today=today,
        recent_finds=repo.recent_finds(business_id, limit=RECENT_FINDS_SHOWN),
        competitors=competitors,
        owner_messages=owner_memory_messages,
    )

    similarity_by_id = {r.observation_id: r.similarity for r in retrieved}

    try:
        payload = reasoner.complete_json(system=SYSTEM_PROMPT, user=prompt,
                                         schema=FINDS_SCHEMA)
        # A provider that ignores the wrapper and returns a bare find is still
        # a usable night — treat it as a list of one rather than failing.
        proposals = payload.get("finds") if isinstance(payload, dict) else None
        if not isinstance(proposals, list) or not proposals:
            proposals = [payload]
        finds = [
            parse_find(item, today=today,
                       known_observation_ids=similarity_by_id.keys())
            for item in proposals[:MAX_FINDS_PER_NIGHT]
        ]
        find = finds[0]
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
                owner_memory_ids=[message.message_id for message in owner_memory_messages],
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

    find_ids = [
        repo.insert_find_with_evidence(
            business_id,
            emoji=proposal.emoji,
            title=proposal.title,
            summary=proposal.summary,
            rationale=proposal.rationale,
            move=proposal.move,
            predicted_daily_cents=proposal.predicted_daily_cents,
            confidence=proposal.confidence,
            verify_after=proposal.verify_after,
            # A fresh find is a proposal, not a decision. The owner holds the
            # leash, and only an accepted find is ever judged.
            status="proposed",
            run_id=run_id,
            evidence=[
                EvidenceRef(observation_id, similarity_by_id[observation_id])
                for observation_id in proposal.evidence_observation_ids
            ],
        )
        for proposal in finds
    ]
    find_id = find_ids[0]

    repo.finish_run(
        run_id,
        status="ok",
        note=_run_note(
            f"{len(retrieved)} retrieved; proposed {len(finds)} move(s), "
            f"led by {find.title!r} at +{find.predicted_daily_cents}c/day, "
            f"verify after {find.verify_after.isoformat()}",
            receipt=retrieval,
            # Across every find of the night: the receipt is about what the
            # retrieval earned, and a second find citing more of it counts.
            cited_hits=sum(len(p.evidence_observation_ids) for p in finds),
            find_id=find_id,
            queries=queries,
            per_query_limit=per_query_limit,
            owner_memory_ids=[message.message_id for message in owner_memory_messages],
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
        find_ids=tuple(find_ids),
    )
