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

**No source may fill the prompt.** Find 7c4a9124 cited four rows that were one
Grubhub storefront — three fragments of a single fetch plus the same store on
Seamless — and read as four confirmations. The rows are capped per source after
ranking, and what the cap removed goes in the run receipt.

**The model must say what would beat its own reading.** The same find called
that storefront broken on a page fetched at 08:07, an hour before the
restaurant opened. "The bot looked while we were shut" fits that evidence
better, and nothing in the find recorded that the question had been asked. So
the prompt now demands the local capture time and the open/closed answer before
any outage claim, and `alternative_explanation` is stored beside the
prediction. Rules and a column, not a gate: gating this rejected 9 of 9 finds
in review, and an owner with an empty deck is not better served than an owner
with a weak one.

**It is told what the business already is, not only what the market says.**
Retrieval answers "what is relevant tonight"; it cannot answer "what does this
restaurant already sell". Find 818fdb2d told Yellow Cow to launch a fixed-price
weekday lunch set while three uncited rows of its own memory named the Dosirak
set meal as its best-selling delivery item. So `business_state` is built from
the *whole* stored corpus — offers, hours reconciled per weekday, and the
domain the owner actually declared — and that block sits above the retrieved
rows. It changes what the Analyst knows, never how many moves it may propose.

**A second model tries to prove each move wrong before it is written.** Every
mechanism above is deterministic, and the audit's remaining failures are not
structural — an invented competitor set, a claim that no bento photo exists
beside a cited row containing one. Those are ordinary nouns, and no extractor
or threshold sees them. So `agents/refuter.py` gets one call per night, with
only the find text and the rows it cites, and the framing inverted: refute,
never review. It runs here rather than in `night.py` because "before the owner
sees it" means before the row exists — a find stored as `proposed` is on her
board the next time the page loads. It fails open: an outage publishes the deck
without its dollar figures, because a checker that can blank the board is worse
than no checker.
"""

from __future__ import annotations

import inspect
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta, timezone

from brasstacks import repository as repository_module
from brasstacks.agent_runs import closing_run
from brasstacks.agents.refuter import (
    DEMOTE, Refutation, RefutationResult, refute_finds,
)
from brasstacks.analyst_trace import encode_analyst_trace
from brasstacks.business_state import build_business_state, describe_business_state
from brasstacks.competitors import CompetitorScout, describe_competitors
from brasstacks.finds import (
    ClaimVerdict, EvidenceFact, InvalidFindError, ParsedFind, parse_find,
)
from brasstacks.providers import Embedder, ProviderError, Reasoner
from brasstacks.provenance import source_host, source_identity
from brasstacks.repository import ChatMessage, EvidenceRef, Repository, Retrieved
from brasstacks.signals import classify_statement

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

#: How many rows one source may put in a night's prompt. Two rather than one,
#: because a second fragment of a long page does often carry something the first
#: did not; two rather than three, because the three captures find 7c4a9124
#: cited shared a 492-character prefix and two of them shared 980. Applied after
#: ranking, so a source keeps its strongest rows and loses only the repetition.
MAX_ROWS_PER_SOURCE = 2

#: Below this, a row does not enter the prompt at any tenant's expense. Measured
#: on live Titan v2 embeddings of ANALYST_QUERIES against all three live tenants
#: on 2026-08-03, and every higher value was disqualified by a row it deleted:
#: 0.30 leaves Palsaik with ZERO retrieved rows (its top row is 0.299) and
#: Yellow Cow with three; 0.25 kills the Dosirak set-meal row that is the
#: correction to find 818fdb2d; 0.20 kills the maps.apple.com "Claim This Place"
#: row; 0.15 kills it on the corpus in the cluster today, by 0.003. Cosine
#: similarity is orthogonal to verifiability — that Apple Maps row scores 0.147
#: and is the single cheapest checkable true win in the whole dataset. What 0.10
#: costs is the tail: a driving-directions card, a rival ramen listing, a
#: marketing bio, three to five rows per tenant per night.
ABSOLUTE_REJECT = 0.10

#: The operative bar in practice: a row enters iff it scores at least this
#: fraction of the tenant's own top similarity. The three tenant tops cluster at
#: 0.295–0.347, so 0.40x lands the bar at 0.118–0.139 — above ABSOLUTE_REJECT
#: for every live tenant, which makes the absolute floor a guard for a thin or
#: brand-new tenant rather than the everyday rule. 0.50x was rejected by
#: measurement: it puts Palsaik's bar at 0.150 and loses the Apple Maps row by
#: 0.003 on the corpus that is actually stored. A rule that flips the dataset's
#: best find on four thousandths of cosine is a coin toss, not a threshold.
RELATIVE_ADMISSION = 0.40

#: The backstop. Measured worst case under the rule is Yellow Cow at nine
#: admitted rows, so eight is the largest value that fires on no live tenant
#: tonight — a backstop that fires routinely is the real rule — while sitting
#: one row under the observed floor, so it has teeth the moment a corpus thins.
#: Four sources because MAX_ROWS_PER_SOURCE = 2 already forces sources >=
#: ceil(rows/2); anything higher would be a second, stricter row cap wearing a
#: different name.
MIN_PROMPT_ROWS = 8
MIN_PROMPT_SOURCES = 4

#: How many prior finds to show the Analyst so it does not repeat itself.
#: `recent_finds` sorts in-play moves ahead of proposals, so this needs to be
#: large enough to hold everything running *plus* a useful tail of recent
#: proposals. At 12 the accepted moves filled the window on their own and fresh
#: proposals stopped being visible; the repeats then came from the other side.
RECENT_FINDS_SHOWN = 20

#: A bounded continuity window from Ask. Complete conversation history remains
#: in CockroachDB; only the newest owner messages enter a nightly prompt.
RECENT_OWNER_MESSAGES_SHOWN = 4

#: How much of a tenant's stored corpus the "what this business already has"
#: block is built from. The whole of it, in practice — the three live tenants
#: hold 43 to 52 rows each — and the number is here so that stops being an
#: assumption. Retrieval cannot serve this: the three rows naming Yellow Cow's
#: Dosirak set meal were in memory on the night find 818fdb2d told the owner to
#: launch a set meal, and the vector search returned none of them.
CORPUS_ROWS_READ = 500

#: How many moves a night may propose. Three because the deck shows a small
#: number well and because the Analyst's later suggestions get noticeably
#: thinner — asking for ten would buy seven restatements of the first.
MAX_FINDS_PER_NIGHT = 3

_FIND_PROPERTIES = {
    "emoji": {"type": "string"},
    "title": {"type": "string"},
    # No `enum`, deliberately. The Anthropic structured-output endpoint has
    # already rejected `minItems`/`maxItems` on this schema, and a 400 here
    # costs a whole night — the Analyst only reaches the call after retrieval
    # has been paid for. The vocabulary is stated in the system prompt and
    # enforced in `finds._known_claim_type`, where a bad value costs one label
    # rather than a deck.
    "claim_type": {"type": "string"},
    "summary": {"type": "string"},
    "rationale": {"type": "string"},
    "move": {"type": "string"},
    "alternative_explanation": {"type": "string"},
    "predicted_daily_cents": {"type": "integer"},
    "confidence": {"type": "number"},
    "verify_after_days": {"type": "integer"},
    "evidence_observation_ids": {"type": "array", "items": {"type": "string"}},
}

#: `alternative_explanation` is required *of the model* and optional *of the
#: parser*. Asking for it in the schema is how the check actually gets run —
#: writing down the reading that beats yours is the step find 7c4a9124 never
#: took. Failing a night because the field came back empty would be a rejection
#: gate, and the gates as designed withheld 9 of 9 finds, so `parse_find`
#: tolerates its absence.
FIND_SCHEMA = {
    "type": "object",
    "properties": dict(_FIND_PROPERTIES),
    "required": ["title", "summary", "rationale", "move", "claim_type",
                 "alternative_explanation",
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
- `summary`: ONE complete sentence, ideally 110–160 characters and never over \
180. State the strongest market signal and why it matters to this business. Use \
plain owner language. Do not repeat the title, preview the action steps, mention \
"memory", "rows", "records" or "observations", or include any evidence id.
- `rationale`: your complete traceable argument for the details view and operator \
record. Reference observation ids here only, never in the title or summary. Do \
not repeat the same point merely to make the paragraph longer.
- `move`: the steps to take. Write them as separate short sentences, one action \
each, so they can be shown as a checklist. No numbered lists inside a paragraph.
- `alternative_explanation`: the operator record, not the card. One or two \
sentences: the rival reading, then why you reject it.
- `claim_type`: which KIND of claim this is. Exactly one of the four below.

WHAT KIND OF CLAIM IS THIS? Every find is one of four kinds, and each is held \
to a different standard by the evidence you cite. Choose honestly: a find that \
claims more than its evidence carries is moved down to the kind it can carry, \
and a find whose wording claims more than any kind allows is not shown to the \
owner at all.
- `current_state`: something is true of this business RIGHT NOW — a listing is \
unclaimed, a storefront is not taking orders, a page is missing a photo. The \
strongest claim available. If you rest it on a row that describes a page's \
momentary state ("not available right now", "closed now", "sold out"), that \
capture must have been taken while the business was open. Check the capture \
time in local time first. A shut restaurant's delivery page is not a broken one.
- `pattern`: something recurs — customers keep mentioning the wait, reviewers \
keep naming the same dish. Needs at least TWO sources that can confirm each \
other. One storefront and its own mirror is one source, however many rows.
- `opportunity`: something WOULD work — a fixed-price lunch set would sell, a \
group package would book. Weaker evidence is allowed here, and most good finds \
are this. What is not allowed is wording that asserts a current state. If your \
sentence says something is broken, unavailable or unclaimed, it is a \
`current_state` claim whatever you label it, and it is judged as one.
- `listing_fact`: ONE verbatim, durable fact from ONE source that the owner can \
check in under a minute — "Apple Maps shows Claim This Place on your profile". \
No revenue figure: set predicted_daily_cents to 0. This is the honest answer on \
a thin night, and a true small fact beats an invented large one.

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
- Do NOT propose something on the 'Already proposed' list. If the obvious move \
is taken, find the next best one. A move the owner already rejected is the worst \
thing to propose again. The 'Considered on an earlier night' list is different: \
those were never shown to the owner, and you may raise one again if you can now \
answer what was missing.

Checks every find must survive. Each of these exists because a find was \
published that could not survive it:
- WAS IT OPEN? Before you claim that any storefront, listing, menu, page or \
service is broken, unavailable, down, missing or failing, state in the rationale \
the capture time in the business's own local time and whether the business was \
open at that instant. Capture times are given to you in UTC. If the business was \
closed then, or the hours you were given do not settle it, do not make the claim \
at all — say what you would need to see instead. A shut restaurant's delivery \
page reading "not available right now" is the shop being shut, not a broken \
storefront.
- ONE PAGE IS ONE DOCUMENT. Rows sharing a source and a capture time are one \
document read once, however many ids they carry. Never present them as separate \
captures, multiple sources, several platforms, a pattern, or independent \
confirmation. One document is one signal even when it is four rows.
- NO UNCITED SPECIFICS. Every dish, price, opening hour, competitor and \
availability state you name in `move` or `rationale` must be carried by an \
observation you cite in evidence_observation_ids. If you take a detail from a \
retrieved row, cite that row; if you cannot cite it, do not name it.
- NAME THE ALTERNATIVE. `alternative_explanation` is the strongest rival reading \
of the same evidence — the one a sceptical owner would give — and why you reject \
it. Required for your lead find and worth writing for all three. "The bot looked \
while we were shut" is a real alternative and is often the right one. If the \
evidence you have cannot reject the alternative, propose a different move rather \
than defend this one.
- THEY MAY ALREADY SELL IT. Read the "what this business already has" block \
before you write a move. If your move would launch, add, introduce, create or \
start something that block already lists, it is not a launch — rewrite it as \
improving, pricing or promoting that offer, and name the offer as the block \
names it. One find told an owner to launch a fixed-price weekday lunch set while \
three rows of that owner's own memory named the set meal as its best-selling \
delivery item. Do not drop the move; correct it.
- WHOSE WEBSITE IS THIS? The block names the domain the owner declared, if any. \
A page on any other domain is somebody else's until you say otherwise: name the \
host and say whose page you believe it is before you criticise anything on it. \
Never call a domain the owner did not declare "your website" or "your online \
ordering". If the owner declared no site at all, every page you have is a \
third-party listing and must be described as one.
- SAME WEEKDAY OR NO CONFLICT. Two sources only contradict each other about \
opening hours when they state different times for the same weekday and the same \
service. A source covering Monday and one covering Thursday agree. A window with \
no weekday attached — "Hours Today Pickup" — describes the day the page was \
fetched and contradicts nothing. Use the reconciled per-weekday hours in the \
block, and never call sources contradictory when the block has not said they \
disagree."""


@dataclass(frozen=True)
class FindOutcome:
    """One proposed move, and what the claim standards did to it.

    The structured verdict this tranche produces and does not persist. The
    status value, the `withheld_reason` column and the board's "held back"
    panel belong to the tranche landing beside this one; the Analyst hands over
    the judgement and lets the caller decide where it lives.
    """

    title: str
    #: The tier it may be published as. ``None`` means no tier fitted.
    claim_type: str | None
    verdict: ClaimVerdict
    #: The row it was written to, or ``None`` when it was withheld and the
    #: repository has nowhere to put a withheld find yet.
    find_id: str | None = None
    #: What the adversarial pass made of it. ``None`` when no refuter was wired
    #: for this night, or when the claim standards had already withheld the move
    #: and there was nothing left to challenge.
    refutation: Refutation | None = None

    @property
    def shown(self) -> bool:
        """Did this move reach the owner's board?

        Two independent ways to be held back — the mechanical claim standards
        and the adversary — and a caller must not have to remember both.
        """
        return not (self.verdict.withheld
                    or (self.refutation is not None and self.refutation.withheld))


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
    #: Every find PROPOSED tonight — reaching the owner's board — in the order
    #: the Analyst ranked them. A withheld find is never in here.
    find_ids: tuple[str, ...] = ()
    #: Every move the model wrote, withheld ones included, with its verdict.
    outcomes: tuple[FindOutcome, ...] = ()
    #: Whether the adversarial pass ran tonight. ``None`` when no refuter was
    #: wired, ``False`` when one was and it failed — and a False night publishes
    #: its deck with the dollar figures suppressed rather than blanking it.
    refutation_checked: bool | None = None


@dataclass(frozen=True)
class RetrievalReceipt:
    """What the vector layer returned before and after deduplication."""

    hits: tuple[Retrieved, ...]
    query_hits: tuple[int, ...]
    raw_hits: int
    #: Deduplicated rows dropped because their source was already at the cap.
    source_capped: int = 0
    #: Rows the similarity bar kept out of the prompt, and where it sat.
    floor_removed: int = 0
    floor_threshold: float = 0.0
    #: The backstop had to refill the prompt. See `apply_similarity_floor`.
    low_confidence: bool = False
    # Reused for one owner-memory search. This adds no embedding model call:
    # the centroid is derived from the six market-query vectors already paid for.
    query_vectors: tuple[tuple[float, ...], ...] = ()


def _cap_per_source(
    ordered: Sequence[Retrieved], limit: int,
) -> tuple[list[Retrieved], int]:
    """Keep at most *limit* rows per source, strongest first.

    Applied to the already-ranked merged set, so a source that genuinely
    dominates on relevance still leads the prompt — it just cannot own it. A
    row with no usable URL has no identity and is never capped: the seeded
    corpus and every owner upload are URL-less, and grouping them together
    would delete most of memory on the way to the model.
    """
    kept: list[Retrieved] = []
    dropped = 0
    seen: Counter[str] = Counter()
    for hit in ordered:
        identity = source_identity(hit.source_url)
        if identity is not None:
            if seen[identity] >= limit:
                dropped += 1
                continue
            seen[identity] += 1
        kept.append(hit)
    return kept, dropped


@dataclass(frozen=True)
class FloorDecision:
    """What the similarity bar admitted, what it removed, and at what cost."""

    admitted: tuple[Retrieved, ...]
    removed: tuple[Retrieved, ...]
    #: The bar actually applied, `max(absolute, relative * top)`.
    threshold: float
    top_similarity: float
    #: Distinct sources among the admitted rows.
    sources: int
    #: True when the backstop had to put rows back to reach `min_rows` or
    #: `min_sources`. It means the night is running on thinner retrieval than
    #: the bar wanted, which is a fact about the night and belongs in the
    #: receipt — not a reason to show the owner nothing.
    low_confidence: bool


def _prompt_source_count(rows: Sequence[Retrieved]) -> int:
    """How many distinct sources these rows represent, for prompt breadth.

    A row with no usable URL counts as its own source. Both readings of a NULL
    identity were available and neither should fall out of a comprehension:
    collapsing every URL-less row into one source would make the backstop fire
    forever for a tenant whose corpus is seeded or owner-uploaded, which is
    every tenant on night one. This counter answers "how broad is the prompt",
    and breadth is exactly what a separate unattributed row adds.

    `finds.py` makes the opposite choice for the `pattern` claim standard, where
    the question is independent confirmation rather than breadth, and a row we
    cannot attribute cannot be shown to be independent of anything.
    """
    seen: set[str] = set()
    for hit in rows:
        seen.add(source_identity(hit.source_url) or f"row:{hit.observation_id}")
    return len(seen)


def apply_similarity_floor(
    ranked: Sequence[Retrieved],
    *,
    absolute: float = ABSOLUTE_REJECT,
    relative: float = RELATIVE_ADMISSION,
    min_rows: int = MIN_PROMPT_ROWS,
    min_sources: int = MIN_PROMPT_SOURCES,
) -> FloorDecision:
    """Decide which ranked rows are strong enough to reach the model.

    The rule: a row enters iff ``similarity >= max(absolute, relative * top)``.
    If that admits fewer than *min_rows* rows or fewer than *min_sources*
    distinct sources, rows are put back in similarity order until both are met.

    The two instructions this implements pull against each other — drop
    everything under *absolute* always, and never let the bar take the prompt
    below *min_rows*. The backstop wins, and says so: for a tenant whose whole
    retrieval sits under 0.10 the alternative is an owner opening the board to
    nothing with no explanation, which is the failure this tranche exists to
    avoid. An earlier design of these gates, measured against the real corpus,
    withheld 9 of 9 live finds. That is not caution, it is a dead product. So
    the prompt is filled and the night is flagged `low_confidence` instead.
    """
    ordered = list(ranked)
    if not ordered:
        return FloorDecision(admitted=(), removed=(), threshold=absolute,
                             top_similarity=0.0, sources=0, low_confidence=True)

    top = max(hit.similarity for hit in ordered)
    threshold = max(absolute, relative * top)

    admitted = [hit for hit in ordered if hit.similarity >= threshold]
    held = [hit for hit in ordered if hit.similarity < threshold]
    # Strongest first, so a top-up restores the best of what the bar rejected.
    held.sort(key=lambda hit: hit.similarity, reverse=True)

    low_confidence = False
    while held and (len(admitted) < min_rows
                    or _prompt_source_count(admitted) < min_sources):
        admitted.append(held.pop(0))
        low_confidence = True

    if len(admitted) < min_rows or _prompt_source_count(admitted) < min_sources:
        low_confidence = True

    admitted.sort(key=lambda hit: hit.similarity, reverse=True)
    return FloorDecision(
        admitted=tuple(admitted),
        removed=tuple(held),
        threshold=threshold,
        top_similarity=top,
        sources=_prompt_source_count(admitted),
        low_confidence=low_confidence,
    )


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
    deduplicated, source-capped set that actually enters the prompt, and
    ``source_capped`` is how many rows the cap took out of it.
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
    kept, capped = _cap_per_source(ordered, MAX_ROWS_PER_SOURCE)
    # The bar runs after the cap, not before it. A source at its limit has
    # already lost its weakest rows, and running the bar first would let a
    # storefront's third fragment take a slot from a different site before the
    # cap could reject it anyway.
    floor = apply_similarity_floor(kept)
    # Re-rank so `rank` reflects position in the merged set, not in one query.
    # This is the number that reaches `find_evidence.rank`, so it is numbered
    # after the cap and after the bar: a row the model never saw has no
    # retrieval position.
    merged = tuple(
        Retrieved(
            observation_id=r.observation_id, content=r.content, kind=r.kind,
            similarity=r.similarity, rank=rank, observed_at=r.observed_at,
            source_name=r.source_name, source_url=r.source_url,
            subject=r.subject,
        )
        for rank, r in enumerate(floor.admitted)
    )
    return RetrievalReceipt(
        hits=merged,
        query_hits=tuple(query_hits),
        raw_hits=sum(query_hits),
        source_capped=capped,
        floor_removed=len(floor.removed),
        floor_threshold=floor.threshold,
        low_confidence=floor.low_confidence,
        query_vectors=tuple(tuple(float(value) for value in vector) for vector in vectors),
    )




# ---------------------------------------------------------------------------
# Was it open? — turning a UTC capture stamp into an answer
# ---------------------------------------------------------------------------
#
# The claim standard in finds.py needs one input this system has never held: was
# the business trading at the moment Radar fetched the page? Nothing stores a
# timezone, and all three live tenants have a NULL `region` with the state
# buried in the address string — "1835 W Redondo Beach Blvd, Gardena, CA 90247".
#
# So the offset is derived here, in pure Python, from the address and the
# standard US rule. No zoneinfo, no tzdata: the Lambda image installs psycopg,
# anthropic and boto3, and a timezone database is not worth widening that for
# one lookup. An address this cannot place returns None, and None flows all the
# way through as "we could not tell" — never as a guess dressed up as an answer.

#: US state and territory codes to (standard offset in minutes, observes DST).
#: Only what the product actually serves. A tenant outside this list gets None,
#: which costs it the current_state tier and nothing else.
_US_TIMEZONES: dict[str, tuple[int, bool]] = {
    **{code: (-300, True) for code in (
        "CT", "DE", "FL", "GA", "IN", "KY", "MA", "MD", "ME", "MI", "NC", "NH",
        "NJ", "NY", "OH", "PA", "RI", "SC", "VA", "VT", "WV", "DC")},
    **{code: (-360, True) for code in (
        "AL", "AR", "IA", "IL", "KS", "LA", "MN", "MO", "MS", "ND", "NE", "OK",
        "SD", "TN", "TX", "WI")},
    **{code: (-420, True) for code in ("CO", "ID", "MT", "NM", "UT", "WY")},
    **{code: (-480, True) for code in ("CA", "NV", "OR", "WA")},
    "AZ": (-420, False),   # no DST, so it sits on Mountain Standard all year
    "HI": (-600, False),
    "AK": (-540, True),
    "PR": (-240, False),
}

#: A two-letter state code as it appears in a US postal address: after a comma,
#: on its own, usually before a ZIP. The comma is load-bearing — without it
#: "31208 Palos Verdes Dr W" offers "Dr" and "W" as candidates, and the word
#: "IN" appears in ordinary prose constantly.
_STATE_IN_ADDRESS = re.compile(r",\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\b")


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """The *nth* given weekday of a month, e.g. the second Sunday of March."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _in_us_dst(moment: datetime) -> bool:
    """The federal rule since 2007: second Sunday of March to first Sunday of
    November, both switching at 02:00 local. The hour of the switch is not
    modelled — the question this feeds is whether a restaurant was trading, and
    no restaurant's answer turns on the hour a clock moved at 2am."""
    start = _nth_weekday(moment.year, 3, 6, 2)   # Sunday == weekday 6
    end = _nth_weekday(moment.year, 11, 6, 1)
    return start <= moment.date() < end


def local_utc_offset_minutes(place: str | None, moment: datetime) -> int | None:
    """Minutes to add to a UTC stamp to read it as local time, or ``None``.

    ``place`` is whatever the business record holds — the `region` column, the
    `city` column, or both joined. Returns ``None`` for anything it cannot
    place, because a guessed offset produces an openness answer that reads as
    measured, and a wrong "the shop was open" is exactly the mistake find
    7c4a9124 made in the other direction.
    """
    match = _STATE_IN_ADDRESS.search(str(place or ""))
    if match is None:
        return None
    zone = _US_TIMEZONES.get(match.group(1))
    if zone is None:
        return None
    standard, observes_dst = zone
    return standard + 60 if observes_dst and _in_us_dst(moment) else standard


def capture_was_open(moment, state, offset_minutes: int | None) -> bool | None:
    """Was this business trading when the page was captured? ``None`` if unknown.

    Three-valued, and the third value is the point. Every observation in the
    cluster was captured before its tenant opened, so a gate that reads "we
    could not tell" as "it was open" is no gate at all — and one that reads it
    as "it was shut" withholds every find in the product. `finds.py` treats
    ``None`` as "cannot carry a current-state claim", which demotes rather than
    withholds unless the wording leaves no weaker tier.
    """
    if moment is None or state is None or offset_minutes is None:
        return None
    local = moment + timedelta(minutes=offset_minutes)
    minutes = local.hour * 60 + local.minute
    by_weekday = {day.weekday: day for day in getattr(state, "days", ())}

    today = by_weekday.get(local.weekday())
    # Yesterday's window may still be running: a Saturday 17:00–02:00 service is
    # stored as closes=1560, and a Sunday 01:00 capture is inside it.
    yesterday = by_weekday.get((local.weekday() - 1) % 7)
    if today is None and yesterday is None:
        return None

    for day, offset in ((today, 0), (yesterday, 24 * 60)):
        if day is None:
            continue
        for claim in day.claims:
            if claim.opens is None or claim.closes is None:
                continue
            if claim.opens <= minutes + offset < claim.closes:
                return True
    # Memory covers this weekday and none of its windows contain the capture.
    return False if today is not None else None


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


def _stored_corpus(repo: Repository, business_id: str) -> list | None:
    """Every row memory holds for this tenant, or None if it cannot be read.

    Read once per night and used twice: `business_state` needs the whole corpus
    to know what the tenant already sells, and the claim standards need each
    row's persisted `statement_type`.

    Returns None when the repository predates `all_observations`. The deployed
    Lambda image and the local harness upgrade separately, and a night must not
    fail because the two halves landed in either order; that mistake cost us a
    night once already over the ledger's `actual_daily_cents` column.
    """
    reader = getattr(repo, "all_observations", None)
    if reader is None:
        return None
    try:
        return list(reader(business_id, limit=CORPUS_ROWS_READ))
    except (AttributeError, NotImplementedError, TypeError):
        return None


def _business_state(business, facts: Sequence[str], stored):
    """What memory already knows the business *is*, or None.

    Built from the whole stored corpus rather than tonight's retrieval, because
    the failure it exists to stop is a find recommending something the tenant
    already sells — and the rows proving that were in memory and unretrieved.
    Without the block the Analyst is exactly as well informed as it was
    yesterday.
    """
    if stored is None:
        return None
    return build_business_state(business=business, facts=facts,
                                observations=stored)


def _evidence_facts(
    retrieved: Sequence[Retrieved],
    *,
    stored,
    business: dict | None,
    business_state,
) -> dict[str, EvidenceFact]:
    """What the claim standards need to know about each retrieved row.

    Three things `Retrieved` does not carry, assembled here so that `finds.py`
    stays a pure validator with no repository, no clock and no timezone table.

    `statement_type` prefers the value Radar persisted and falls back to
    re-classifying the row's own text. The fallback is load-bearing right now:
    every one of the 124 web rows in the cluster was written before typing
    existed and carries NULL, so without it the page_state gate would be blind
    to exactly the rows it was built for. Re-classification is deterministic,
    offline and free, and is the same function Radar calls on insert.
    """
    place = " ".join(str((business or {}).get(field) or "")
                     for field in ("region", "city"))
    persisted = {
        str(getattr(row, "observation_id", "")): getattr(row, "statement_type", None)
        for row in (stored or ())
    }
    facts: dict[str, EvidenceFact] = {}
    for hit in retrieved:
        statement_type = (persisted.get(hit.observation_id)
                          or classify_statement(hit.content))
        offset = (local_utc_offset_minutes(place, hit.observed_at)
                  if hit.observed_at is not None else None)
        facts[hit.observation_id] = EvidenceFact(
            observation_id=hit.observation_id,
            statement_type=statement_type,
            source_identity=source_identity(hit.source_url),
            captured_while_open=capture_was_open(
                hit.observed_at, business_state, offset),
        )
    return facts


def _withheld_status() -> str | None:
    """The status a find the pipeline declined to show is stored under.

    Probed rather than named, because the status value, the `withheld_reason`
    column and both migrations belong to the tranche landing beside this one.
    Until they arrive a withheld find is judged and reported but not written —
    the alternative is a night that stores nothing at all because one INSERT
    named a column the cluster has never heard of, which has cost us a night
    twice: once for `find.alternative_explanation` and once for the ledger's
    actual column.
    """
    return getattr(repository_module, "WITHHELD_STATUS", None)


def _can_store_withheld(repo: Repository) -> bool:
    if _withheld_status() is None:
        return False
    try:
        parameters = inspect.signature(repo.insert_find_with_evidence).parameters
    except (TypeError, ValueError):
        return False
    return ("withheld_reason" in parameters
            or any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in parameters.values()))


def _retrieve(repo: Repository, embedder: Embedder, business_id: str,
              queries: Sequence[str], per_query_limit: int) -> list[Retrieved]:
    """Compatibility wrapper returning only the deduplicated observations."""
    return list(_retrieve_with_receipt(
        repo, embedder, business_id, queries, per_query_limit
    ).hits)


def _observed_stamp(moment) -> str:
    """Where the row came from in time, to the second.

    A date was all the prompt carried, which is why three rows written by one
    fetch — identical to the microsecond — could read as three sightings on one
    day. Seconds are enough to make a shared fetch obvious without spending a
    line on it.
    """
    if moment is None:
        return "unknown"
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _observation_line(hit: Retrieved) -> str:
    """One retrieved row, with enough provenance to weigh it.

    Kept to a single line because this repeats ~20 times per prompt: kind,
    subject, source, when, similarity, content. The source is the host rather
    than the full URL — every Tavily row is stored under source_name "web", so
    the host is the only thing that tells one storefront from another, and the
    path would cost more tokens than it earns.
    """
    subject = f" about {hit.subject}" if hit.subject else ""
    source = source_host(hit.source_url) or hit.source_name
    origin = f", {source}" if source else ""
    return (
        f"- id={hit.observation_id} [{hit.kind}{subject}{origin}, "
        f"{_observed_stamp(hit.observed_at)}, "
        f"similarity {hit.similarity:.2f}] {hit.content}"
    )


def build_prompt(*, business: dict | None, facts: Sequence[str],
                 rules: Sequence, retrieved: Sequence[Retrieved],
                 today: date, recent_finds: Sequence = (),
                 competitors: Sequence = (),
                 owner_messages: Sequence = (),
                 business_state=None) -> str:
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

    # Above the market rows on purpose. Everything below this point is what
    # somebody else says about the business; this is what the business already
    # is, and a move that contradicts it is wrong before it is priced.
    inventory = describe_business_state(business_state) if business_state else ""
    if inventory:
        lines.append("")
        lines.append(inventory)

    # Without this the Analyst re-proposes the same move night after night: it
    # remembers the business's observations but not its own recommendations.
    #
    # Two lists, not one. A find the owner saw is off limits; a find a gate
    # withheld was never proposed to anyone, and forbidding it would mean the
    # corrected version can never be raised either.
    if recent_finds:
        shown = [found for found in recent_finds
                 if getattr(found, "seen_by_owner", True)]
        held_back = [found for found in recent_finds
                     if not getattr(found, "seen_by_owner", True)]
        if shown:
            lines.append("\nAlready proposed — do not repeat any of these:")
            for found in shown:
                lines.append(
                    f"- [{found.status}] {found.title} "
                    f"(+{found.predicted_daily_cents}c/day)"
                )
        if held_back:
            lines.append(
                "\nConsidered on an earlier night and never shown to the owner. "
                "These are NOT off limits. Propose one again if tonight's "
                "evidence answers what was missing last time; do not re-file the "
                "same argument unchanged."
            )
            # No cents figure. A prediction that was never shown was never a
            # promise, and reprinting it invites the model to anchor on it.
            for found in held_back:
                lines.append(f"- {found.title}")

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
    lines.extend(_observation_line(hit) for hit in retrieved)
    # Rows sharing a source and a timestamp are one page read once, not
    # independent confirmations. Said plainly because the model cannot see the
    # capture boundaries, only the rows.
    lines.append(
        "Rows with the same source and the same time came from one capture of "
        "one page. Count them as one signal, however many ids they carry."
    )
    # Everything the "was it open?" check needs, and an honest account of what
    # is missing. Nothing in this system stores opening hours or a timezone: a
    # business row has a city and that is all, so the model is told to name the
    # timezone it assumed rather than silently pick one. The distinction in the
    # first line is load-bearing — a web row's time is when Radar fetched the
    # page, which is the only time that can settle whether a storefront was
    # shut, while a review's time is when the customer wrote it.
    lines.append(
        "\nThe times above are UTC. For a web page it is the moment Radar "
        "fetched it; for a review it is when the customer wrote it. Convert a "
        f"capture time to local time in {city or 'the business city above'} "
        "before you read anything as an outage, and name the timezone you "
        "used — no business record stores one."
    )
    lines.append(
        "Opening hours are not a stored field either. What you have is "
        "whatever the owner facts and the rows above happen to say about when "
        "this business trades. If they do not settle whether it was open at a "
        "capture time, that capture cannot carry a claim that something is "
        "broken."
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
    claims_withheld: int = 0,
    claims_demoted: int = 0,
    refutation: str | None = None,
    refuted_withheld: int = 0,
    refuted_demoted: int = 0,
    refuted_unpriced: int = 0,
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
            source_capped=receipt.source_capped,
            floor_removed=receipt.floor_removed,
            floor_threshold=receipt.floor_threshold,
            low_confidence=receipt.low_confidence,
            claims_withheld=claims_withheld,
            claims_demoted=claims_demoted,
            refutation=refutation,
            refuted_withheld=refuted_withheld,
            refuted_demoted=refuted_demoted,
            refuted_unpriced=refuted_unpriced,
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
    refuter: Reasoner | None = None,
) -> AnalystResult:
    """Retrieve, reason, and store one night's find.

    The run row is opened here and closed by whichever path below ends the
    night. `closing_run` covers the paths that do not exist yet: an abandoned
    row reads to the owner as "the agents are working", forever.

    `refuter` is a second, adversarial model call over the moves this one
    proposed. Optional because a deployment without one has to behave exactly as
    it did before — absence is not a failed check, and the receipt keeps the two
    apart. It runs before anything is written, because "before the owner sees
    it" means before the row exists: a find stored as `proposed` is already on
    her board the next time the page loads.
    """
    run_id = repo.start_run(business_id, agent="analyst", model_id=model_id)
    with closing_run(repo, run_id):
        return _analyst_night(
            run_id=run_id, repo=repo, embedder=embedder, reasoner=reasoner,
            business_id=business_id, today=today, queries=queries,
            per_query_limit=per_query_limit, competitors=competitors,
            refuter=refuter,
        )


def _analyst_night(
    *,
    run_id: str,
    repo: Repository,
    embedder: Embedder,
    reasoner: Reasoner,
    business_id: str,
    today: date,
    queries: Sequence[str],
    per_query_limit: int,
    competitors: Sequence,
    refuter: Reasoner | None = None,
) -> AnalystResult:
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

    business = repo.get_business(business_id) if hasattr(repo, "get_business") else None
    facts = repo.get_business_facts(business_id)
    stored = _stored_corpus(repo, business_id)
    business_state = _business_state(business, facts, stored)

    prompt = build_prompt(
        business=business,
        facts=facts,
        rules=repo.get_owner_rules(business_id),
        retrieved=retrieved,
        today=today,
        recent_finds=repo.recent_finds(business_id, limit=RECENT_FINDS_SHOWN,
                                       include_unseen=True),
        competitors=competitors,
        owner_messages=owner_memory_messages,
        business_state=business_state,
    )

    # Everything the claim standards need that a `Retrieved` row does not
    # carry: what each row asserts, which source it belongs to, and whether the
    # shop was trading when it was captured.
    evidence_facts = _evidence_facts(
        retrieved, stored=stored, business=business,
        business_state=business_state,
    )

    similarity_by_id = {r.observation_id: r.similarity for r in retrieved}
    # `find_evidence.rank` says, in the schema and on screen, that it is the
    # row's position in the retrieved set. It was the order the model wrote its
    # citations in, which put a 0.088 row at rank 0 of find 8b4009e5 while its
    # 0.299 row sat at rank 4.
    rank_by_id = {r.observation_id: r.rank for r in retrieved}

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
                       known_observation_ids=similarity_by_id.keys(),
                       evidence_facts=evidence_facts)
            for item in proposals[:MAX_FINDS_PER_NIGHT]
        ]
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

    # The adversarial pass, over the moves the mechanical standards let through
    # and nothing else. Asking a model to refute something already off the board
    # spends tokens and seconds on a card nobody will see, and a `publish`
    # verdict on one must never be able to put it back.
    survivors = [proposal for proposal in finds if not proposal.claim.withheld]
    refutation = RefutationResult()
    refutation_ran = bool(refuter is not None and survivors)
    if refutation_ran:
        refutation = refute_finds(
            reasoner=refuter,
            finds=survivors,
            retrieved={hit.observation_id: hit for hit in retrieved},
            evidence_facts=evidence_facts,
            business=business,
            business_state=business_state,
        )

    # One judgement per proposal, positionally, or None where there was nothing
    # to challenge. Built as a list rather than looked up by title, because two
    # moves of a night can and do share a title prefix.
    judgements: list[Refutation | None] = [None] * len(finds)
    if refuter is not None:
        position = 0
        for index, proposal in enumerate(finds):
            if proposal.claim.withheld:
                continue
            judgements[index] = refutation.for_index(position)
            position += 1

    def store(proposal: ParsedFind, judgement: Refutation | None) -> str | None:
        """Write one move, on the board or held back, or not at all."""
        held = proposal.claim.withheld or (
            judgement is not None and judgement.withheld)
        # An unrefuted move keeps its price. A demoted one loses it because a
        # specific it named was not carried by its evidence, and an unchecked
        # one loses it because nothing stood between the model's number and the
        # owner. Zero is the same "no revenue figure" state `listing_fact`
        # already uses, and the Meter reads a zero prediction as a find with no
        # bar to clear rather than as a promise of nothing.
        cents = proposal.predicted_daily_cents
        if judgement is not None and not judgement.money_allowed:
            cents = 0
        extra: dict = {}
        if held:
            if not _can_store_withheld(repo):
                return None
            reason = (proposal.claim.reason if proposal.claim.withheld
                      else judgement.reason)
            extra = {"status": _withheld_status(), "withheld_reason": reason}
        else:
            # A fresh find is a proposal, not a decision. The owner holds the
            # leash, and only an accepted find is ever judged.
            extra = {"status": "proposed"}
        return repo.insert_find_with_evidence(
            business_id,
            emoji=proposal.emoji,
            title=proposal.title,
            summary=proposal.summary,
            rationale=proposal.rationale,
            move=proposal.move,
            alternative_explanation=proposal.alternative_explanation,
            # The tier AFTER demotion, not the one the model asked for. It is
            # the difference between "worth a look" and "your storefront is
            # broken", and until it had a column it died with the process:
            # nothing downstream could say which claim a card was making.
            claim_type=proposal.claim_type,
            predicted_daily_cents=cents,
            confidence=proposal.confidence,
            verify_after=proposal.verify_after,
            run_id=run_id,
            evidence=[
                EvidenceRef(observation_id, similarity_by_id[observation_id],
                            rank=rank_by_id[observation_id])
                for observation_id in proposal.evidence_observation_ids
            ],
            **extra,
        )

    outcomes = tuple(
        FindOutcome(title=proposal.title, claim_type=proposal.claim_type,
                    verdict=proposal.claim,
                    find_id=store(proposal, judgements[index]),
                    refutation=judgements[index])
        for index, proposal in enumerate(finds)
    )
    # The board only ever sees what survived — both gates. `find_ids` is what
    # the caller tells the owner about, so a held-back row must not be in it
    # however it was persisted.
    proposed = [outcome for outcome in outcomes if outcome.shown]
    find_ids = [outcome.find_id for outcome in proposed if outcome.find_id]
    find_id = find_ids[0] if find_ids else None
    withheld = len(outcomes) - len(proposed)
    demoted = sum(1 for outcome in outcomes if outcome.verdict.demoted)
    claims_withheld = sum(1 for outcome in outcomes if outcome.verdict.withheld)
    refuted_withheld = sum(1 for judgement in judgements
                           if judgement is not None and judgement.withheld)
    refuted_demoted = sum(1 for judgement in judgements
                          if judgement is not None
                          and judgement.action == DEMOTE)
    # Not the same number. A find the model returned no verdict for publishes
    # unpriced without being demoted, and a card with no money on it has to be
    # explainable from the run row alone.
    refuted_unpriced = sum(1 for index, judgement in enumerate(judgements)
                           if judgement is not None
                           and outcomes[index].shown
                           and not judgement.money_allowed)
    # Absent when no adversarial call was made at all — either nothing is wired,
    # or the claim standards had already taken every move and there was nothing
    # left to challenge. "ok" has to mean a model read this deck and could not
    # fault it, or the key is worth nothing to whoever reads it later.
    refutation_status = (
        ("ok" if refutation.checked else "unavailable") if refutation_ran
        else None)

    if find_id is not None:
        lead = next(index for index, outcome in enumerate(outcomes)
                    if outcome.shown)
        lead_find = finds[lead]
        lead_cents = (0 if judgements[lead] is not None
                      and not judgements[lead].money_allowed
                      else lead_find.predicted_daily_cents)
        headline = (
            f"{len(retrieved)} retrieved; proposed {len(proposed)} move(s), "
            f"led by {lead_find.title!r} at +{lead_cents}c/day, "
            f"verify after {lead_find.verify_after.isoformat()}"
        )
        if withheld:
            headline += f"; {withheld} held back"
    else:
        # Not a failure — the agents looked and did not trust what they saw
        # enough to put money on it. The run stays 'ok' so the board can say
        # so, because "we held everything back" and "the night never ran" are
        # different states and an owner told neither concludes we are broken.
        headline = (f"{len(retrieved)} retrieved; {withheld} move(s) held "
                    "back, none proposed")

    repo.finish_run(
        run_id,
        status="ok",
        note=_run_note(
            headline,
            receipt=retrieval,
            # Distinct rows across every find of the night: the receipt is
            # about what the retrieval earned, and a second find citing more of
            # it counts — but a second find citing the SAME row is not a second
            # row. This was a sum, which made `cited_hits` exceed `unique_hits`
            # the moment two moves shared a citation, and `encode_analyst_trace`
            # raises on that. The whole run note was one overlapping citation
            # away from being lost.
            cited_hits=len({observation_id for p in finds
                            for observation_id in p.evidence_observation_ids}),
            find_id=find_id,
            queries=queries,
            per_query_limit=per_query_limit,
            owner_memory_ids=[message.message_id for message in owner_memory_messages],
            claims_withheld=claims_withheld,
            claims_demoted=demoted,
            refutation=refutation_status,
            refuted_withheld=refuted_withheld,
            refuted_demoted=refuted_demoted,
            refuted_unpriced=refuted_unpriced,
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
        outcomes=outcomes,
        refutation_checked=refutation.checked if refutation_ran else None,
    )
