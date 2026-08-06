"""The Coster — one model call that prices what a move will cost the owner.

`predicted_daily_cents` is **gross revenue**, and until this module existed it
was the only money in the system. A find could tell an owner a move was worth
+$23/day while it burned $400 of ingredients and six hours of labour, and
nothing in the pipeline noticed. Revenue without cost is not a recommendation,
it is a sales pitch.

**It never sees the revenue figure.** This is the whole reason it is a separate
call rather than four more fields on `FIND_SCHEMA`. A model shown "this is
worth 2300c/day" and asked what it costs produces a number that makes the move
look worth it — the anchor does the reasoning. `build_costing_prompt` is a pure
function of everything except what the move is worth, and a test asserts that
by building the same prompt across five revenue figures and comparing bytes.
Confidence and claim tier are withheld for the same reason: both are the
Analyst's assessment of its own find, and a coster told how much to believe the
thing it is pricing is not independent of it.

**It fails to UNKNOWN, never to zero.** The Refuter can fail open by publishing
a find with its price stripped, because a move with no dollar figure is still an
honest move. A coster that failed to zero would print "costs nothing" on every
card in the deck during an outage, and free is the single most persuasive thing
you can say about a bad idea. So an error, a malformed body, or an estimate the
parser could not stand behind produces no estimate at all: `has_estimate` is
False, `as_dict()` is None, and the column stays NULL. Absent and free are
different facts and the database must be able to tell them apart.

**A cost is a prediction too.** Nothing measures it yet — the Meter judges
revenue against observed outcomes and has no path to a receipt. So every
estimate carries `measured=False`, every line records whether any cited row
actually backs it, and none of this may reach a verified figure. When an owner
confirms real spend on a completed Maker task, that is the measurement, and
this is the field it will correct.

**Payback is computed here, in Python, from both agents.** `payback_days()` is
ordinary arithmetic over the Analyst's revenue and this agent's costs; no model
is ever asked for it. That separation is what makes it trustworthy, and it
exposes something already true of the live deck: a $900 setup against +$23/day
takes 40 days to break even while `verify_after_days` defaults to 14, so the
Meter would stamp the find VERIFIED 26 days before the owner was square.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any

from brasstacks.finds import ParsedFind
from brasstacks.provenance import source_host
from brasstacks.repository import Retrieved

#: The two shapes a cost comes in, and they must not be collapsed. Revenue in
#: this system is per-day; costs are not. A $900 menu reprint folded into a
#: daily figure reads as a permanent drain, and a daily wage folded into setup
#: reads as survivable. Payback needs both kinds separately or it is fiction.
COST_KINDS = ("setup", "daily")

SETUP, DAILY = COST_KINDS

#: Output is a handful of short priced lines. The Analyst's 16k ceiling would
#: buy nothing here but latency on a call already sharing a night's budget.
COSTING_MAX_TOKENS = 2048

#: What a find carries when nobody priced it. Written as an admission, matching
#: the Refuter's NOT_CHECKED_REASON: the move is unchanged and the pipeline
#: simply could not get a costing for it tonight.
NOT_COSTED_REASON = (
    "the costing pass could not run tonight, so this move is shown without an "
    "estimate of what it will cost"
)

COSTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "estimates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    # One sentence for the owner about where these numbers come
                    # from. Required, because a total nobody can argue with is a
                    # total nobody can correct.
                    "basis": {"type": "string"},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "cents": {"type": "integer"},
                                # No `enum`, matching FIND_SCHEMA and
                                # REFUTATION_SCHEMA. The structured-output
                                # endpoint has rejected keywords on that schema
                                # before, and a 400 here costs every card in the
                                # deck its costing. The vocabulary is stated in
                                # the prompt and enforced on read.
                                "kind": {"type": "string"},
                                "cited_observation_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["label", "cents", "kind",
                                         "cited_observation_ids"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["index", "basis", "lines"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["estimates"],
    "additionalProperties": False,
}

COSTER_SYSTEM_PROMPT = """You are the Coster for Brass Tacks. Somebody else \
proposed these moves to a small-business owner. Your only job is to say what \
each one will COST that owner to carry out.

You are not judging whether a move is a good idea, and you are deliberately not \
being told what anyone thinks it will earn. Do not estimate revenue, do not \
say whether a move is worth it, and do not describe a cost as small, cheap or \
easily recovered. Somebody else prices the upside; you price the bill.

Break every move into short priced lines, each one of:
- `setup`: money spent ONCE to start. A menu reprint, a photographer, a sign, \
a deposit, a licence, the hours spent setting the thing up.
- `daily`: money spent EVERY DAY it runs. Extra ingredients, an added shift, a \
delivery commission, a subscription divided down to a daily figure.

Getting that split right matters more than getting either total exactly right. \
A one-off charge recorded as daily turns a $900 reprint into $900 a day.

Rules that are not negotiable:
- Whole cents, as integers. 18000 is $180.00. Never a fraction, never a range, \
never a currency symbol.
- Every cost is POSITIVE. If a move saves money somewhere, that is not your \
call to make and there is no line for it.
- Count the owner's own labour. An hour they spend is an hour they are not \
running the business, and a move that is "free" because they do the work \
themselves is the most commonly underestimated bill there is.
- Do not price something they already own. If the "what this business already \
has" block shows they have the thing, the setup cost of acquiring it is zero \
and the line does not belong.
- If a row you were shown names a real price, use it and put its id in \
`cited_observation_ids`. If you are working from ordinary knowledge of what \
things cost, leave that list empty and say so in `basis`. An empty list is an \
honest answer and it is expected on most lines — wages and ingredients are not \
in a review corpus. Never cite a row that does not support the number.
- If a move genuinely costs nothing to run, return no lines for it rather than \
lines of zero.

`basis` is one sentence, for the owner, saying where these figures came from — \
"the extra midday shift is priced at the local minimum wage for four hours" is \
a basis, "estimated costs" is not."""


# ---------------------------------------------------------------------------
# What one costing is
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostLine:
    """One priced line of a move's bill.

    ``sourced`` is the honest half, and it is computed here rather than taken
    from the model: it is True only when the line cited a row this find actually
    put in front of the coster. A line the owner can trace and a line resting on
    general knowledge are both legitimate, and they are not the same claim.
    """

    label: str
    cents: int
    kind: str
    sourced: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "cents": self.cents,
                "kind": self.kind, "sourced": self.sourced}


@dataclass(frozen=True)
class CostEstimate:
    """What one move will cost, or the admission that nobody could say.

    ``checked`` distinguishes "we priced this and it is genuinely free" from
    "nobody priced it". Both would render as zero if this flag did not exist,
    and one of them is a lie.
    """

    index: int
    lines: tuple[CostLine, ...] = ()
    basis: str = ""
    #: False when this find was never actually priced — the call failed, or came
    #: back without an estimate this find could use, or every line was refused.
    checked: bool = True

    @property
    def setup_cost_cents(self) -> int:
        return sum(item.cents for item in self.lines if item.kind == SETUP)

    @property
    def recurring_daily_cost_cents(self) -> int:
        return sum(item.cents for item in self.lines if item.kind == DAILY)

    @property
    def has_estimate(self) -> bool:
        """May a cost figure be shown to the owner for this move at all?

        Only when an agent actually priced it and produced a line we could
        stand behind. Everything else shows nothing — never zero.
        """
        return self.checked and bool(self.lines)

    @property
    def measured(self) -> bool:
        """Has anyone confirmed this against real spend? Not yet, by construction.

        A property rather than a field so no caller can construct an estimate
        that claims to be measured. When owner-confirmed spend lands, that is a
        separate stored fact that supersedes this one; it is not something the
        costing agent may assert about itself.
        """
        return False

    def pays_back_within(self, *, days: int,
                         daily_revenue_cents: int | None) -> bool | None:
        """Will the setup cost be recovered inside ``days``? ``None`` if unknown.

        Three-valued on purpose. False is an accusation — "this will not pay for
        itself in the window we promised to measure it over" — and an unpriced
        move has not earned it. Flattening unknown into False would put that
        accusation on every card during an outage.
        """
        if not self.has_estimate:
            return None
        recovered = payback_days(
            setup_cost_cents=self.setup_cost_cents,
            daily_revenue_cents=daily_revenue_cents,
            daily_cost_cents=self.recurring_daily_cost_cents,
        )
        if recovered is None:
            return False
        return recovered <= days

    def as_dict(self) -> dict[str, Any] | None:
        """The JSONB payload, or ``None`` when there is nothing to store.

        None rather than a dict of zeroes. Once a row of zeroes is in the
        database it is indistinguishable from a genuinely free move, and no
        later reader can recover which one it was.
        """
        if not self.has_estimate:
            return None
        return {
            "setup_cost_cents": self.setup_cost_cents,
            "recurring_daily_cost_cents": self.recurring_daily_cost_cents,
            "basis": self.basis,
            "measured": self.measured,
            "lines": [item.as_dict() for item in self.lines],
        }


@dataclass(frozen=True)
class CostingResult:
    """Every estimate of one night, and whether the pass ran at all."""

    estimates: tuple[CostEstimate, ...] = ()
    #: False when the call errored, timed out, or returned a body we could not
    #: read. The night still publishes; no card carries a cost.
    checked: bool = True
    error: str | None = None

    def for_index(self, index: int) -> CostEstimate:
        """The estimate for one find, or the unknown one.

        Never raises and never returns ``None``, for the same reason
        ``RefutationResult.for_index`` does not: a caller reaching for an
        estimate that is not there is exactly when the board must not go dark.
        """
        for found in self.estimates:
            if found.index == index:
                return found
        return _unknown(index)


def _unknown(index: int) -> CostEstimate:
    return CostEstimate(index=index, lines=(), basis=NOT_COSTED_REASON,
                        checked=False)


# ---------------------------------------------------------------------------
# Payback — ordinary arithmetic over two agents' outputs
# ---------------------------------------------------------------------------

def payback_days(*, setup_cost_cents: int | None,
                 daily_revenue_cents: int | None,
                 daily_cost_cents: int | None) -> int | None:
    """Days until the one-time cost is recovered, or ``None`` if it never is.

    ``None`` covers three genuinely different situations that share one honest
    answer: the costing is unknown, the find carries no revenue figure because
    it was demoted or unchecked, or the daily cost eats the daily revenue. In
    the last case "never" is the true answer and a very large integer would be
    a worse way to say it.

    Rounds up. 39.1 days is not paid back on day 39, and rounding down would let
    a ledger call a move square before it was.
    """
    if setup_cost_cents is None or daily_revenue_cents is None:
        return None
    daily_cost = 0 if daily_cost_cents is None else daily_cost_cents
    net = daily_revenue_cents - daily_cost
    if net <= 0:
        # Includes the unpriced find, whose revenue is 0 because it was demoted
        # or never challenged. Dividing by that would raise; calling it instant
        # payback would be worse than raising.
        return None
    if setup_cost_cents <= 0:
        return 0
    return math.ceil(setup_cost_cents / net)


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def _stamp(moment) -> str:
    if moment is None:
        return "capture time unknown"
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _evidence_line(hit: Retrieved) -> str:
    """One cited row, in full.

    Untruncated for the same reason the Refuter's rows are: a real price is as
    likely to be in the middle of a long listing row as at the front of it, and
    a cheaper prompt would be blind to the only citable numbers it has.
    """
    subject = f" about {hit.subject}" if hit.subject else ""
    host = source_host(hit.source_url) or hit.source_name or "no source recorded"
    return (f"  - id={hit.observation_id} [{hit.kind}{subject}, {host}, "
            f"{_stamp(hit.observed_at)}]\n    {hit.content}")


def build_costing_prompt(
    *,
    finds: Sequence[ParsedFind],
    retrieved: Mapping[str, Retrieved],
    business: Mapping[str, Any] | None = None,
    business_state: Any = None,
) -> str:
    """What the coster is shown — deliberately not what the move is worth.

    Everything here is either the work to be done or the context needed to price
    it. `predicted_daily_cents`, `confidence` and `claim_type` are absent by
    construction, not by redaction: this function never reads them, so no
    formatting change can leak one back in.
    """
    # Imported here rather than at module scope: `describe_business_state` pulls
    # in the business-state module, and this agent must stay importable in a
    # test that has faked that layer away entirely.
    from brasstacks.business_state import describe_business_state

    name = (business or {}).get("name") if business else None
    lines = [f"Business: {name}" if name else "Business: this owner's business"]

    inventory = describe_business_state(business_state) if business_state else ""
    if inventory:
        lines.append("")
        lines.append(inventory)

    lines.append(
        f"\n{len(finds)} move(s) were proposed for this owner. Price each one."
    )
    for index, proposal in enumerate(finds):
        lines.append(f"\n--- index {index} ---")
        lines.append(f"Title: {proposal.title}")
        lines.append(f"Summary: {proposal.summary}")
        lines.append(f"Move: {proposal.move}")
        cited = [retrieved[observation_id]
                 for observation_id in proposal.evidence_observation_ids
                 if observation_id in retrieved]
        if cited:
            lines.append(f"Rows this move rests on ({len(cited)}):")
            lines.extend(_evidence_line(hit) for hit in cited)
        else:
            # Said plainly rather than left as an empty heading, so "no rows
            # shown" and "no rows exist" do not look the same to the model.
            lines.append("Rows this move rests on: no rows were available to show.")

    lines.append(
        "\nReturn one estimate per index. Times above are UTC. Cite a row only "
        "under the move it was listed for, and only when it carries the price."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _citable_ids(finds: Sequence[ParsedFind]) -> dict[int, set[str]]:
    """Which observation ids each estimate may name as the source of a price.

    Its own cited rows and nothing else — the same discipline as the Refuter's
    `contradicted_by`. An id the coster was not shown under this find cannot be
    what it read the number off.
    """
    return {index: set(proposal.evidence_observation_ids)
            for index, proposal in enumerate(finds)}


def _read_line(item: Any, allowed: set[str]) -> CostLine | None:
    """One priced line, or ``None`` if it is not one we can stand behind.

    Every rejection here removes money from a total rather than adding it, which
    is the safe direction for a parser to be wrong in: an omitted line makes a
    move look cheaper than it is, and the Analyst's revenue figure is already
    the optimistic half of the card. The alternative — keeping a line we cannot
    read and guessing at its value — puts an invented number on the owner's bill.
    """
    if not isinstance(item, Mapping):
        return None

    cents = item.get("cents")
    # `True` is an int in Python and would silently price a line at one cent.
    if isinstance(cents, bool) or not isinstance(cents, int):
        return None
    if cents < 0:
        # A negative cost is a revenue claim wearing a cost's clothes, and it
        # would flatter the move by reducing its own total. Only the Analyst
        # says a move earns anything.
        return None

    kind = item.get("kind")
    if kind not in COST_KINDS:
        return None

    label = item.get("label")
    label = label.strip() if isinstance(label, str) else ""
    if not label:
        return None

    raw = item.get("cited_observation_ids")
    sourced = any(
        value in allowed
        for value in (raw if isinstance(raw, (list, tuple)) else ())
        if isinstance(value, str)
    )
    return CostLine(label=label, cents=cents, kind=kind, sourced=sourced)


def _read_estimate(item: Any, allowed: dict[int, set[str]]) -> CostEstimate | None:
    """One estimate from the model, or ``None`` if it names no find we proposed."""
    if not isinstance(item, Mapping):
        return None
    index = item.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if index not in allowed:
        return None

    raw = item.get("lines")
    lines = tuple(
        parsed
        for parsed in (_read_line(entry, allowed[index])
                       for entry in (raw if isinstance(raw, (list, tuple)) else ()))
        if parsed is not None
    )
    if not lines:
        # Every line was refused, or there were none. That is not "this move is
        # free" — it is "the coster produced nothing we can stand behind", and
        # the difference is the whole point of `checked`.
        return _unknown(index)

    basis = item.get("basis")
    basis = basis.strip() if isinstance(basis, str) and basis.strip() else ""
    return CostEstimate(index=index, lines=lines,
                        basis=basis or "no basis given")


def cost_finds(
    *,
    reasoner: Any,
    finds: Sequence[ParsedFind],
    retrieved: Mapping[str, Retrieved],
    business: Mapping[str, Any] | None = None,
    business_state: Any = None,
    max_tokens: int = COSTING_MAX_TOKENS,
) -> CostingResult:
    """Price tonight's moves. One call, whatever the deck size.

    Never raises. Every failure path — provider error, timeout, refusal,
    malformed body, an estimate list that names nothing we proposed — returns a
    result whose estimates are all unknown, and an unknown estimate shows the
    owner nothing rather than showing them zero.

    ``reasoner`` is the ``Reasoner`` protocol, never an SDK client, so the whole
    pass is fakeable offline and swappable if Bedrock ever grants this account a
    current Claude model.
    """
    finds = list(finds)
    if not finds:
        return CostingResult(estimates=(), checked=True)

    try:
        # Inside the try, not before it. Assembling the prompt reads
        # business_state, parses URLs and formats capture times, and any of
        # those can raise on a shape nobody anticipated. Outside the try that
        # exception would propagate out of the Analyst's night and take the
        # whole deck with it — an advisory pass that can end a night is not
        # advisory. The Refuter learned this the same way.
        prompt = build_costing_prompt(
            finds=finds, retrieved=retrieved,
            business=business, business_state=business_state,
        )
        payload = reasoner.complete_json(
            system=COSTER_SYSTEM_PROMPT, user=prompt,
            schema=COSTING_SCHEMA, max_tokens=max_tokens,
        )
    except Exception as e:  # noqa: BLE001 — see the docstring; this is the point
        return CostingResult(
            estimates=tuple(_unknown(index) for index in range(len(finds))),
            checked=False,
            error=f"{type(e).__name__}: {e}",
        )

    raw = payload.get("estimates") if isinstance(payload, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return CostingResult(
            estimates=tuple(_unknown(index) for index in range(len(finds))),
            checked=False,
            error=f"expected a list of estimates, got {type(raw).__name__}",
        )

    allowed = _citable_ids(finds)
    seen: dict[int, CostEstimate] = {}
    for item in raw:
        parsed = _read_estimate(item, allowed)
        # First estimate per index wins, matching the Refuter. A model that
        # answers twice for one find has contradicted itself, and taking the
        # later answer would let a trailing guess overwrite a considered one.
        if parsed is not None and parsed.index not in seen:
            seen[parsed.index] = parsed

    estimates = tuple(seen[index] if index in seen else _unknown(index)
                      for index in range(len(finds)))
    # `seen` is not the test: an estimate whose every line was refused lands in
    # `seen` as an *unknown*. A receipt saying "ok" there would tell an operator
    # the deck was priced when nothing usable came back.
    checked = any(found.checked for found in seen.values())
    return CostingResult(
        estimates=estimates,
        checked=checked,
        error=None if checked else "no estimate named a find proposed tonight",
    )


# ---------------------------------------------------------------------------
# The display contract
# ---------------------------------------------------------------------------

def _stored_cents(value: Any) -> int | None:
    """One integer-cent figure out of JSONB, or ``None`` if it is not one.

    A JSONB column is not a schema. Anything a hand-edited fixture, an older
    writer or a future one can put in there arrives here, and the only safe
    reading of a value this cannot verify is "no figure".
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _money(cents: int) -> str:
    """Integer cents to the page's money string.

    Duplicated from `build_web.money` and `workflow_snapshot.money` rather than
    imported, because this module must not depend on a script or on the
    snapshot layer. It is the same two lines and a test pins the format: CLAUDE
    .md requires every money value crossing into the page to be formatted once,
    in Python, so no card ever divides by 100 in JavaScript.
    """
    dollars = cents / 100
    return f"${dollars:,.0f}" if cents % 100 == 0 else f"${dollars:,.2f}"


def cost_for_display(
    stored: Any,
    *,
    predicted_daily_cents: int | None,
    verify_after_days: int,
) -> dict[str, Any]:
    """The stable camelCase contract the owner's card reads.

    Mirrors `feed_brief_for_display`: the static first paint and the live
    workflow refresh both come through here, so the two cannot disagree about
    what a move costs.

    ``hasEstimate`` is the only field a renderer needs to branch on, and every
    figure is ``None`` when it is False. Nothing here may render as zero for a
    find nobody priced — that is the same lie at the last layer before markup.
    """
    payload: dict[str, Any] = {
        "hasEstimate": False,
        "setupCents": None,
        "dailyCents": None,
        "basis": "",
        # Never read from the row. Nothing measures a cost yet, and a stored
        # `true` — from a hand-edited fixture, or a future writer landing ahead
        # of the agent that earns it — must not reach the card as a measurement.
        # The Meter judges revenue; when owner-confirmed spend exists it will be
        # a separate stored fact, and this line is what will change to read it.
        "measured": False,
        "lines": [],
        "paybackDays": None,
        "paysBackWithinWindow": None,
        # Empty strings, not "$0". A renderer that prints whatever it is given
        # must print nothing for a move nobody priced.
        "setupTxt": "",
        "dailyTxt": "",
        "paybackTxt": "",
    }
    if not isinstance(stored, Mapping):
        return payload

    setup = _stored_cents(stored.get("setup_cost_cents"))
    daily = _stored_cents(stored.get("recurring_daily_cost_cents"))
    if setup is None and daily is None:
        return payload
    setup = setup or 0
    daily = daily or 0

    raw = stored.get("lines")
    lines = [
        {
            "label": str(item.get("label") or ""),
            "cents": _stored_cents(item.get("cents")) or 0,
            "kind": str(item.get("kind") or ""),
            "sourced": bool(item.get("sourced")),
        }
        for item in (raw if isinstance(raw, (list, tuple)) else ())
        if isinstance(item, Mapping)
    ]

    basis = stored.get("basis")
    recovered = payback_days(
        setup_cost_cents=setup,
        daily_revenue_cents=predicted_daily_cents,
        daily_cost_cents=daily,
    )
    payload.update({
        "hasEstimate": True,
        "setupCents": setup,
        "dailyCents": daily,
        "basis": basis.strip() if isinstance(basis, str) else "",
        "lines": lines,
        "paybackDays": recovered,
        # None when there is no revenue figure to divide into, because a
        # demoted or unchecked find carries 0 cents and "it never pays back"
        # would be an accusation resting on the missing half of the sum.
        "paysBackWithinWindow": (None if not predicted_daily_cents
                                 else recovered is not None
                                 and recovered <= verify_after_days),
        "setupTxt": _money(setup),
        "dailyTxt": _money(daily),
        # Words, not a number, for the two cases a number cannot carry. "Not at
        # this rate" is the honest reading of a daily cost that eats the daily
        # revenue; 0 days would read as "free money" and an empty string would
        # read as an unpriced move.
        "paybackTxt": (
            "" if not predicted_daily_cents
            else "Not at this rate" if recovered is None
            else "From day one" if recovered == 0
            else f"{recovered} day{'' if recovered == 1 else 's'}"
        ),
    })
    return payload


__all__ = [
    "COSTER_SYSTEM_PROMPT", "COSTING_MAX_TOKENS", "COSTING_SCHEMA",
    "COST_KINDS", "DAILY", "NOT_COSTED_REASON", "SETUP",
    "CostEstimate", "CostLine", "CostingResult",
    "build_costing_prompt", "cost_finds", "cost_for_display", "payback_days",
]
