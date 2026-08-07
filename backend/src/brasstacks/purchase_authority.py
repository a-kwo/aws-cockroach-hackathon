"""Whether the Quartermaster may place an order without asking the owner first.

The owner grants standing authority ahead of time — "handle produce up to $200 a
week" — and this module decides, deterministically, whether a specific cart falls
inside what they granted.

Three properties matter more than anything else here:

**No model is involved.** ``owner_rule`` already stores the owner's constraints as
prose and feeds them to the Analyst and Maker as prompt context. That is the right
home for guidance about *reasoning*. It is the wrong home for a spend ceiling,
because a limit a language model reads and interprets is a limit that can be
argued with. This module reads structured caps and does integer arithmetic.

**Failing closed means asking, not dropping.** There are only two outcomes: place
it now, or draft it and put it in front of the owner. Every uncertainty — no
authority, a revoked one, a total over the cap, a suspicious zero — resolves to
NEEDS_APPROVAL. Nothing the owner asked for is ever silently discarded, and
nothing they did not authorise is ever silently bought.

**Money is integer cents.** A float reaching this module means a receipt was
parsed wrong upstream, so it is a TypeError rather than something to coerce.

See docs/ORDERS_AGENT.md sections 6 and 10.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

DEFAULT_PERIOD_DAYS = 7


class Level(str, Enum):
    """How much rope the owner gave this item or category."""

    #: Draft it and ask, every time. The default for anything unconfigured, and
    #: forced for orders triggered by an inferred stock level.
    ASK_ALWAYS = "ask_always"
    #: Place it automatically below a threshold; ask above it.
    ASK_IF_OVER = "ask_if_over"
    #: Place it automatically within the caps, and tell the owner afterwards.
    AUTO = "auto"


class Decision(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class PurchaseAuthority:
    """One standing permission the owner granted.

    ``scope`` is an item name or a category name. An authority naming the item
    wins over one naming its category, because the specific rule is the one the
    owner wrote about this particular thing.
    """

    scope: str
    level: Level
    #: The most this authority may spend on a single order.
    per_order_cap_cents: int
    #: The most it may spend across ``period_days``. None means only the
    #: per-order cap binds — deliberate, but see the runaway-loop note in
    #: the tests before choosing it.
    period_cap_cents: int | None = None
    period_days: int = DEFAULT_PERIOD_DAYS
    enabled: bool = True
    #: Required by ASK_IF_OVER, meaningless otherwise.
    auto_threshold_cents: int | None = None


@dataclass(frozen=True)
class Authorization:
    decision: Decision
    #: Shown to the owner when they are asked to approve, so it says what
    #: happened in words rather than leaving them to infer it from a refusal.
    reason: str
    #: Which authority matched, if any.
    scope: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def _norm(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def _money(cents: int) -> str:
    """Format for humans without ever constructing a float."""
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}{whole}.{part:02d}"


def _require_cents(value: object, label: str) -> int:
    # bool is a subclass of int, and True silently behaving as 1 cent is exactly
    # the sort of thing that is discovered later, in a receipt.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{label} must be integer cents, got {type(value).__name__}. "
            "Money is never a float in this codebase."
        )
    if value < 0:
        raise ValueError(f"{label} must not be negative, got {value}")
    return value


def _match(item: str, category: str | None,
           authorities: Sequence[PurchaseAuthority]) -> PurchaseAuthority | None:
    """The most specific authority covering this item, enabled or not.

    Disabled rules are returned too, so a revoked permission can say so instead
    of being reported as though the owner never granted anything.
    """
    item_key = _norm(item)
    for authority in authorities:
        if _norm(authority.scope) == item_key:
            return authority
    if category:
        category_key = _norm(category)
        for authority in authorities:
            if _norm(authority.scope) == category_key:
                return authority
    return None


def authorize(
    *,
    item: str,
    order_total_cents: int,
    authorities: Sequence[PurchaseAuthority],
    category: str | None = None,
    spent_in_period_cents: int = 0,
) -> Authorization:
    """Decide whether this order may be placed without a human.

    ``spent_in_period_cents`` is what the caller measured from ``tool_execution``
    history over the matched authority's period. It is passed in rather than
    queried here so this stays pure and exhaustively testable.
    """
    if not str(item or "").strip():
        raise ValueError("item is required to authorise a purchase")
    _require_cents(order_total_cents, "order_total_cents")
    _require_cents(spent_in_period_cents, "spent_in_period_cents")

    authority = _match(item, category, authorities)
    if authority is None:
        return Authorization(
            decision=Decision.NEEDS_APPROVAL,
            reason=f"No standing authority covers {item.strip()}.",
        )

    if not authority.enabled:
        return Authorization(
            decision=Decision.NEEDS_APPROVAL,
            reason=(f"Standing authority for {authority.scope} was revoked, "
                    "so this needs approval."),
            scope=authority.scope,
        )

    # Validate the rule at the moment it is relied upon. An authority that
    # cannot be enforced must stop the order, not wave it through.
    _require_cents(authority.per_order_cap_cents, "per_order_cap_cents")
    if authority.period_cap_cents is not None:
        _require_cents(authority.period_cap_cents, "period_cap_cents")
    if authority.level is Level.ASK_IF_OVER:
        if authority.auto_threshold_cents is None:
            raise ValueError(
                f"authority for {authority.scope!r} is ask_if_over but sets no "
                "auto_threshold_cents; guessing one would be guessing with the "
                "owner's money"
            )
        _require_cents(authority.auto_threshold_cents, "auto_threshold_cents")

    if authority.level is Level.ASK_ALWAYS:
        return Authorization(
            decision=Decision.NEEDS_APPROVAL,
            reason=f"{authority.scope} is set to ask every time.",
            scope=authority.scope,
        )

    if order_total_cents == 0:
        # Not a purchase. Usually a cart that failed to price itself, which is
        # worth a human look rather than a free pass.
        return Authorization(
            decision=Decision.NEEDS_APPROVAL,
            reason="Order total is zero, which usually means pricing failed.",
            scope=authority.scope,
        )

    if order_total_cents > authority.per_order_cap_cents:
        return Authorization(
            decision=Decision.NEEDS_APPROVAL,
            reason=(f"${_money(order_total_cents)} is over the "
                    f"${_money(authority.per_order_cap_cents)} per-order limit "
                    f"for {authority.scope}."),
            scope=authority.scope,
        )

    if authority.period_cap_cents is not None:
        if spent_in_period_cents + order_total_cents > authority.period_cap_cents:
            return Authorization(
                decision=Decision.NEEDS_APPROVAL,
                reason=(
                    f"This would bring {authority.period_days}-day spend on "
                    f"{authority.scope} to "
                    f"${_money(spent_in_period_cents + order_total_cents)}, over "
                    f"the ${_money(authority.period_cap_cents)} limit."
                ),
                scope=authority.scope,
            )

    if authority.level is Level.ASK_IF_OVER:
        threshold = int(authority.auto_threshold_cents or 0)
        if order_total_cents > threshold:
            return Authorization(
                decision=Decision.NEEDS_APPROVAL,
                reason=(f"${_money(order_total_cents)} is over the "
                        f"${_money(threshold)} ask-me-first threshold for "
                        f"{authority.scope}."),
                scope=authority.scope,
            )

    return Authorization(
        decision=Decision.ALLOW,
        reason=(f"Within the standing authority for {authority.scope} "
                f"(${_money(order_total_cents)} of "
                f"${_money(authority.per_order_cap_cents)})."),
        scope=authority.scope,
    )


__all__ = [
    "Authorization",
    "DEFAULT_PERIOD_DAYS",
    "Decision",
    "Level",
    "PurchaseAuthority",
    "authorize",
]
