"""The boundary between Brass Tacks and whoever actually moves the money.

One protocol, a fake that implements it, and the Stripe adapter. Same design as
``ordering.py`` and for the same reason: everything above this line runs
against the fake, offline, and the adapter changes nothing above it.

The protocol is deliberately two verbs. ``charge`` spends the owner's money;
``refund`` gives it back. Nothing on the free side of that split lives here —
pricing and approval decisions belong to the Quartermaster, which calls
``charge`` only after an order is authorised to be placed.

The Stripe adapter speaks the wire format directly over ``httpx`` — the same
choice the Google Business integration made — rather than pulling in the
``stripe`` package for two form-encoded POSTs. Amounts cross the wire as the
integer cents they already are; nothing here converts through a float, ever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


class PaymentError(RuntimeError):
    """The processor refused, failed, or was asked something incoherent."""


class PaymentDeclined(PaymentError):
    """The card said no.

    Its own type because it is the one failure with an obvious owner-facing
    remedy — a different card — rather than a retry.
    """


def _require_cents(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{label} must be integer cents, got {type(value).__name__}. "
            "Money is never a float in this codebase."
        )
    if value <= 0:
        raise ValueError(f"{label} must be positive cents, got {value}")
    return value


def _require_key(value: object) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError(
            "idempotency_key is required; without one a retry would charge "
            "the owner twice"
        )
    return key


@dataclass(frozen=True)
class Charge:
    """Proof that money moved on the processor's side."""

    external_reference: str
    amount_cents: int
    currency: str
    created_at: datetime
    status: str = "succeeded"


@dataclass(frozen=True)
class Refund:
    """Proof that money came back."""

    external_reference: str
    charge_reference: str
    amount_cents: int
    created_at: datetime


@runtime_checkable
class PaymentTool(Protocol):
    #: Named in receipts and task events, so a failure says which tool failed.
    name: str

    def charge(self, *, amount_cents: int, description: str,
               idempotency_key: str) -> Charge: ...

    def refund(self, *, charge_reference: str,
               idempotency_key: str) -> Refund: ...


class FakePaymentTool:
    """A processor that never touches anyone's card.

    Deterministic on purpose, like ``FakeOrderingTool``: references derive from
    the idempotency key rather than random, so tests can assert on them without
    stubbing a clock or a UUID source.
    """

    name = "fake"

    def __init__(self) -> None:
        self._charges: dict[str, Charge] = {}
        self._refunds: dict[str, Refund] = {}
        self._fail_next: tuple[str, bool] | None = None
        self._fail_next_refund: str | None = None
        self.charged: list[Charge] = []
        self.refunded: list[Refund] = []

    # -- test controls ----------------------------------------------------

    def fail_next(self, message: str, *, declined: bool = False) -> None:
        """Make the next ``charge`` raise, once."""
        self._fail_next = (message, declined)

    def fail_next_refund(self, message: str) -> None:
        """Make the next ``refund`` raise, once — the worst path there is."""
        self._fail_next_refund = message

    # -- the protocol -----------------------------------------------------

    def charge(self, *, amount_cents: int, description: str,
               idempotency_key: str) -> Charge:
        key = _require_key(idempotency_key)
        amount = _require_cents(amount_cents, "amount_cents")

        seen = self._charges.get(key)
        if seen is not None:
            # A replay of the same charge returns the original. A different
            # amount under the same key is a caller bug worth surfacing rather
            # than papering over in either direction.
            if seen.amount_cents != amount:
                raise PaymentError(
                    f"idempotency key {key!r} was already used for a "
                    "different amount"
                )
            return seen

        if self._fail_next is not None:
            (message, declined), self._fail_next = self._fail_next, None
            # Deliberately before any recording: a charge that did not happen
            # must leave no trace and stay retryable under the same key.
            raise (PaymentDeclined if declined else PaymentError)(message)

        charge = Charge(
            external_reference=f"pay-{key}",
            amount_cents=amount,
            currency="usd",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self._charges[key] = charge
        self.charged.append(charge)
        return charge

    def refund(self, *, charge_reference: str,
               idempotency_key: str) -> Refund:
        key = _require_key(idempotency_key)

        seen = self._refunds.get(key)
        if seen is not None:
            return seen

        if self._fail_next_refund is not None:
            message, self._fail_next_refund = self._fail_next_refund, None
            raise PaymentError(message)

        charge = next((c for c in self._charges.values()
                       if c.external_reference == charge_reference), None)
        if charge is None:
            raise PaymentError(
                f"no charge {charge_reference!r} exists to refund")

        refund = Refund(
            external_reference=f"refund-{key}",
            charge_reference=charge_reference,
            amount_cents=charge.amount_cents,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self._refunds[key] = refund
        self.refunded.append(refund)
        return refund


def _default_transport(*, method: str, url: str, data: dict,
                       headers: dict) -> tuple[int, dict]:
    import httpx

    response = httpx.request(method, url, data=data, headers=headers,
                             timeout=30.0)
    return response.status_code, response.json()


class StripePaymentTool:
    """Stripe, spoken directly.

    ``charge`` creates and confirms a PaymentIntent against the saved payment
    method, off-session, in one call — the Quartermaster runs at night with
    nobody at a keyboard, so anything short of ``succeeded`` is a failure, not
    a state to wait in. ``refund`` posts against the PaymentIntent.

    The transport is injectable for exactly the reason the ordering tool is
    fake in tests: the wire format gets asserted on without a network.
    """

    name = "stripe"

    _API = "https://api.stripe.com/v1"

    def __init__(self, *, api_key: str, payment_method: str,
                 customer: str | None = None, currency: str = "usd",
                 transport=None) -> None:
        if not str(api_key or "").strip():
            raise ValueError(
                "a Stripe api_key is required; failing here beats failing at "
                "checkout with an approved order in hand"
            )
        if not str(payment_method or "").strip():
            raise ValueError("a saved payment_method is required")
        self._api_key = api_key
        self._payment_method = payment_method
        self._customer = customer
        self._currency = currency
        self._transport = transport or _default_transport

    def _post(self, path: str, data: dict, idempotency_key: str) -> dict:
        status, body = self._transport(
            method="POST",
            url=f"{self._API}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Idempotency-Key": idempotency_key,
            },
        )
        if status >= 400:
            error = (body or {}).get("error", {})
            message = error.get("message") or f"Stripe returned HTTP {status}"
            if error.get("type") == "card_error":
                raise PaymentDeclined(message)
            raise PaymentError(message)
        return body or {}

    def charge(self, *, amount_cents: int, description: str,
               idempotency_key: str) -> Charge:
        key = _require_key(idempotency_key)
        amount = _require_cents(amount_cents, "amount_cents")

        data = {
            "amount": str(amount),
            "currency": self._currency,
            "payment_method": self._payment_method,
            "description": description,
            # One call, no human: create and confirm together, and say so, so
            # Stripe uses the saved card's stored authentication.
            "confirm": "true",
            "off_session": "true",
        }
        if self._customer:
            data["customer"] = self._customer

        body = self._post("/payment_intents", data, key)

        status = str(body.get("status", ""))
        if status != "succeeded":
            # `requires_action` and friends mean a human step. Off-session
            # there is no human, so this is a failure, not a pending success.
            raise PaymentError(
                f"payment intent {body.get('id', '?')} is {status or 'unknown'},"
                " not succeeded"
            )

        return Charge(
            external_reference=str(body["id"]),
            amount_cents=int(body.get("amount", amount)),
            currency=str(body.get("currency", self._currency)),
            created_at=datetime.now(timezone.utc),
        )

    def refund(self, *, charge_reference: str,
               idempotency_key: str) -> Refund:
        key = _require_key(idempotency_key)
        body = self._post("/refunds", {"payment_intent": charge_reference}, key)
        return Refund(
            external_reference=str(body.get("id", "")),
            charge_reference=charge_reference,
            amount_cents=int(body.get("amount", 0)),
            created_at=datetime.now(timezone.utc),
        )


__all__ = [
    "Charge",
    "FakePaymentTool",
    "PaymentDeclined",
    "PaymentError",
    "PaymentTool",
    "Refund",
    "StripePaymentTool",
]
