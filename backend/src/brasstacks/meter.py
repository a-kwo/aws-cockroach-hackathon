"""The Meter — judging whether a prediction actually paid off.

This module is deliberately pure: no database, no Bedrock, no clock. It takes a
prediction the Analyst made on some earlier night and the outcome we measured
later, and returns a verdict. Everything about *fetching* those numbers lives
elsewhere, so the judgement itself can be tested exhaustively and offline.

Money is integer cents. Floats are rejected at the boundary rather than quietly
rounded, because that is how money bugs get in.
"""

from __future__ import annotations

from enum import Enum
from fractions import Fraction

#: Below this fraction of the prediction, a positive result is still a miss.
#: A move that delivers 2% of what we promised did not work; calling it a win
#: because the number is technically above zero is the kind of scorekeeping the
#: whole product exists to replace.
DEFAULT_MISS_THRESHOLD = 0.25


class Verdict(Enum):
    """Mirrors the `ledger_verdict` enum in db/schema.sql."""

    VERIFIED = "verified"
    ESTIMATED = "estimated"
    MISS = "miss"


def _require_cents(name: str, value: object) -> int:
    # bool is a subclass of int, and `True` is never a legitimate cent amount.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be integer cents, got {type(value).__name__}: {value!r}"
        )
    return value


def judge(
    *,
    predicted_daily_cents: int,
    actual_daily_cents: int | None,
    has_outcome_data: bool,
    miss_threshold: float = DEFAULT_MISS_THRESHOLD,
) -> Verdict:
    """Return the ledger verdict for a single find.

    Args:
        predicted_daily_cents: What the Analyst predicted this move would earn
            per day. Must be a non-negative integer.
        actual_daily_cents: What we measured. May be negative — a move can cost
            money — and may be ``None``, which means nothing was measured.
        has_outcome_data: Whether real outcome data exists yet. When False the
            result is always ESTIMATED, regardless of what `actual` says. We do
            not guess at a verdict we cannot support.
        miss_threshold: Fraction of the prediction that must be cleared for a
            positive result to count as verified.

    Raises:
        TypeError: if either cent amount is neither an int nor absent.
        ValueError: if the prediction is negative, if outcome data is claimed
            without a number behind it, or if the threshold is outside the unit
            interval.
    """
    predicted = _require_cents("predicted_daily_cents", predicted_daily_cents)
    # ``None`` is absence, not corruption. It is what an unmeasured find now
    # carries, since the Meter stopped writing the prediction into the actual
    # column. A *claimed* measurement with no number is still corrupt, so that
    # case is caught below rather than waved through.
    actual = (
        None if actual_daily_cents is None
        else _require_cents("actual_daily_cents", actual_daily_cents)
    )

    if predicted < 0:
        raise ValueError(
            f"predicted_daily_cents must be non-negative, got {predicted}. "
            "A negative prediction means the Analyst wrote corrupt data; we fail "
            "loudly rather than score it."
        )
    if not 0.0 <= miss_threshold <= 1.0:
        raise ValueError(
            f"miss_threshold must be within [0.0, 1.0], got {miss_threshold}"
        )

    # Validation runs before this short-circuit on purpose: corrupt input is a
    # bug worth surfacing even on a run we could not have scored anyway.
    if not has_outcome_data:
        return Verdict.ESTIMATED

    if actual is None:
        raise ValueError(
            "has_outcome_data is True but actual_daily_cents is missing. The "
            "outcome source contradicted itself; we fail loudly rather than "
            "score a find on nothing."
        )

    if actual <= 0:
        return Verdict.MISS

    # A find we valued at nothing that earned real money has no bar to clear.
    if predicted == 0:
        return Verdict.VERIFIED

    # Exact rational comparison rather than `actual < predicted * threshold`, so
    # the boundary cannot drift by a cent on a float rounding artifact.
    bar = Fraction(miss_threshold).limit_denominator(10**6) * predicted
    if actual < bar:
        return Verdict.MISS

    return Verdict.VERIFIED
