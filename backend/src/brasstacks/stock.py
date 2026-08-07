"""What is probably left in the store cupboard, and what is probably running out.

Built from three things the system already has or can ask for once: what the
owner keeps on hand, what they last bought through Brass Tacks, and how long ago
that was. No POS integration, no camera, no new hardware — and it sharpens as
purchase history accumulates, which is the memory layer doing something a
stateless assistant could not.

**Everything here is an estimate.** The ledger already refuses to print a
modelled figure under a heading that says Actual; a depletion guess is the same
mistake wearing different clothes. So every result is labelled, carries the
reasoning that produced it, and — per the ordering rules — can only ever open a
draft for the owner to look at. It cannot place an order by itself.

Quantities are whole units, for the same reason money is integer cents.

See docs/ORDERS_AGENT.md section 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

DAYS_PER_WEEK = 7


@dataclass(frozen=True)
class StockItem:
    """What the owner told us they keep, plus what we last bought for them."""

    name: str
    #: Estimated remaining at or below this triggers a reorder draft.
    reorder_at: int
    #: How fast it goes, in whole units per week. Owner-declared to begin with;
    #: refinable from purchase intervals once there is history.
    usage_per_week: int
    #: How many to buy when it runs low. Owner-declared rather than derived,
    #: because "how much do you like to have in" is a judgement about storage
    #: and cash flow that the agent has no basis to infer.
    reorder_quantity: int = 1
    last_purchased_on: date | None = None
    last_purchased_quantity: int | None = None
    category: str | None = None


@dataclass(frozen=True)
class StockEstimate:
    name: str
    #: None when there is no purchase history to reason from.
    remaining: int | None
    is_low: bool
    #: How the figure was reached, in words, so the owner can correct a wrong
    #: model rather than discovering it in a delivery.
    basis: str
    known: bool
    category: str | None = None
    #: Always true. Present so callers cannot forget what they are handling.
    is_estimate: bool = True


def _require_whole(value: object, label: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{label} must be a whole number, got {type(value).__name__}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{label} must not be negative, got {value}")
    return value


def estimate_remaining(item: StockItem, *, today: date) -> StockEstimate:
    """How much of this is probably left."""
    _require_whole(item.reorder_at, "reorder_at")
    _require_whole(item.usage_per_week, "usage_per_week")

    if item.last_purchased_on is None or item.last_purchased_quantity is None:
        return StockEstimate(
            name=item.name,
            remaining=None,
            # Not low — merely unknown. Treating absence of history as "order
            # some" would buy one of everything on the owner's list the first
            # night it ran.
            is_low=False,
            known=False,
            basis=("No record of buying this through Brass Tacks yet, so there "
                   "is nothing to estimate from."),
            category=item.category,
        )

    quantity = _require_whole(item.last_purchased_quantity,
                              "last_purchased_quantity")
    days = (today - item.last_purchased_on).days
    if days < 0:
        raise ValueError(
            f"{item.name} was last purchased on {item.last_purchased_on}, which "
            f"is after {today}; refusing to guess at negative elapsed time"
        )

    # Round consumption up. The agent should err towards asking early rather
    # than letting the business run out: a false prompt costs the owner a tap,
    # an empty shelf costs them service.
    used = -(-(days * item.usage_per_week) // DAYS_PER_WEEK)
    remaining = max(0, quantity - used)

    return StockEstimate(
        name=item.name,
        remaining=remaining,
        is_low=remaining <= item.reorder_at,
        known=True,
        basis=(
            f"Last bought {days} days ago ({quantity} units); you get through "
            f"about {item.usage_per_week} a week, so roughly {remaining} left."
        ),
        category=item.category,
    )


def low_items(items, *, today: date) -> list[StockEstimate]:
    """Everything we believe is at or below its reorder point.

    Unknown items are left out rather than reported low — see the note above
    about not converting an absence of data into a purchase.
    """
    estimates = (estimate_remaining(entry, today=today) for entry in items)
    return [est for est in estimates if est.known and est.is_low]


__all__ = [
    "DAYS_PER_WEEK",
    "StockEstimate",
    "StockItem",
    "estimate_remaining",
    "low_items",
]
