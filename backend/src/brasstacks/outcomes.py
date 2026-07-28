"""Where the Meter gets the truth about what actually happened.

This module carries the project's most important honesty constraint. The Meter's
whole value is that its verdicts are trustworthy — a published hit rate means
nothing if the outcomes behind it were manufactured. So the default source
reports *no data*, which produces an ESTIMATE rather than a verified win.

A verified verdict requires a real measurement from somewhere: the owner
reporting item-level sales, or a payment-terminal integration. Nothing in this
module ever guesses at an outcome.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from brasstacks.repository import DueFind


@dataclass(frozen=True)
class Outcome:
    """What actually happened, and how we know.

    ``has_outcome_data=False`` is a first-class answer, not an error: it is the
    honest state before a sales integration exists, and it maps to an ESTIMATE.
    """

    actual_daily_cents: int
    has_outcome_data: bool
    method: str
    note: str | None = None


@runtime_checkable
class OutcomeSource(Protocol):
    def measure(self, find: DueFind, *, business_id: str) -> Outcome: ...


class NoOutcomeSource:
    """The honest default: nothing is connected, so nothing is verified.

    Every find it measures becomes an ESTIMATE. This is what runs until a real
    sales integration exists, and it is why the demo's hit rate cannot be
    inflated by simply running the Meter more often.
    """

    def measure(self, find: DueFind, *, business_id: str) -> Outcome:
        return Outcome(
            actual_daily_cents=find.predicted_daily_cents,
            has_outcome_data=False,
            method="modelled from the prediction; no sales data connected",
            note=(
                "Recorded as an estimate. Verifying this requires item-level "
                "sales for the measurement window."
            ),
        )


class RecordedOutcomeSource:
    """Outcomes the owner reported, keyed by find id.

    This is the realistic path for a small business without an API: the owner
    says "we sold 233 of them", and that is a real measurement. Finds with no
    recorded outcome fall through to an estimate rather than being scored blind.
    """

    def __init__(self, outcomes: Mapping[str, Outcome]) -> None:
        self._outcomes = dict(outcomes)
        self._fallback = NoOutcomeSource()

    def measure(self, find: DueFind, *, business_id: str) -> Outcome:
        recorded = self._outcomes.get(find.find_id)
        if recorded is None:
            return self._fallback.measure(find, business_id=business_id)
        return recorded
