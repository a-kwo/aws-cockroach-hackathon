"""The boundary between Brass Tacks and whoever actually sells the goods.

One protocol, and a fake that implements it. The real DoorDash CLI adapter lands
here later as ``DoorDashCliTool`` and changes nothing above this line — that is
the entire reason the seam exists. See docs/ORDERS_AGENT.md section 11, phase 0.

The protocol is deliberately two verbs. ``draft`` prices a cart and spends
nothing; ``place`` spends money. Everything that can be done without a payment
method stays on the free side of that split, so the dangerous call has exactly
one implementation to audit.

Money is integer cents throughout. Prices arrive from providers as decimal
strings and must be converted at the adapter, not carried inwards as floats.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol, Sequence, runtime_checkable


class OrderingError(RuntimeError):
    """The provider refused, failed, or was asked something incoherent."""


class ItemUnavailable(OrderingError):
    """The store does not sell this.

    Its own type because it is the one failure with an obvious owner-facing
    remedy — pick something else — rather than a retry.
    """


def _require_cents(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{label} must be integer cents, got {type(value).__name__}. "
            "Money is never a float in this codebase."
        )
    if value < 0:
        raise ValueError(f"{label} must not be negative, got {value}")
    return value


@dataclass(frozen=True)
class LineItem:
    name: str
    quantity: int
    unit_price_cents: int

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


@dataclass(frozen=True)
class Cart:
    """A priced basket, not yet bought."""

    store_id: str
    store_name: str
    lines: tuple[LineItem, ...]
    fees_cents: int = 0

    @property
    def subtotal_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.fees_cents

    @property
    def fingerprint(self) -> str:
        """Identifies this exact basket at this exact price.

        Approval is bound to a fingerprint so that a price moving between the
        owner seeing a cart and the order being placed is detectable rather
        than silently charged. Item order is normalised because the same
        basket listed differently is the same basket.
        """
        payload = {
            "store_id": self.store_id,
            "fees_cents": self.fees_cents,
            "lines": sorted(
                [[line.name, line.quantity, line.unit_price_cents]
                 for line in self.lines]
            ),
        }
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Receipt:
    """Proof that an order exists on the provider's side."""

    external_reference: str
    total_cents: int
    lines: tuple[LineItem, ...]
    placed_at: datetime
    store_name: str = ""
    status: str = "placed"


@runtime_checkable
class OrderingTool(Protocol):
    #: Named in task events and receipts, so a failure says which tool failed.
    name: str

    def draft(self, *, items: Sequence[tuple[str, int]],
              near: str | None = ...) -> Cart: ...

    def place(self, *, cart: Cart, idempotency_key: str) -> Receipt: ...


class FakeOrderingTool:
    """A shop that never charges anyone.

    Deterministic on purpose: the same cart always fingerprints the same, and
    receipt references are derived from the idempotency key rather than random,
    so tests can assert on them without stubbing a clock or a UUID source.
    """

    name = "fake"

    def __init__(self, *, catalogue: dict[str, int] | None = None,
                 store_id: str = "store-1", store_name: str = "Fake Store",
                 fees_cents: int = 0) -> None:
        self._catalogue = dict(catalogue or {})
        self._store_id = store_id
        self._store_name = store_name
        self._fees_cents = fees_cents
        self._receipts: dict[str, Receipt] = {}
        self._fingerprints: dict[str, str] = {}
        self._fail_next: str | None = None
        self.placed: list[Receipt] = []

    # -- test controls ----------------------------------------------------

    def set_price(self, item: str, cents: int) -> None:
        self._catalogue[item] = cents

    def fail_next(self, message: str) -> None:
        """Make the next ``place`` raise, once."""
        self._fail_next = message

    # -- the protocol -----------------------------------------------------

    def draft(self, *, items: Sequence[tuple[str, int]],
              near: str | None = None) -> Cart:
        if not items:
            raise ValueError("cannot draft an empty cart")

        lines: list[LineItem] = []
        for name, quantity in items:
            key = str(name).strip().lower()
            if key not in self._catalogue:
                raise ItemUnavailable(f"{name} is not sold here")
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                raise ValueError(f"quantity for {name} must be a whole number")
            if quantity <= 0:
                raise ValueError(
                    f"quantity for {name} must be positive, got {quantity}")
            unit = _require_cents(self._catalogue[key], f"price of {name}")
            lines.append(LineItem(name=key, quantity=quantity,
                                  unit_price_cents=unit))

        return Cart(store_id=self._store_id, store_name=self._store_name,
                    lines=tuple(lines), fees_cents=self._fees_cents)

    def place(self, *, cart: Cart, idempotency_key: str) -> Receipt:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError(
                "idempotency_key is required; without one a retry would charge "
                "the owner twice"
            )

        seen = self._receipts.get(key)
        if seen is not None:
            # A replay of the same order returns the original receipt. A
            # different cart under the same key is a caller bug worth surfacing
            # rather than papering over in either direction.
            if self._fingerprints.get(key) != cart.fingerprint:
                raise OrderingError(
                    f"idempotency key {key!r} was already used for a different "
                    "cart"
                )
            return seen

        if self._fail_next is not None:
            message, self._fail_next = self._fail_next, None
            # Deliberately before any recording: an order that did not happen
            # must leave no trace and stay retryable under the same key.
            raise OrderingError(message)

        receipt = Receipt(
            external_reference=f"fake-{key}",
            total_cents=cart.total_cents,
            lines=cart.lines,
            placed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            store_name=cart.store_name,
        )
        self._receipts[key] = receipt
        self._fingerprints[key] = cart.fingerprint
        self.placed.append(receipt)
        return receipt


__all__ = [
    "Cart",
    "FakeOrderingTool",
    "ItemUnavailable",
    "LineItem",
    "OrderingError",
    "OrderingTool",
    "Receipt",
]
