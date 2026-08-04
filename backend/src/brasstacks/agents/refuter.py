"""The Refuter — one model call whose only job is to prove tonight's finds wrong.

Everything upstream of this is deterministic: a similarity bar, a per-source
cap, a capture-time gate, a claim tier. Measured against the nine finds live on
2026-08-03, those mechanisms remove the structural failures and none of the
factual ones, because the factual ones are ordinary nouns:

* **9bc53e94** told Asaka to photograph its lunch bento. The row it cited
  describes a bento photo already on the listing.
* **818fdb2d** told Yellow Cow to launch a fixed-price weekday lunch set. Three
  uncited rows of that tenant's memory name the Dosirak set meal as its
  best-selling delivery item.
* **f26662ea** priced a takeout push against a competitor set that appears in no
  observation anyone made.
* **8b4009e5** priced a group package off a 2018 newspaper article.

"a bento photo", "Dosirak", "chicken fingers, burgers", "2018". No extractor
flags those and no threshold deletes them. What catches them is reading the find
beside the rows it cites and asking whether the sentence survives — which is a
model's job, done once per night, with the framing inverted.

**Refute, do not review.** The framing is the mechanism, not a prompt flourish.
"Review this find" produces balanced prose that publishes everything, because a
model asked to review will always find something good to say. "Prove this find
wrong, and name the row that does it" produces either a citation or nothing, and
nothing is a publish.

**It fails open, always.** This is the highest-failure-rate component in the
system: one network call, one model, one JSON parse, on a nightly schedule with
nobody watching. The failure this entire line of work exists to prevent is an
owner opening the board to nothing — an earlier design of the deterministic
gates withheld 9 of 9 and would have been a dead product. A checker outage that
blanked the board would be that same dead product arriving by another route. So
an error, a timeout, or a body we cannot read publishes the deterministic
survivors and takes the dollar figure off them, and the run receipt says the
check did not run.

**A withhold has to quote.** A verdict of `withhold` that cannot name an
observation the refuter was shown is downgraded to `demote`. A model that can
withhold on a feeling is one uneasy night away from an empty board; requiring
the row makes the adversary produce evidence rather than doubt, and a demotion
still takes the money off the card.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from typing import Any

from brasstacks.business_state import describe_business_state
from brasstacks.finds import EvidenceFact, ParsedFind
from brasstacks.provenance import source_host
from brasstacks.repository import Retrieved

#: What a verdict may say. `publish` is the default and the safe answer;
#: `demote` keeps the move on the board with no revenue figure; `withhold` keeps
#: it off the board entirely and is the only one that requires a citation.
REFUTER_ACTIONS = ("publish", "demote", "withhold")

PUBLISH, DEMOTE, WITHHOLD = REFUTER_ACTIONS

#: Output is three short verdicts. Nothing like the Analyst's 16k is needed, and
#: a smaller ceiling is one fewer second on a call already budgeted at 40–70.
REFUTATION_MAX_TOKENS = 2048

#: What a find carries when nobody actually checked it. Written as an admission
#: rather than a judgement: the move is unchanged and the pipeline simply could
#: not get an adversary to look at it tonight.
NOT_CHECKED_REASON = (
    "the adversarial check could not run tonight, so this move is shown "
    "without a revenue figure until it has been challenged"
)

REFUTATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    # No `enum`, matching FIND_SCHEMA in analyst.py. The
                    # structured-output endpoint has already rejected keywords
                    # on that schema, and a 400 here costs the whole check —
                    # which, failing open, silently costs every find its price.
                    # The vocabulary is stated in the system prompt and an
                    # unrecognised value is treated as "unchecked" below.
                    "action": {"type": "string"},
                    "reason": {"type": "string"},
                    "contradicted_by": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["index", "action", "reason", "contradicted_by"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

REFUTER_SYSTEM_PROMPT = """You are the Refuter for Brass Tacks. Somebody else \
wrote these recommendations. Your job is to try to prove them WRONG.

You are not reviewing them. You are not improving them, balancing their \
strengths against their weaknesses, or suggesting what would make them better. \
You are the sceptical owner who has been sold a bad idea before, reading each \
move beside the exact evidence it rests on, looking for the sentence that does \
not survive contact with that evidence.

These are the mistakes that have actually reached owners, so look for these \
first:
- A SPECIFIC NOBODY OBSERVED. A dish, a price, a competitor, a rating, a date, \
an opening hour, a platform ranking named in the move or the rationale that \
appears in NONE of the rows cited under that find. One published move compared \
a takeout menu against a competitor set that exists in no observation anyone \
ever made.
- A CLAIM THE EVIDENCE CONTRADICTS. The move says something is missing, absent \
or not there, and one of its own cited rows shows it present. One published \
move told an owner to photograph a lunch bento; the row it cited describes the \
bento photo already on the listing.
- SOMETHING THEY ALREADY SELL. Read the "what this business already has" \
block. A move that launches, adds, introduces or starts something already in \
that list is wrong as written. One published move told an owner to launch a \
fixed-price weekday lunch set while their own menu listed a set meal as their \
best-selling delivery item.
- A NUMBER FROM THE WRONG PLACE. A revenue figure or a price traced to a source \
that is years old, is about a different business, or is not in the evidence at \
all. One published move priced a group package off a 2018 newspaper article.
- SOMEBODY ELSE'S PAGE. A move that criticises or takes credit for a page on a \
domain the owner did not declare, as though it were theirs.
- ONE DOCUMENT COUNTED TWICE. Rows sharing a source and a capture time are one \
page read once. A move that presents them as several confirmations, several \
platforms, or a pattern is overstating what it has.

What is NOT a refutation, and this matters as much as the list above:
- CAPTURE TIME, except on a page_state row. Every row carries the moment the \
page was read, and almost every page in this system is read outside trading \
hours — the sweep runs early and restaurants open late. That says nothing about \
a review, a price, a menu, an opening-hours table or a listing fact, all of \
which are as true at 08:00 as at 20:00. A row is only vulnerable to when it was \
captured when it describes a page's momentary state, and those rows say so.
- WISHING FOR BETTER EVIDENCE. Thin support is not a false claim. A move backed \
by one honest row is a publish.
- DISAGREEING WITH THE JUDGEMENT. Whether this is the best use of the owner's \
week is her call, not yours. You are checking whether the sentences are true.

Return exactly one verdict per find, using its index. Each verdict is one of:
- `publish`: you tried to refute it and could not. This is the DEFAULT and it \
is the right answer most of the time. Not being fully convinced is a publish. \
Wishing the evidence were stronger is a publish. Publish anything you cannot \
actually fault.
- `demote`: the core of the move survives, but a specific it names is not \
carried by the evidence, or the revenue figure rests on something that is not \
there. The owner still sees the move; the dollar figure comes off it.
- `withhold`: you proved a load-bearing claim false. The owner does not see \
this one at all. To withhold you MUST put at least one observation id in \
`contradicted_by` — an id from that find's own cited evidence, or from the \
"what this business already has" block — and `reason` must say what that row \
says that the move cannot survive. A withhold that names no row is treated as a \
demotion, because doubt is not refutation.

`reason` is one sentence, written for the business owner, naming the specific \
thing that is wrong. "The competitor prices in this move appear in none of the \
rows it cites" is a reason. "The evidence is weak" is not.

Withholding is expensive. An owner who opens an empty board every morning \
stops opening it, and every find you withhold is a morning with less on it. \
Withhold when you have the row in your hand, demote when the move is bigger \
than its evidence, and publish when you simply could not fault it."""


@dataclass(frozen=True)
class Refutation:
    """What the adversary managed to prove about one move.

    ``checked`` is the honest half. ``publish`` after a real attempt and
    ``publish`` because nobody could look are the same action and different
    facts, and only the second one costs the find its price.
    """

    index: int
    action: str
    reason: str
    contradicted_by: tuple[str, ...] = ()
    #: False when this find was never actually challenged — the call failed, or
    #: came back without a verdict this find could use.
    checked: bool = True

    @property
    def withheld(self) -> bool:
        return self.action == WITHHOLD

    @property
    def money_allowed(self) -> bool:
        """May this find carry its predicted daily figure to the owner?

        Only a find an adversary actually read and could not fault. A demotion
        loses it because a specific it named was unsupported; an unchecked find
        loses it because nothing stood between the model's number and the owner.
        """
        return self.checked and self.action == PUBLISH


@dataclass(frozen=True)
class RefutationResult:
    """Every verdict of one night, and whether the check ran at all."""

    verdicts: tuple[Refutation, ...] = ()
    #: False when the call errored, timed out, or returned a body we could not
    #: read. The night still publishes; the receipt says the check did not run.
    checked: bool = True
    error: str | None = None

    def for_index(self, index: int) -> Refutation:
        """The verdict for one find, or the fail-open verdict.

        Never raises and never returns ``None``. A caller reaching for a verdict
        that is not there is exactly the case where the board must not go dark,
        so an absent verdict is an unchecked publish like any other.
        """
        for found in self.verdicts:
            if found.index == index:
                return found
        return _unchecked(index)


def _unchecked(index: int) -> Refutation:
    return Refutation(index=index, action=PUBLISH, reason=NOT_CHECKED_REASON,
                      checked=False)


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def _stamp(moment) -> str:
    if moment is None:
        return "capture time unknown"
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _openness(fact: EvidenceFact | None) -> str:
    """Whether the shop was trading when this page was read, where that matters.

    Only for a `page_state` row, and the restriction is the whole point.
    Measured against the live cluster on 2026-08-04: all 51 rows cited by the
    nine live finds answer "the business was shut", because Radar sweeps at
    06:00 and 18:00 and restaurants open at 11:00. Printed on every row that is
    a lever an adversary can pull to refute all nine — the same trap that made
    the blunt version of the deterministic gate withhold 9 of 9. What is
    perishable is the *statement*, not the capture: a review, a price and a menu
    are as true at 08:00 as at 20:00, and only a page's momentary state is not.

    Three-valued where it does apply, because "we could not tell" is the answer
    for any tenant this system cannot place in a timezone, and flattening that
    into "open" would hand the refuter a false alibi for a shop that was shut.
    """
    if fact is None or fact.statement_type != "page_state":
        return ""
    if fact.captured_while_open is None:
        return ", we could not tell whether the business was open then"
    return (", captured while the business was open"
            if fact.captured_while_open
            else ", captured while the business was shut")


def _evidence_line(hit: Retrieved, fact: EvidenceFact | None) -> str:
    """One cited row: what it is, where it came from, when, and its full text.

    Untruncated, deliberately. This call exists to notice that a find's own
    evidence contradicts it, and the contradicting phrase is as likely to be in
    the middle of a long listing row as at the front of it. A shorter prompt
    would be blind to the thing it was built to see.
    """
    subject = f" about {hit.subject}" if hit.subject else ""
    host = source_host(hit.source_url) or hit.source_name or "no source recorded"
    statement = (f", says a {fact.statement_type}"
                 if fact is not None and fact.statement_type else "")
    return (f"  - id={hit.observation_id} [{hit.kind}{subject}, {host}, "
            f"{_stamp(hit.observed_at)}{statement}{_openness(fact)}]\n"
            f"    {hit.content}")


def build_refutation_prompt(
    *,
    finds: Sequence[ParsedFind],
    retrieved: Mapping[str, Retrieved],
    evidence_facts: Mapping[str, EvidenceFact] | None = None,
    business: Mapping[str, Any] | None = None,
    business_state: Any = None,
) -> str:
    """What the adversary is shown: the moves, and only what they cite.

    Not the whole retrieved set. A row the find did not cite is a row the
    refuter could use to argue *for* the move, and helping the find is the one
    thing this call must not do. The exception is the "what this business
    already has" block, which is not market opinion — it is what the tenant
    already is, and it is the only way to catch a move that launches something
    already on the menu.
    """
    facts = evidence_facts or {}
    name = (business or {}).get("name") if business else None

    lines = [f"Business: {name}" if name else "Business: this owner's business"]

    inventory = describe_business_state(business_state) if business_state else ""
    if inventory:
        lines.append("")
        lines.append(inventory)

    lines.append(
        f"\n{len(finds)} move(s) were proposed tonight. Refute each one."
    )
    for index, proposal in enumerate(finds):
        lines.append(f"\n--- index {index} ---")
        lines.append(f"Title: {proposal.title}")
        lines.append(f"Summary: {proposal.summary}")
        lines.append(f"Move: {proposal.move}")
        lines.append(f"Rationale: {proposal.rationale}")
        lines.append(
            f"Claims it is worth: {proposal.predicted_daily_cents} cents per "
            f"day (claim type: {proposal.claim_type})"
        )
        cited = [retrieved[observation_id]
                 for observation_id in proposal.evidence_observation_ids
                 if observation_id in retrieved]
        if cited:
            lines.append(f"Everything it cites ({len(cited)} row(s)):")
            lines.extend(_evidence_line(hit, facts.get(hit.observation_id))
                         for hit in cited)
        else:
            # Nothing to read it against. Said plainly rather than left as an
            # empty heading, because "no rows shown" and "no rows exist" would
            # otherwise look the same to the model.
            lines.append("Everything it cites: no rows were available to show.")

    lines.append(
        "\nReturn one verdict per index. Times above are UTC. A row is only "
        "evidence for the find it is listed under."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _quotable_ids(finds: Sequence[ParsedFind], business_state: Any) -> dict[int, set[str]]:
    """Which observation ids each find's verdict may name to justify a withhold.

    Its own cited rows, plus the ids printed in the "already has" block. The
    second half is load-bearing for the 818fdb2d shape: what refutes that find
    is a row it never cited, because it never looked — but those ids are in the
    block the refuter was given, so naming one is naming something it can see.
    """
    inventory: set[str] = set()
    for offer in getattr(business_state, "offers", ()) or ():
        inventory.update(getattr(offer, "observation_ids", ()) or ())
    return {index: set(proposal.evidence_observation_ids) | inventory
            for index, proposal in enumerate(finds)}


def _read_verdict(item: Any, allowed: dict[int, set[str]]) -> Refutation | None:
    """One verdict from the model, or ``None`` if it names no find we proposed.

    Everything questionable resolves toward publishing. An action we do not
    recognise is not a reason to withhold something, and it is not a reason to
    silently price something either — it comes back as an unchecked publish.
    """
    if not isinstance(item, Mapping):
        return None
    index = item.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if index not in allowed:
        return None

    action = item.get("action")
    if action not in REFUTER_ACTIONS:
        return _unchecked(index)

    reason = item.get("reason")
    reason = reason.strip() if isinstance(reason, str) and reason.strip() else ""

    raw = item.get("contradicted_by")
    named = tuple(
        value for value in (raw if isinstance(raw, (list, tuple)) else ())
        if isinstance(value, str) and value in allowed[index]
    )

    if action == WITHHOLD and not named:
        # Prove it or price it. A withhold that cannot name the row it read is
        # doubt, and doubt already has an action: the move stays on the board
        # and the dollar figure comes off it.
        action = DEMOTE
        reason = (reason or "the adversarial check could not stand this up") + (
            " (recorded as a demotion: no row was named that contradicts it)")

    return Refutation(index=index, action=action,
                      reason=reason or "no reason given",
                      contradicted_by=named)


def refute_finds(
    *,
    reasoner: Any,
    finds: Sequence[ParsedFind],
    retrieved: Mapping[str, Retrieved],
    evidence_facts: Mapping[str, EvidenceFact] | None = None,
    business: Mapping[str, Any] | None = None,
    business_state: Any = None,
    max_tokens: int = REFUTATION_MAX_TOKENS,
) -> RefutationResult:
    """Try to prove tonight's moves wrong. One call, whatever the deck size.

    Never raises. Every failure path — provider error, timeout, refusal,
    malformed body, a verdict list that names nothing we proposed — returns a
    result with ``checked=False``, which publishes the deck without its prices.
    The caller has already paid for retrieval and reasoning by the time this
    runs, and nothing an advisory check can do is worth throwing that away.

    ``reasoner`` is the ``Reasoner`` protocol, never an SDK client. Reasoning
    runs on the Anthropic API because Bedrock cannot grant this account a
    current Claude model, and that has to stay a config value.
    """
    finds = list(finds)
    if not finds:
        return RefutationResult(verdicts=(), checked=True)

    try:
        # Inside the try, not before it. Assembling the prompt reads
        # business_state, parses URLs and formats capture times, and any of
        # those can raise on a shape nobody anticipated. Outside the try that
        # exception propagated out of run_analyst: the run went to 'failed',
        # nothing was stored, and the board went empty — the exact outcome this
        # module exists to prevent, arriving through the module built to
        # prevent it. An advisory check that can end a night is not advisory.
        prompt = build_refutation_prompt(
            finds=finds, retrieved=retrieved, evidence_facts=evidence_facts,
            business=business, business_state=business_state,
        )
        payload = reasoner.complete_json(
            system=REFUTER_SYSTEM_PROMPT, user=prompt,
            schema=REFUTATION_SCHEMA, max_tokens=max_tokens,
        )
    except Exception as e:  # noqa: BLE001 — see the docstring; this is the point
        return RefutationResult(
            verdicts=tuple(_unchecked(index) for index in range(len(finds))),
            checked=False,
            error=f"{type(e).__name__}: {e}",
        )

    raw = payload.get("verdicts") if isinstance(payload, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return RefutationResult(
            verdicts=tuple(_unchecked(index) for index in range(len(finds))),
            checked=False,
            error=f"expected a list of verdicts, got {type(raw).__name__}",
        )

    allowed = _quotable_ids(finds, business_state)
    seen: dict[int, Refutation] = {}
    for item in raw:
        parsed = _read_verdict(item, allowed)
        # First verdict per index wins. A model that answers twice for one find
        # has contradicted itself, and taking the later answer would let a
        # trailing "withhold" quietly overwrite a considered "publish".
        if parsed is not None and parsed.index not in seen:
            seen[parsed.index] = parsed

    verdicts = tuple(seen[index] if index in seen else _unchecked(index)
                     for index in range(len(finds)))
    # A body that parsed but produced no verdict we could read is an outage with
    # better manners, and it is reported as one — `seen` is not the test,
    # because an unrecognised action lands in `seen` as an *unchecked* verdict.
    # A receipt saying "ok" here would tell an operator the deck was challenged
    # when nothing had looked at it.
    checked = any(found.checked for found in seen.values())
    return RefutationResult(
        verdicts=verdicts,
        checked=checked,
        error=None if checked else "no verdict named a find proposed tonight",
    )


__all__ = [
    "NOT_CHECKED_REASON", "REFUTATION_MAX_TOKENS", "REFUTATION_SCHEMA",
    "REFUTER_ACTIONS", "REFUTER_SYSTEM_PROMPT", "Refutation",
    "RefutationResult", "build_refutation_prompt", "refute_finds",
]
