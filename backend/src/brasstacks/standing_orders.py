"""Recurring orders the owner set up once: which of them are due today.

A standing order says *what* and *when*. It does not say *how much* — the spend
limits in ``purchase_authority`` still apply when the resulting cart is priced,
so a Tuesday basket that has doubled in price stops and asks rather than being
bought because it was Tuesday.

No scheduler. The night already runs daily for every active tenant, so it
evaluates these itself. That avoids provisioning per-tenant EventBridge rules,
keeps the logic in the offline test suite, and means a schedule cannot drift out
of step with the row that describes it.

See docs/ORDERS_AGENT.md section 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Sequence

#: Monday is 0, matching ``datetime.date.weekday()``.
MONDAY, SUNDAY = 0, 6


@dataclass(frozen=True)
class StandingOrder:
    """One recurring purchase.

    Exactly one of ``weekday`` or ``interval_days`` must be set. A weekly order
    is anchored to a day the owner picked; an interval order counts from when it
    last ran, which suits things bought "about every fortnight".
    """

    name: str
    items: tuple[tuple[str, int], ...]
    weekday: int | None = None
    interval_days: int | None = None
    last_run_on: date | None = None
    enabled: bool = True
    #: Inclusive. "Paused until the 4th" means the 4th is still paused.
    paused_until: date | None = None
    category: str | None = None


class MalformedStandingOrder(ValueError):
    """The row cannot be evaluated, so it must not quietly never fire."""


def _validate(order: StandingOrder) -> None:
    if not order.items:
        raise MalformedStandingOrder(
            f"standing order {order.name!r} has no items")
    if order.weekday is None and order.interval_days is None:
        raise MalformedStandingOrder(
            f"standing order {order.name!r} has neither a weekday nor an "
            "interval, so it would never fire"
        )
    if order.weekday is not None and not (MONDAY <= order.weekday <= SUNDAY):
        raise MalformedStandingOrder(
            f"standing order {order.name!r} has weekday {order.weekday}, "
            f"expected {MONDAY}-{SUNDAY}"
        )
    if order.interval_days is not None and order.interval_days <= 0:
        raise MalformedStandingOrder(
            f"standing order {order.name!r} has interval_days "
            f"{order.interval_days}, which must be positive"
        )


def is_due(order: StandingOrder, *, today: date) -> bool:
    """Should this order fire today?

    Evaluated once per day, so an order that was missed for three weeks fires
    once when the run next happens rather than catching up on every occurrence
    it slept through. Catching up would place three orders for supplies the
    business has already got through without them.
    """
    _validate(order)

    if not order.enabled:
        return False
    if order.paused_until is not None and today <= order.paused_until:
        return False
    if order.last_run_on is not None and order.last_run_on >= today:
        # Already handled today. Two nightly runs, a manual re-run or a retried
        # invocation must not each place an order.
        return False

    if order.weekday is not None:
        if today.weekday() != order.weekday:
            return False
        if order.last_run_on is not None:
            if (today - order.last_run_on).days < 7:
                return False
        return True

    if order.last_run_on is None:
        return True
    return (today - order.last_run_on).days >= int(order.interval_days or 0)


def due_orders(
    orders: Iterable[StandingOrder],
    *,
    today: date,
    on_error: Callable[[tuple[StandingOrder, str]], None] | None = None,
) -> list[StandingOrder]:
    """Which of these should fire today.

    A malformed row is skipped rather than raised, because one bad standing
    order must not stop a tenant's other orders from running. It is handed to
    ``on_error`` instead of being swallowed — silently dropping it would look
    exactly like "nothing was due", and the owner would find out by running out
    of something.
    """
    due: list[StandingOrder] = []
    for order in orders:
        try:
            if is_due(order, today=today):
                due.append(order)
        except MalformedStandingOrder as exc:
            if on_error is not None:
                on_error((order, str(exc)))
    return due


__all__ = [
    "MalformedStandingOrder",
    "StandingOrder",
    "due_orders",
    "is_due",
]
