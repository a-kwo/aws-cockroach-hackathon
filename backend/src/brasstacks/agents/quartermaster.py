"""The Quartermaster: the agent that buys the business's own supplies.

Four things can start an order — the owner asking in words, a standing schedule,
an inferred stock level, or a find the owner accepted — and all four arrive here.
The trigger decides *who asked* and *what authorises it*; it never changes the
machinery, the receipt, or the safety rules. One code path, one receipt format.

This module is deliberately free of database and network access. It takes a
request, an ordering tool and the owner's standing permissions, and returns
either a receipt or a priced cart for a human to look at. The durable task rows,
the lease protocol and the real provider live outside it, which is what makes
the dangerous decisions here exhaustively testable offline.

See docs/ORDERS_AGENT.md sections 5, 6 and 10.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from brasstacks.ordering import (
    Cart,
    ItemUnavailable,
    OrderingError,
    OrderingTool,
    Receipt,
)
from brasstacks.payments import Charge, PaymentError, PaymentTool
from brasstacks.purchase_authority import (
    Authorization,
    Decision,
    PurchaseAuthority,
    authorize,
)

#: How an order came to be proposed.
TRIGGER_OWNER_INSTRUCTION = "owner_instruction"
TRIGGER_STANDING_ORDER = "standing_order"
TRIGGER_STOCK_THRESHOLD = "stock_threshold"
TRIGGER_FIND = "find"

TRIGGERS = frozenset({
    TRIGGER_OWNER_INSTRUCTION,
    TRIGGER_STANDING_ORDER,
    TRIGGER_STOCK_THRESHOLD,
    TRIGGER_FIND,
})

#: Triggers whose orders always go past a human, whatever standing permission
#: exists. A depletion model is a guess, and a guess must not be able to spend.
ALWAYS_ASK_TRIGGERS = frozenset({TRIGGER_STOCK_THRESHOLD})


class Status(str, Enum):
    PLACED = "placed"
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"


@dataclass(frozen=True)
class OrderRequest:
    items: tuple[tuple[str, int], ...]
    trigger: str
    #: Lets a category-level permission ("produce") cover a specific item.
    category: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class OrderPlan:
    status: Status
    reason: str
    #: Present whenever pricing succeeded — including when the answer is "ask
    #: the owner", because approving a spend requires seeing the amount.
    cart: Cart | None = None
    receipt: Receipt | None = None
    authorization: Authorization | None = None
    #: The money side of the receipt. Set only when a payment tool was in play
    #: and the charge succeeded — including a charge that was later refunded
    #: because the order failed, since the owner's statement will show both.
    charge: Charge | None = None

    @property
    def placed(self) -> bool:
        return self.status is Status.PLACED


def _validate(request: OrderRequest) -> None:
    if request.trigger not in TRIGGERS:
        raise ValueError(
            f"unknown trigger {request.trigger!r}; expected one of "
            f"{sorted(TRIGGERS)}"
        )


def authorize_cart(
    *,
    cart: Cart,
    request: OrderRequest,
    authorities: Sequence[PurchaseAuthority],
    spent_in_period_cents: int = 0,
) -> Authorization:
    """Whether this whole cart may be bought without asking.

    Every line must be covered, and the **cart total** is what each line's
    authority is tested against rather than that line's own subtotal. So a rule
    permitting $10 of olive oil does not help authorise a $200 order that
    happens to contain some. The strictest applicable limit wins, and one
    uncovered item stops the whole order — otherwise anything could be bought by
    bundling it with something that was covered.
    """
    for line in cart.lines:
        result = authorize(
            item=line.name,
            category=request.category,
            order_total_cents=cart.total_cents,
            authorities=authorities,
            spent_in_period_cents=spent_in_period_cents,
        )
        if result.decision is not Decision.ALLOW:
            return result
    return Authorization(
        decision=Decision.ALLOW,
        reason="Every item is covered by standing authority.",
    )


def plan_order(
    *,
    request: OrderRequest,
    tool: OrderingTool,
    authorities: Sequence[PurchaseAuthority],
    spent_in_period_cents: int = 0,
    near: str | None = None,
    idempotency_key: str | None = None,
    payment_tool: PaymentTool | None = None,
) -> OrderPlan:
    """Price the request, decide whether it may be placed, and place it if so.

    When a ``payment_tool`` is present the card is charged immediately before
    the order is placed — never earlier, so nothing the authority rules stop
    can cost a cent — and a failed order refunds its charge.
    """
    _validate(request)

    try:
        cart = tool.draft(items=list(request.items), near=near)
    except ItemUnavailable as exc:
        return OrderPlan(status=Status.FAILED, reason=str(exc))
    except OrderingError as exc:
        return OrderPlan(status=Status.FAILED, reason=str(exc))

    if request.trigger in ALWAYS_ASK_TRIGGERS:
        # Deliberately before the authority check, so that no configuration can
        # cause an inferred order to skip the human.
        return OrderPlan(
            status=Status.AWAITING_APPROVAL,
            reason=("This order came from an inferred stock level, which is an "
                    "estimate, so it always needs your approval."),
            cart=cart,
        )

    verdict = authorize_cart(cart=cart, request=request,
                             authorities=authorities,
                             spent_in_period_cents=spent_in_period_cents)
    if verdict.decision is not Decision.ALLOW:
        return OrderPlan(status=Status.AWAITING_APPROVAL, reason=verdict.reason,
                         cart=cart, authorization=verdict)

    key = idempotency_key or f"auto:{cart.fingerprint}"
    return _place(tool=tool, cart=cart, idempotency_key=key,
                  authorization=verdict, payment_tool=payment_tool)


def place_approved_order(
    *,
    request: OrderRequest,
    tool: OrderingTool,
    approved_fingerprint: str,
    idempotency_key: str,
    near: str | None = None,
    payment_tool: PaymentTool | None = None,
) -> OrderPlan:
    """Buy the cart the owner approved — and only that cart.

    Re-prices first and compares against the fingerprint the owner agreed to. If
    anything moved, the order stops and the new cart goes back for a fresh
    approval rather than being charged at a price nobody agreed to.
    """
    _validate(request)
    if not str(approved_fingerprint or "").strip():
        raise ValueError(
            "approved_fingerprint is required; an approval that is not bound to "
            "a specific cart is not an approval"
        )
    if not str(idempotency_key or "").strip():
        raise ValueError("idempotency_key is required to place an order")

    try:
        cart = tool.draft(items=list(request.items), near=near)
    except ItemUnavailable as exc:
        return OrderPlan(status=Status.FAILED, reason=str(exc))
    except OrderingError as exc:
        return OrderPlan(status=Status.FAILED, reason=str(exc))

    if cart.fingerprint != approved_fingerprint:
        return OrderPlan(
            status=Status.AWAITING_APPROVAL,
            reason=("The price or contents changed since you approved this, so "
                    "it was not placed. Here is the current cart."),
            cart=cart,
        )

    return _place(tool=tool, cart=cart, idempotency_key=idempotency_key,
                  payment_tool=payment_tool)


def requests_from_standing_orders(orders, *, today) -> list[OrderRequest]:
    """Turn today's due schedules into order requests.

    Nothing is placed here. A standing order says *what* and *when*; it says
    nothing about *how much*, so the resulting cart is priced and checked
    against the owner's spend limits like any other. A Tuesday basket whose
    price has doubled stops and asks rather than being bought because it is
    Tuesday.
    """
    from brasstacks.standing_orders import due_orders

    return [
        OrderRequest(
            items=tuple(order.items),
            trigger=TRIGGER_STANDING_ORDER,
            category=order.category,
            note=f"Standing order: {order.name}.",
        )
        for order in due_orders(orders, today=today)
    ]


def requests_from_stock(items, *, today) -> list[OrderRequest]:
    """Turn low-stock estimates into draft requests.

    Every one of these is a guess, and ``plan_order`` refuses to place a
    stock-triggered order however much standing authority exists. The estimate's
    reasoning travels along in ``note`` so the owner can correct a wrong model
    instead of discovering it in a delivery.
    """
    from brasstacks.stock import low_items

    requests: list[OrderRequest] = []
    for estimate in low_items(items, today=today):
        source = next((entry for entry in items
                       if entry.name == estimate.name), None)
        quantity = int(getattr(source, "reorder_quantity", 1) or 1)
        requests.append(OrderRequest(
            items=((estimate.name, quantity),),
            trigger=TRIGGER_STOCK_THRESHOLD,
            category=estimate.category,
            note=estimate.basis,
        ))
    return requests


def _money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def _place(*, tool: OrderingTool, cart: Cart, idempotency_key: str,
           authorization: Authorization | None = None,
           payment_tool: PaymentTool | None = None) -> OrderPlan:
    # Charge first: a provider will not hand over goods unpaid, and the charge
    # is the easier of the two to undo. Both legs share the order's idempotency
    # key (namespaced apart), so a retried invocation replays rather than
    # re-spends.
    charge: Charge | None = None
    if payment_tool is not None:
        try:
            charge = payment_tool.charge(
                amount_cents=cart.total_cents,
                description=f"{cart.store_name} — Brass Tacks order",
                idempotency_key=f"pay:{idempotency_key}",
            )
        except PaymentError as exc:
            return OrderPlan(
                status=Status.FAILED,
                reason=f"Payment failed, so nothing was ordered: {exc}",
                cart=cart,
                authorization=authorization,
            )

    try:
        receipt = tool.place(cart=cart, idempotency_key=idempotency_key)
    except OrderingError as exc:
        if charge is None:
            return OrderPlan(status=Status.FAILED, reason=str(exc), cart=cart,
                             authorization=authorization)
        # The money moved and then the shop said no. Undo the charge, and say
        # both things happened — the owner's card statement will.
        try:
            payment_tool.refund(charge_reference=charge.external_reference,
                                idempotency_key=f"refund:{idempotency_key}")
        except PaymentError as refund_exc:
            return OrderPlan(
                status=Status.FAILED,
                reason=(f"The order failed ({exc}) after your card was "
                        f"charged {_money(charge.amount_cents)}, and the "
                        f"charge could not be refunded ({refund_exc}). It "
                        "needs attention."),
                cart=cart,
                authorization=authorization,
                charge=charge,
            )
        return OrderPlan(
            status=Status.FAILED,
            reason=(f"The order failed ({exc}), so the "
                    f"{_money(charge.amount_cents)} charge to your card was "
                    "refunded."),
            cart=cart,
            authorization=authorization,
            charge=charge,
        )

    return OrderPlan(
        status=Status.PLACED,
        reason=f"Placed for {_money(receipt.total_cents)}."
               + (" Paid by card." if charge is not None else ""),
        cart=cart,
        receipt=receipt,
        authorization=authorization,
        charge=charge,
    )


__all__ = [
    "ALWAYS_ASK_TRIGGERS",
    "OrderPlan",
    "OrderRequest",
    "Status",
    "TRIGGERS",
    "TRIGGER_FIND",
    "TRIGGER_OWNER_INSTRUCTION",
    "TRIGGER_STANDING_ORDER",
    "TRIGGER_STOCK_THRESHOLD",
    "authorize_cart",
    "place_approved_order",
    "plan_order",
    "requests_from_standing_orders",
    "requests_from_stock",
]
