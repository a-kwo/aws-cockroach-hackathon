"""Validating a model-proposed find before it becomes a stored prediction.

This is the trust boundary between the Analyst's reasoning and the ledger. Once
a find is written it is a commitment: the Meter will judge it later and the
published hit rate will reflect it. So a proposal that fails validation fails
the whole run rather than being coerced into something plausible-looking.

Pure by design — no database, no model client, no clock. `today` is injected so
the verify window is deterministic under test.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

#: A literal \uXXXX that survived JSON decoding, because the model wrote a
#: doubled backslash. Only well-formed four-hex-digit sequences match, so a
#: legitimate backslash in ordinary text is left alone.
_STRAY_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")

#: Internal observation ids belong in the Analyst trace, not in owner-facing
#: copy. Older rows can fall back from ``summary`` to a rationale that cites
#: ids such as ``(c62cebb0, 51db9a91)``. Keep those links in the database while
#: removing that implementation detail from the card.
_EVIDENCE_ID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{8}"
_EVIDENCE_GROUP = re.compile(
    rf"\s*[\(\[]\s*(?:{_EVIDENCE_ID})(?:\s*[,;]\s*(?:{_EVIDENCE_ID}))*\s*[\)\]]",
    re.IGNORECASE,
)
_BARE_EVIDENCE_ID = re.compile(
    rf"(?<![0-9A-Za-z])(?:{_EVIDENCE_ID})(?![0-9A-Za-z])", re.IGNORECASE
)

#: Ceiling on a single find's predicted daily value. A neighbourhood business
#: earning +$5,000/day from one menu change means the model confused units or
#: hallucinated; publishing it would poison the ledger.
MAX_PREDICTED_DAILY_CENTS = 1_000_000  # $10,000/day

#: The Meter needs real outcome data, which does not exist the same day. And a
#: find nobody can check within a season is not a prediction.
MIN_VERIFY_AFTER_DAYS = 1
MAX_VERIFY_AFTER_DAYS = 180

DEFAULT_EMOJI = "💡"

#: The For You card is a decision surface, not a second rationale. These
#: bounds keep the Analyst's structured labels scan-friendly while preserving
#: the full argument and action plan in their existing fields.
FEED_BRIEF_EFFORTS = ("low", "medium", "high")
FEED_BRIEF_MAX_TAGS = 3
FEED_BRIEF_TAG_MAX_CHARS = 28
FEED_BRIEF_LABEL_MAX_CHARS = 48
FEED_BRIEF_GLANCE_MAX_CHARS = 72


class InvalidFindError(ValueError):
    """The model's proposal is not trustworthy enough to store."""


# ---------------------------------------------------------------------------
# What kind of claim is this, and what does that kind have to survive?
# ---------------------------------------------------------------------------
#
# The audit of 2026-08-03 found four of nine live finds false and one weak, and
# not one of them is removed by any similarity threshold that leaves the product
# alive — their evidence scores *well*. What separates a true find from a false
# one here is the kind of assertion it makes and whether its evidence can carry
# that kind:
#
# * `current_state` — "your delivery menu is broken", "your Apple Maps listing
#   is unclaimed". The strongest claim available, and the one find 7c4a9124 made
#   on three `page_state` rows captured 140 minutes before the shop opened.
# * `pattern` — "customers repeatedly mention the wait". Repeatedly means more
#   than one source, and a storefront plus its own mirror is one source.
# * `opportunity` — "a fixed-price lunch set would sell". Weak evidence is
#   allowed. Asserting a current state is not.
# * `listing_fact` — one verbatim, durable fact an owner can check in under a
#   minute. The cold-start tier: on night one a tenant has had one sweep, and
#   "Apple Maps shows Claim This Place" is worth showing and worth no money.
#
#: Ordered by evidential burden, strongest first, with the cold-start tier last
#: because it is reached by declaration rather than by demotion.
CLAIM_TYPES = ("current_state", "pattern", "opportunity", "listing_fact")

#: The demotion ladder. A find that cannot carry its tier drops to the next one
#: it can carry, and is only withheld when none fits. Demotion over rejection is
#: the whole design: an earlier version of these gates, measured against this
#: corpus, withheld 9 of 9 real finds, and an owner who opens the board to "no
#: recommendations" every morning churns.
CLAIM_LADDER = ("current_state", "pattern", "opportunity")

DEFAULT_CLAIM_TYPE = "opportunity"

#: How many distinct sources a `pattern` needs. Two, because one is an anecdote.
MIN_PATTERN_SOURCES = 2

#: Assertions that something the owner controls is not functioning, not
#: available, or not claimed *right now*. Deliberately narrow, and calibrated
#: against the owner-facing copy of all nine live finds: it fires on exactly two
#: of them, which is exactly the two the audit read as current-state claims.
#: A loose reading here would promote ordinary opportunity finds into the
#: strictest tier and then withhold them, which is the failure this tranche
#: exists to avoid.
#:
#: Note what is NOT here. A bare "closed" is opening-hours vocabulary — find
#: 7c4a9124's own move says "Tue closed" — and "down" only counts after a verb,
#: because find cbac2b29's move opens "Write down your true open hours".
_CURRENT_STATE_ASSERTION = re.compile(
    r"\b(?:is\s*n[o’']t|isn[’']t|is not|are not|aren[’']t|not)\s+available\b"
    r"|\bunavailable\b"
    r"|\bbroken\b"
    r"|\b(?:is|are|was|were|went|goes)\s+(?:currently\s+)?down\b"
    r"|\bnot\s+working\b"
    r"|\bstopped\s+working\b"
    r"|\bno\s+way\s+to\s+(?:order|buy|book|pay|reserve)\b"
    r"|\bcan(?:not|[’']t)\s+(?:be\s+)?(?:order|buy|book|pay|check\s*out)"
    r"|\bnot\s+accepting\b"
    r"|\b(?:unclaimed|not\s+claimed|claim\s+this\s+place)\b"
    r"|\bsold[-\s]?out\b|\bout\s+of\s+stock\b"
    r"|\b(?:temporarily|currently)\s+closed\b",
    re.IGNORECASE,
)


def asserts_current_state(text: str) -> bool:
    """Does this owner-facing copy claim something is broken or absent now?

    Run over the title, summary and move — never the rationale. The rationale is
    the operator record, and the "WAS IT OPEN?" prompt rule *requires* it to
    discuss whether the business was shut at capture time; scanning it would
    read a find's own honesty as the claim it was being honest about.
    """
    return bool(_CURRENT_STATE_ASSERTION.search(str(text or "")))


@dataclass(frozen=True)
class EvidenceFact:
    """What the Analyst knows about one cited row, beyond its text.

    ``captured_while_open`` is three-valued on purpose. ``None`` means nobody
    could tell — no business record stores a timezone and opening hours are not
    a stored field — and "we could not tell" must read as "cannot make the
    claim", or the gate is decoration.
    """

    observation_id: str
    #: What the row asserts: `page_state`, `hours`, `review`, `listing`, … See
    #: `signals.STATEMENT_TYPES`. ``None`` means nobody classified it.
    statement_type: str | None = None
    #: `provenance.source_identity`. ``None`` for a row with no usable URL.
    source_identity: str | None = None
    captured_while_open: bool | None = None


@dataclass(frozen=True)
class ClaimVerdict:
    """The tier a find landed in, and why it is not the tier it asked for."""

    requested: str
    #: The tier the find may be published as. ``None`` means no tier fits and
    #: the find must not reach the owner's board.
    tier: str | None
    #: The find asked for one tier and a standard moved it. NOT simply
    #: ``tier != requested``: the assertion scan *promotes* an opportunity whose
    #: wording claims a current state, and find cbac2b29 — "your Apple Maps
    #: profile is unclaimed", the cheapest true win in the dataset — is exactly
    #: that case. Reporting a promotion as a demotion would put the one find
    #: worth trusting in the same bucket as the four the audit called false.
    demoted: bool
    withheld: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        """One sentence for a `withheld_reason` column or an operator log."""
        return "; ".join(self.reasons)


@dataclass(frozen=True)
class FeedBrief:
    """Compact, owner-facing structure for one For You recommendation.

    The fields are deliberately descriptive rather than evidential. Impact,
    confidence, evidence count, approval policy and the verification date stay
    deterministic elsewhere in the product; the model only supplies the short
    labels that help an owner understand what kind of move this is.
    """

    effort: str
    category: str
    move_type: str
    price_point: str | None
    goal: str
    beneficiary: str
    success_signal: str
    tags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation stored in CockroachDB."""
        return {
            "effort": self.effort,
            "category": self.category,
            "move_type": self.move_type,
            "price_point": self.price_point,
            "goal": self.goal,
            "beneficiary": self.beneficiary,
            "success_signal": self.success_signal,
            "tags": list(self.tags),
        }


def _independent_sources(facts: Sequence[EvidenceFact]) -> int:
    """How many rows here can be shown to confirm each other.

    A row with no source identity does not count. Both readings of a NULL
    identity were available and neither may fall out of a comprehension: the
    retrieval backstop in `analyst.py` counts each unattributed row as its own
    source, because it measures how *broad* the prompt is. This measures
    *independence*, and a row we cannot attribute cannot be shown to be
    independent of the row beside it. Each choice is conservative about the
    thing it is protecting.
    """
    return len({fact.source_identity for fact in facts if fact.source_identity})


def _distinct_sources(facts: Sequence[EvidenceFact]) -> int:
    """How many sources these rows came from, counting the unattributable apart.

    Used by `listing_fact`, which needs *exactly one*. Here an unattributed row
    counts separately for the same conservative reason: two rows we cannot
    attribute cannot be shown to be one source, so they do not pass as one.
    """
    return len({fact.source_identity or f"row:{fact.observation_id}"
                for fact in facts})


def _current_state_supported(facts: Sequence[EvidenceFact]) -> str | None:
    """``None`` if a current-state claim stands, else why it does not.

    The precise version of a gate that, written bluntly, withheld everything.
    "A capture taken outside opening hours cannot support a current-state
    claim" is unusable: every observation in the cluster was captured before its
    tenant opened, because Radar sweeps at 06:00 and 18:00 and restaurants open
    at 11:00. What is perishable is not the capture, it is the *statement* — so
    the gate is `statement_type = page_state`, and a durable fact like Apple
    Maps' "Claim This Place" is as true at 08:07 as at 20:00.
    """
    perishable = [fact for fact in facts if fact.statement_type == "page_state"]
    if not perishable:
        return None
    if any(fact.captured_while_open is True for fact in perishable):
        return None
    unknown = all(fact.captured_while_open is None for fact in perishable)
    return (
        "the page-state rows behind this were captured when we could not "
        "confirm the business was open"
        if unknown else
        "the page-state rows behind this were captured while the business was "
        "shut, and a closed shop's storefront is not a broken one"
    )


def _listing_fact_problems(facts: Sequence[EvidenceFact]) -> list[str]:
    problems = []
    sources = _distinct_sources(facts)
    if sources != 1:
        problems.append(
            f"a listing fact is one checkable fact from one place, and this "
            f"cites {sources}"
        )
    if any(fact.statement_type == "page_state" for fact in facts):
        problems.append(
            "a listing fact has to be durable, and a page-state row is true "
            "for minutes"
        )
    return problems


def evaluate_claim(
    requested: str,
    *,
    facts: Sequence[EvidenceFact],
    owner_copy: str,
) -> ClaimVerdict:
    """Which tier this find may be published as, or none at all.

    ``owner_copy`` is the title, summary and move joined — what the owner
    actually reads. It is scanned because the declared tier is a hint, not a
    fact: without this, a model can dodge the current-state standard by
    labelling "your delivery menu is broken" an `opportunity`, and the gate
    becomes decorative.
    """
    requested = _known_claim_type(requested)
    asserted = asserts_current_state(owner_copy)
    reasons: list[str] = []

    if requested == "listing_fact":
        problems = _listing_fact_problems(facts)
        if not problems:
            # Not promoted by the assertion scan even when the fact is
            # "your listing is unclaimed". This tier is by construction a
            # *weaker* claim — one verbatim durable fact, one source, no money
            # on it — and promoting it on a word would withhold the cheapest
            # true win in the dataset.
            return ClaimVerdict(requested=requested, tier="listing_fact",
                                demoted=False, withheld=False, reasons=())
        reasons.extend(problems)
        # It asked for the weakest tier and missed. Re-judge it as what it is,
        # starting at `pattern` rather than at the strictest tier — failing a
        # weak standard is not evidence for a strong claim.
        start = "current_state" if asserted else "pattern"
    else:
        start = "current_state" if (requested == "current_state" or asserted) \
            else requested

    for tier in CLAIM_LADDER[CLAIM_LADDER.index(start):]:
        if tier == "current_state":
            problem = _current_state_supported(facts)
            if problem:
                reasons.append(problem)
                continue
        elif asserted:
            # The prose says something is broken right now. Relabelling it a
            # pattern or an opportunity would not make that sentence weaker; it
            # would only hide which standard the sentence should have met.
            reasons.append(
                "the wording asserts something is broken or unclaimed right "
                "now, which no weaker tier can honestly carry"
            )
            break
        elif tier == "pattern":
            sources = _independent_sources(facts)
            if sources < MIN_PATTERN_SOURCES:
                reasons.append(
                    f"a pattern needs {MIN_PATTERN_SOURCES} sources that can "
                    f"confirm each other, and this has {sources}"
                )
                continue
        return ClaimVerdict(requested=requested, tier=tier,
                            demoted=tier != requested and bool(reasons),
                            withheld=False, reasons=tuple(reasons))

    return ClaimVerdict(requested=requested, tier=None, demoted=False,
                        withheld=True, reasons=tuple(reasons))


def _known_claim_type(value: Any) -> str:
    """The tier the model asked for, or the weakest one.

    A missing or unrecognised label is not worth failing a night over — the
    model has already done the reasoning and the standards below judge the
    evidence, not the word. Falling back to the weakest tier is safe because
    `evaluate_claim` re-reads the owner copy anyway: a find that asserts a
    current state is judged as one whatever it called itself.
    """
    return value if value in CLAIM_TYPES else DEFAULT_CLAIM_TYPE


@dataclass(frozen=True)
class ParsedFind:
    """A validated find, ready to be written to the `find` table."""

    emoji: str
    title: str
    #: One sentence, for the card face. The rationale is the full argument and
    #: runs to a paragraph; a paragraph on a card is what made the first real
    #: deck unreadable.
    summary: str
    rationale: str
    move: str
    predicted_daily_cents: int
    confidence: float
    verify_after: date
    #: The ids the model chose to cite, in the order it wrote them. That order
    #: is not stored: `find_evidence.rank` is the row's position in what
    #: retrieval returned, looked up per id by the Analyst.
    evidence_observation_ids: tuple[str, ...]
    #: Structured copy for the owner feed. The default keeps older direct
    #: constructors (notably the Refuter tests and historical integrations)
    #: source-compatible; `parse_find` always replaces it with a brief derived
    #: from the proposal and its real measurement window.
    feed_brief: FeedBrief = field(default_factory=lambda: FeedBrief(
        effort="medium",
        category="Growth opportunity",
        move_type="Growth experiment",
        price_point=None,
        goal="Review the proposed move",
        beneficiary="Customers and the business",
        success_signal="Meter verdict after the measurement window",
        tags=("Measurable", "Owner reviewed"),
    ))
    #: The strongest rival reading of the same evidence, and why it was
    #: rejected. Optional rather than required: this is a prompt rule, not a
    #: gate, and losing a whole night's deck because a model skipped a field
    #: would trade a real deck for a procedural one. Absent means the model
    #: said nothing, which is not the same as having no alternative.
    alternative_explanation: str | None = None
    #: The tier this find may be published as, after demotion. ``None`` means no
    #: tier fits its evidence and it must not reach the owner's board.
    claim_type: str | None = DEFAULT_CLAIM_TYPE
    #: Why it is that tier and not the one the model asked for. The caller
    #: stores this; `finds.py` has no opinion about which column it lands in.
    claim: ClaimVerdict = field(
        default_factory=lambda: ClaimVerdict(
            requested=DEFAULT_CLAIM_TYPE, tier=DEFAULT_CLAIM_TYPE,
            demoted=False, withheld=False))


def repair_escapes(text: str) -> str:
    """Decode ``\\uXXXX`` sequences the model left as literal characters.

    Models occasionally double-escape their own JSON, so ``json.loads`` yields
    the seven characters ``\\u2014`` rather than an em dash. Observed in
    production, where it reached the database and rendered as garbage on screen.

    Repairing here means every consumer of a find gets clean text, rather than
    each one reimplementing the same workaround.
    """
    return _STRAY_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def clean_owner_copy(text: str) -> str:
    """Remove internal evidence identifiers from owner-facing prose."""
    cleaned = _EVIDENCE_GROUP.sub("", repair_escapes(str(text or "")))
    cleaned = _BARE_EVIDENCE_ID.sub("", cleaned)
    cleaned = re.sub(r"\(\s*\)|\[\s*\]", "", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" ,;:")


#: The card face. Long enough for a real sentence, short enough that three of
#: them fit on a deck without the owner scrolling a card to learn what it says.
SUMMARY_MAX_CHARS = 180


def owner_card_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Return a short, complete owner-facing sentence without an ellipsis.

    Older rows were hard-cut at the card limit and end in an ellipsis. When a
    useful signal clause precedes an em dash or semicolon, keep that complete
    clause instead of showing a visibly truncated thought. New Analyst runs are
    already prompted to produce a complete sentence within the limit.
    """
    cleaned = " ".join(clean_owner_copy(text).split())
    if not cleaned:
        return ""

    was_truncated = cleaned.endswith("…") or cleaned.endswith("...")
    stem = re.sub(r"(?:…|\.{3})$", "", cleaned).strip()

    if was_truncated:
        for marker in (" — ", "; ", ": "):
            index = stem.find(marker)
            if index >= 60:
                stem = stem[:index]
                break

    if len(stem) > limit:
        window = stem[:limit]
        boundary = max(window.rfind(" — "), window.rfind("; "), window.rfind(": "))
        if boundary >= 60:
            stem = window[:boundary]
        else:
            stem = window.rsplit(" ", 1)[0]

    stem = stem.rstrip(" ,;:—-")
    if stem and stem[-1] not in ".!?":
        stem += "."
    return stem


def _bounded_owner_label(value: Any, fallback: str, limit: int) -> str:
    """Clean one short owner-facing label and cap it at a word boundary."""
    text = clean_owner_copy(value) if isinstance(value, str) else ""
    text = " ".join(text.split()) or " ".join(clean_owner_copy(fallback).split())
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return shortened or text[:limit].rstrip(" ,;:—-")


def _feed_effort(raw: Any, move: str) -> str:
    value = str(raw or "").strip().lower().replace("_", " ")
    aliases = {
        "easy": "low", "light": "low", "small": "low", "tiny": "low",
        "moderate": "medium", "mid": "medium", "medium effort": "medium",
        "large": "high", "heavy": "high", "complex": "high",
    }
    value = aliases.get(value, value)
    if value in FEED_BRIEF_EFFORTS:
        return value

    lowered = move.lower()
    if any(term in lowered for term in (
        "renovat", "construction", "new location", "sign a lease",
        "hire a manager", "replace the kitchen", "build out",
    )):
        return "high"
    steps = len([part for part in re.split(r"[.;]\s+|\s+and\s+", move) if part.strip()])
    if steps >= 3 or any(term in lowered for term in (
        "train ", "schedule ", "extend hours", "launch ", "introduce ",
        "new menu", "campaign", "photograph", "update every",
    )):
        return "medium"
    return "low"


def _feed_category(*, title: str, move: str,
                   evidence_kinds: Sequence[str]) -> str:
    """Classify the move, using source kind only when the move is ambiguous.

    A recommendation supported by a trend row is not automatically a demand
    campaign. The owner is deciding what will change, so title and move intent
    outrank where Radar happened to find the evidence.
    """
    heading = title.lower()
    text = f"{title} {move}".lower()
    digital_terms = (
        "listing", "website", "photograph", "photo", "profile", "maps",
        "google business", "yelp", "order page", "publish",
    )
    menu_terms = (
        "price", "menu", "dish", "bundle", "set", "portion", "offer",
        "bento", "package",
    )
    operations_terms = (
        "staff", "schedule", "wait", "hours", "service", "prep", "shift",
    )

    # The title is the Analyst's statement of intent and avoids a long move
    # being misclassified merely because one implementation step says
    # "upload a photo" or "confirm the price".
    if any(term in heading for term in digital_terms):
        return "Digital presence"
    if any(term in heading for term in menu_terms):
        return "Menu & offerings"
    if any(term in heading for term in operations_terms):
        return "Operations"
    if any(term in text for term in digital_terms):
        return "Digital presence"
    if any(term in text for term in menu_terms):
        return "Menu & offerings"
    if any(term in text for term in operations_terms):
        return "Operations"

    kinds = {str(kind or "").lower() for kind in evidence_kinds}
    if kinds & {"rival_price", "rival_menu"}:
        return "Menu & pricing"
    if "review" in kinds:
        return "Customer experience"
    if kinds & {"trend", "social"}:
        return "Demand generation"
    if "owner_upload" in kinds:
        return "Operations"
    return "Growth opportunity"


def _feed_move_type(title: str, move: str) -> str:
    """Name the intervention, not incidental words in its checklist."""
    heading = title.lower()
    text = f"{title} {move}".lower()
    listing_terms = (
        "listing", "website", "photograph", "photo", "profile", "maps",
        "google business", "yelp", "publish",
    )
    product_terms = (
        "bundle", "set", "menu", "dish", "package", "portion", "bento",
    )
    operations_terms = (
        "staff", "schedule", "hours", "prep", "wait", "service", "shift",
    )
    pricing_actions = (
        "raise price", "lower price", "change price", "reprice", "price at",
        "set the price", "discount", "margin",
    )

    if any(term in heading for term in listing_terms):
        return "Listing improvement"
    if any(term in heading for term in product_terms):
        return "Product improvement"
    if any(term in heading for term in pricing_actions):
        return "Pricing improvement"
    if any(term in heading for term in operations_terms):
        return "Operational improvement"
    if any(term in text for term in pricing_actions):
        return "Pricing improvement"
    if any(term in text for term in product_terms):
        return "Product improvement"
    if any(term in text for term in listing_terms):
        return "Listing improvement"
    if any(term in text for term in operations_terms):
        return "Operational improvement"
    if any(term in text for term in ("promote", "marketing", "campaign")):
        return "Demand generation"
    return "Growth experiment"


def _feed_goal(category: str, move_type: str) -> str:
    """Provide a useful legacy fallback without repeating the card title."""
    if move_type == "Listing improvement" or category == "Digital presence":
        return "Make the offer easier to discover"
    if move_type == "Product improvement" or category == "Menu & offerings":
        return "Improve customer value and repeat orders"
    if move_type == "Pricing improvement" or category == "Menu & pricing":
        return "Improve value perception and conversion"
    if move_type == "Operational improvement" or category == "Operations":
        return "Improve service speed and consistency"
    if move_type == "Demand generation" or category == "Demand generation":
        return "Increase qualified customer demand"
    if category == "Customer experience":
        return "Improve the customer experience"
    return "Test a measurable growth opportunity"


_PRICE_POINT = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")


def _feed_price_point(
    raw: Any, *, title: str, summary: str, move: str, infer: bool,
) -> str | None:
    """Return an explicit price label, never a model-invented one.

    A present-but-empty `price_point` is an intentional "not specified" from
    the Analyst and must stay empty. Inference is reserved for legacy rows that
    predate the object entirely; even then it only copies a currency value or
    the exact phrase "no price change" from owner-facing source fields.
    """
    if isinstance(raw, str):
        if raw.strip():
            return _bounded_owner_label(raw, "", FEED_BRIEF_LABEL_MAX_CHARS) or None
        return None
    if not infer:
        return None
    text = " ".join((title, move, summary))
    match = _PRICE_POINT.search(text)
    if match:
        return match.group(0).replace(" ", "")
    if "no price change" in text.lower():
        return "No price change"
    return None


def _feed_beneficiary(*, title: str, summary: str, move: str,
                      evidence_kinds: Sequence[str]) -> str:
    text = f"{title} {summary} {move}".lower()
    if any(term in text for term in ("takeout", "delivery", "pickup")):
        return "Takeout customers"
    if any(term in text for term in ("lunch", "dinner", "breakfast")):
        return "Meal-period customers"
    if "review" in {str(kind or "").lower() for kind in evidence_kinds}:
        return "Customers"
    return "Customers and the business"


def _feed_tags(raw: Any, *, category: str, move_type: str,
               beneficiary: str) -> tuple[str, ...]:
    """Clean model tags without silently changing a valid two-tag brief."""
    tags: list[str] = []
    seen: set[str] = set()

    def add(candidate: Any) -> None:
        if not isinstance(candidate, str) or len(tags) >= FEED_BRIEF_MAX_TAGS:
            return
        tag = _bounded_owner_label(candidate, "", FEED_BRIEF_TAG_MAX_CHARS)
        key = tag.casefold()
        if not tag or key in seen:
            return
        seen.add(key)
        tags.append(tag)

    if isinstance(raw, (list, tuple)):
        for candidate in raw:
            add(candidate)
        # Two specific model labels already satisfy the contract. Adding a
        # generic category here makes a carefully authored brief noisier and
        # creates a static/live mismatch for no owner benefit.
        if len(tags) >= 2:
            return tuple(tags)

    for candidate in (category, move_type, beneficiary):
        add(candidate)
        if len(tags) >= 2:
            break
    for fallback in ("Measurable", "Owner reviewed"):
        add(fallback)
        if len(tags) >= 2:
            break
    return tuple(tags)


def normalise_feed_brief(
    value: Any,
    *,
    title: str,
    summary: str,
    move: str,
    verify_after_days: int,
    evidence_kinds: Sequence[str] = (),
) -> FeedBrief:
    """Return a complete, bounded brief from model JSON or legacy find copy.

    The model is prompted and schema-bound to produce this object, but the
    parser deliberately does not turn presentation metadata into a rejection
    gate. A malformed brief is repaired from the trusted title, move,
    verification window and evidence kinds; it never fabricates a price.
    """
    raw = value if isinstance(value, Mapping) else {}
    category_fallback = _feed_category(
        title=title, move=move, evidence_kinds=evidence_kinds,
    )
    category = _bounded_owner_label(
        raw.get("category"), category_fallback, FEED_BRIEF_LABEL_MAX_CHARS,
    )
    move_type = _bounded_owner_label(
        raw.get("move_type", raw.get("moveType")),
        _feed_move_type(title, move), FEED_BRIEF_LABEL_MAX_CHARS,
    )
    beneficiary = _bounded_owner_label(
        raw.get("beneficiary"),
        _feed_beneficiary(
            title=title, summary=summary, move=move,
            evidence_kinds=evidence_kinds,
        ),
        FEED_BRIEF_GLANCE_MAX_CHARS,
    )
    goal = _bounded_owner_label(
        raw.get("goal"), _feed_goal(category, move_type),
        FEED_BRIEF_GLANCE_MAX_CHARS,
    )
    success_signal = _bounded_owner_label(
        raw.get("success_signal", raw.get("successSignal")),
        f"Meter verdict after {max(1, int(verify_after_days))} days",
        FEED_BRIEF_GLANCE_MAX_CHARS,
    )
    return FeedBrief(
        effort=_feed_effort(raw.get("effort"), move),
        category=category,
        move_type=move_type,
        price_point=_feed_price_point(
            raw.get("price_point", raw.get("pricePoint")),
            title=title, summary=summary, move=move,
            infer=not bool(raw),
        ),
        goal=goal,
        beneficiary=beneficiary,
        success_signal=success_signal,
        tags=_feed_tags(
            raw.get("tags"), category=category, move_type=move_type,
            beneficiary=beneficiary,
        ),
    )


def feed_brief_for_display(
    value: Any,
    *,
    title: str,
    summary: str,
    move: str,
    verify_after_days: int,
    evidence_kinds: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the stable camelCase contract consumed by the owner feed.

    Static builds and the live workflow endpoint intentionally call the same
    normaliser so an old CockroachDB row cannot look different after the first
    refresh. Only descriptive labels come from the model. Revenue, approval,
    evidence and dates remain deterministic fields elsewhere in the response.
    """
    brief = normalise_feed_brief(
        value,
        title=title,
        summary=summary,
        move=move,
        verify_after_days=verify_after_days,
        evidence_kinds=evidence_kinds,
    )
    return {
        "effort": brief.effort,
        "category": brief.category,
        "moveType": brief.move_type,
        "pricePoint": brief.price_point,
        "goal": brief.goal,
        "beneficiary": brief.beneficiary,
        "successSignal": brief.success_signal,
        "tags": list(brief.tags),
    }


def _require_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidFindError(
            f"{field} must be a non-empty string, got {value!r}"
        )
    return repair_escapes(value.strip())


def _summary(payload: Mapping[str, Any], rationale: str) -> str:
    """One sentence for the card, from the model or from the rationale.

    Optional rather than required: a row written before this field existed, or
    a model that skips it, must still render a card rather than fail the night.
    Falling back to the rationale's first sentence is what the page would have
    had to do anyway, done once here instead of in every renderer.
    """
    value = payload.get("summary")
    text = value.strip() if isinstance(value, str) and value.strip() else ""
    if not text:
        # First sentence of the argument. Not perfect, but it is the model's
        # own opening line and reads as a summary far more often than not.
        first, _, _ = rationale.partition(". ")
        text = first.strip()
        if text and not text.endswith("."):
            text += "."

    return owner_card_summary(text)


def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
    """A field the ledger can live without, cleaned the same way as the rest.

    Blank collapses to absence: an empty string in the column would read as
    "the model considered the alternative and had nothing to say", which is a
    claim, and a stronger one than the model made.
    """
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return repair_escapes(value.strip())


def _require_cents(payload: Mapping[str, Any]) -> int:
    field = "predicted_daily_cents"
    value = payload.get(field)

    # bool is an int subclass, and True is never a cent amount.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFindError(
            f"{field} must be a number of integer cents, got {value!r}. "
            "A string here usually means the model ignored the output schema."
        )

    if isinstance(value, float):
        # 2300.0 is unambiguous; 23.5 cents is not a real amount.
        if not value.is_integer():
            raise InvalidFindError(
                f"{field} must be whole cents, got {value!r}"
            )
        value = int(value)

    if value < 0:
        raise InvalidFindError(f"{field} must be non-negative, got {value}")
    if value > MAX_PREDICTED_DAILY_CENTS:
        raise InvalidFindError(
            f"{field}={value} is implausible (ceiling "
            f"{MAX_PREDICTED_DAILY_CENTS}). Likely a unit confusion — dollars "
            "reported as cents, or a monthly figure reported as daily."
        )
    return value


def _require_confidence(payload: Mapping[str, Any]) -> float:
    value = payload.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFindError(f"confidence must be a number, got {value!r}")
    if not 0.0 <= value <= 1.0:
        raise InvalidFindError(
            f"confidence must be within [0.0, 1.0], got {value}. A value like "
            "82 means the model emitted a percentage."
        )
    return float(value)


def _require_verify_after(payload: Mapping[str, Any], today: date) -> date:
    field = "verify_after_days"
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidFindError(f"{field} must be an integer, got {value!r}")
    if not MIN_VERIFY_AFTER_DAYS <= value <= MAX_VERIFY_AFTER_DAYS:
        raise InvalidFindError(
            f"{field} must be within "
            f"[{MIN_VERIFY_AFTER_DAYS}, {MAX_VERIFY_AFTER_DAYS}], got {value}"
        )
    return today + timedelta(days=value)


def _require_evidence(
    payload: Mapping[str, Any], known: Collection[str]
) -> tuple[str, ...]:
    field = "evidence_observation_ids"
    raw = payload.get(field)
    if not isinstance(raw, (list, tuple)):
        raise InvalidFindError(f"{field} must be a list, got {raw!r}")

    known_set = set(known)
    seen: list[str] = []
    for obs_id in raw:
        if not isinstance(obs_id, str):
            raise InvalidFindError(f"{field} entries must be strings, got {obs_id!r}")
        if obs_id not in known_set:
            raise InvalidFindError(
                f"{field} cites {obs_id!r}, which was not retrieved for this "
                "find. Evidence must come from the vector search results — a "
                "citation outside them is a hallucination."
            )
        if obs_id not in seen:
            seen.append(obs_id)

    if not seen:
        raise InvalidFindError(
            f"{field} must cite at least one retrieved observation. A find with "
            "no evidence cannot be defended."
        )
    return tuple(seen)


def parse_find(
    payload: Mapping[str, Any],
    *,
    today: date,
    known_observation_ids: Collection[str],
    evidence_facts: Mapping[str, EvidenceFact] | None = None,
) -> ParsedFind:
    """Validate a model-proposed find and decide what kind of claim it may make.

    Args:
        payload: The model's structured output.
        today: The run date. Injected rather than read from a clock so the
            verify window is deterministic.
        known_observation_ids: Observation IDs the vector search actually
            returned. Evidence citations must fall within this set.
        evidence_facts: What the Analyst knows about each retrieved row —
            statement type, source identity, and whether the business was open
            when it was captured. Omitted by every caller that predates the
            claim standards; without it the find keeps the tier it asked for,
            because a standard cannot be enforced against data nobody supplied.

    Raises:
        InvalidFindError: on any field the ledger cannot safely inherit. A find
            that fails a *claim* standard is not an error — it comes back with a
            weaker tier, or with ``claim.withheld``. Raising would cost the
            whole night's deck for one bad card, which is how a gate ends up
            withholding 9 of 9.
    """
    emoji = payload.get("emoji")
    if not isinstance(emoji, str) or not emoji.strip():
        emoji = DEFAULT_EMOJI

    rationale = _require_text(payload, "rationale")
    title = _require_text(payload, "title")
    summary = _summary(payload, rationale)
    move = _require_text(payload, "move")
    cents = _require_cents(payload)
    cited = _require_evidence(payload, known_observation_ids)
    verify_after = _require_verify_after(payload, today)
    feed_brief = normalise_feed_brief(
        payload.get("feed_brief"),
        title=title,
        summary=summary,
        move=move,
        verify_after_days=(verify_after - today).days,
    )

    requested = _known_claim_type(payload.get("claim_type"))
    if evidence_facts is None:
        verdict = ClaimVerdict(requested=requested, tier=requested,
                               demoted=False, withheld=False)
    else:
        verdict = evaluate_claim(
            requested,
            facts=[evidence_facts[observation_id] for observation_id in cited
                   if observation_id in evidence_facts],
            owner_copy="\n".join((title, summary, move)),
        )

    if verdict.tier == "listing_fact" and cents:
        # A single checkable fact is worth showing and worth no money. Zeroed
        # rather than rejected: this is the cold-start tier, and refusing the
        # find because the model priced it would leave a night-one tenant with
        # an empty board when it has one honest thing to say.
        cents = 0
        verdict = ClaimVerdict(
            requested=verdict.requested, tier=verdict.tier,
            demoted=verdict.demoted, withheld=verdict.withheld,
            reasons=verdict.reasons + (
                "a listing fact carries no revenue figure, so its prediction "
                "was cleared",))

    return ParsedFind(
        emoji=emoji.strip(),
        title=title,
        summary=summary,
        rationale=rationale,
        move=move,
        feed_brief=feed_brief,
        predicted_daily_cents=cents,
        confidence=_require_confidence(payload),
        verify_after=verify_after,
        evidence_observation_ids=cited,
        alternative_explanation=_optional_text(payload, "alternative_explanation"),
        claim_type=verdict.tier,
        claim=verdict,
    )


#: What the owner is offered when reporting a figure, and how each reads in a
#: sentence. Kept beside the find payload rather than in the page, because the
#: label is part of what the ledger will claim: "$210 a week" measured against
#: a daily prediction is a statement about somebody's money.
BASIS_LABEL = {"day": "a day", "week": "a week", "month": "a month"}


def reported_outcome_view(raw: Any, *, money: Any) -> dict[str, Any] | None:
    """The owner's latest reported figure, shaped for the board.

    `None` when nobody has reported anything, which is not the same as zero: a
    reported zero is a measured miss and has to survive as a number all the way
    to the screen.

    Formatting is done here, once, by the caller's money formatter — the page
    is not allowed to do arithmetic on money, and both the static build and the
    live snapshot have to agree on the string.
    """
    if not isinstance(raw, Mapping):
        return None
    amount = raw.get("amount_cents")
    if amount is None:
        return None

    amount = int(amount)
    basis = str(raw.get("basis") or "day")
    daily = int(raw.get("daily_cents") or 0)
    reported_at = raw.get("reported_at")
    return {
        "amountCents": amount,
        "amountTxt": money(amount),
        "basis": basis,
        "basisLabel": BASIS_LABEL.get(basis, basis),
        "dailyCents": daily,
        "dailyTxt": money(daily),
        "note": (raw.get("note") or None),
        "reportedAt": (reported_at.isoformat()
                       if hasattr(reported_at, "isoformat") else reported_at),
    }
