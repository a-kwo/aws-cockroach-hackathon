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
import re
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


class EmailOrderingTool:
    """Orders by email to the owner's real supplier.

    The one real-world supplier that needs no API partner: many small
    businesses genuinely order by emailing a rep. ``draft`` prices from the
    known catalogue — estimates, since the supplier invoices directly, which
    is also why this mode never touches the card — and ``place`` sends the
    approved cart to a human. The receipt's external reference is the mail
    provider's message id: what an owner quotes when chasing a delivery.
    """

    name = "email"

    def __init__(self, *, catalogue: dict[str, int], recipient: str,
                 source: str, send, business_name: str = "",
                 fees_cents: int = 0) -> None:
        if not str(recipient or "").strip() or "@" not in str(recipient):
            raise ValueError("the email supplier needs a recipient address")
        if not str(source or "").strip():
            raise ValueError("a verified sender address is required")
        self._pricing = FakeOrderingTool(
            catalogue=dict(catalogue), store_id=f"email:{recipient}",
            store_name=f"Supplier ({recipient})", fees_cents=fees_cents)
        self._recipient = recipient
        self._source = source
        self._send = send
        self._business_name = str(business_name or "").strip()

    def draft(self, *, items: Sequence[tuple[str, int]],
              near: str | None = None) -> Cart:
        return self._pricing.draft(items=items, near=near)

    def place(self, *, cart: Cart, idempotency_key: str) -> Receipt:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError(
                "idempotency_key is required; without one a retry would order "
                "twice"
            )
        lines = "\n".join(
            f"  - {line.name}: {line.quantity}" for line in cart.lines)
        who = self._business_name or "a Brass Tacks business"
        subject = f"Supply order from {who}"
        body = (
            f"Hello,\n\n{who} would like to order:\n\n{lines}\n\n"
            f"Estimated value ${cart.total_cents // 100}."
            f"{cart.total_cents % 100:02d} at last known prices — please "
            "invoice as usual.\n\n"
            f"Reference: {key}\n\n"
            "Sent by the owner's Brass Tacks ordering agent, after the owner "
            "approved this order."
        )
        try:
            message_id = self._send(source=self._source,
                                    recipient=self._recipient,
                                    subject=subject, body=body)
        except Exception as exc:
            raise OrderingError(
                f"the order email could not be sent: {exc}") from exc
        return Receipt(
            external_reference=f"email:{message_id}",
            total_cents=cart.total_cents,
            lines=cart.lines,
            placed_at=datetime.now(timezone.utc),
            store_name=cart.store_name,
            status="sent",
        )


#: "street[, suite], city, ST 12345" — the shape a US owner actually types.
#: Greedy street keeps suite lines with the street; the city is whatever
#: comma-free chunk sits before the state and ZIP.
_US_ADDRESS = re.compile(
    r"^(?P<street>.+),\s*(?P<city>[^,]+?),?\s+"
    r"(?P<state>[A-Za-z]{2})[,\s]+(?P<zip>\d{5})(?:-\d{4})?$")

_TRAILING_COUNTRY = re.compile(r"[,\s]+(?:USA|US|United States)\.?\s*$",
                               re.IGNORECASE)


def zinc_shipping_address(*, business_name: str,
                          address_text: str | None,
                          phone: str | None = None) -> dict:
    """The ship-to, derived from the owner's own signup — never typed twice.

    The business row already holds the address the owner gave at signup;
    this turns it into the structured form Zinc demands, or refuses with a
    sentence the owner can act on. Deterministic parsing only: a *guessed*
    delivery address spends real money at a wrong door, so anything the
    pattern cannot read is an honest refusal, not a best effort.
    """
    text = _TRAILING_COUNTRY.sub("", " ".join(str(address_text or "").split()))
    matched = _US_ADDRESS.match(text)
    if matched is None:
        raise ValueError(
            "The business address on file doesn't read as a delivery "
            "address — Amazon needs street, city, two-letter state and ZIP, "
            "like \"1 Harbor Way, San Francisco, CA 94107\". Update it under "
            "Profile."
        )
    # Zinc requires two name fields; a business receives the parcel. A
    # one-word business ships to "<Name> Receiving", which is what a label
    # at a loading door says anyway.
    words = str(business_name or "").split()
    first = words[0] if words else "Receiving"
    last = " ".join(words[1:]) or "Receiving"
    address = {
        "first_name": first,
        "last_name": last,
        "address_line1": matched["street"].strip(),
        "city": matched["city"].strip(),
        "state": matched["state"].upper(),
        "postal_code": matched["zip"],
        "country": "US",
    }
    # Zinc requires a delivery contact number; the tenant's own wins when
    # the profile holds one. Absent beats invented — with no number here
    # and no deployment fallback, the adapter refuses at construction
    # rather than shipping with a made-up contact.
    digits = re.sub(r"[^\d+]", "", str(phone or ""))
    if digits:
        address["phone_number"] = digits
    return address


def _zinc_transport(*, method: str, url: str, json_body: dict | None,
                    headers: dict) -> tuple[int, dict]:
    import httpx

    response = httpx.request(method, url, json=json_body, headers=headers,
                             timeout=30.0)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body


class ZincOrderingTool:
    """Real retail orders through api.zinc.com — Amazon, spoken directly.

    Two honesty problems are structural here and both are solved in the cart
    rather than the prose. First, a search result is a specific product, not
    the owner's word for it: the line carries the product's real title, so
    the fingerprint the owner approves is bound to what will actually be
    bought, and a different search result at approval time stops the order.
    Second, Amazon adds tax and shipping the search price cannot know, so the
    draft carries an explicit allowance in ``fees_cents`` and the order is
    capped at the cart total via Zinc's required ``max_price`` — the owner
    approves a ceiling, and anything past it fails loudly at Zinc rather
    than charging what nobody agreed to.

    Zinc's ordering is asynchronous: an order moves pending → in_progress →
    order_placed (or order_failed) on its own clock. ``place`` polls
    briefly; an order still confirming when the polls run out comes back as
    a ``processing`` receipt with the order id to chase it by — never a
    silent success, never a spurious failure. Idempotency is native, but
    Zinc caps the key at 36 characters, so the wire carries a UUID5 of the
    logical key (stable per order, distinct across orders) and the original
    rides in ``metadata`` for the audit trail.

    Payment never touches the Stripe card: orders draw on the funded Zinc
    wallet. A key with the ``zn_test_`` prefix routes to Zinc's sandbox
    automatically — same lifecycle, compressed clock, nothing ships.

    (First written against the classic api.zinc.io v1 dialect; the owner's
    account lives on api.zinc.com, a different wire format. The seam held —
    nothing above this class changed.)
    """

    name = "zinc"

    _API = "https://api.zinc.com"
    _ADDRESS_FIELDS = ("first_name", "last_name", "address_line1",
                       "postal_code", "city", "state", "country",
                       "phone_number")

    def __init__(self, *, client_token: str, shipping_address: dict,
                 retailer: str = "amazon",
                 retailer_credentials_id: str | None = None,
                 fees_allowance_percent: int = 15,
                 poll_attempts: int = 5, poll_delay_seconds: float = 2.0,
                 transport=None, sleeper=None) -> None:
        if not str(client_token or "").strip():
            raise ValueError(
                "a Zinc client token is required; failing here beats failing "
                "at checkout with an approved order in hand"
            )
        missing = [field for field in self._ADDRESS_FIELDS
                   if not str((shipping_address or {}).get(field) or "").strip()]
        if missing:
            raise ValueError(
                "the shipping address is missing "
                f"{', '.join(missing)} — Zinc would reject the order hours "
                "after the owner approved it"
            )
        self._token = str(client_token).strip()
        self._address = dict(shipping_address)
        self._retailer = str(retailer or "amazon").strip() or "amazon"
        self._credentials_id = str(retailer_credentials_id or "").strip() \
            or None
        self._allowance = int(fees_allowance_percent)
        self._poll_attempts = max(1, int(poll_attempts))
        self._poll_delay = float(poll_delay_seconds)
        self._transport = transport or _zinc_transport
        if sleeper is None:  # pragma: no cover - trivial default
            import time
            sleeper = time.sleep
        self._sleep = sleeper
        #: cart fingerprint -> the resolved (product URL, quantity) pairs
        #: that priced it. ``place`` refuses a cart it never priced rather
        #: than guessing URLs from names with real money.
        self._resolved: dict[str, tuple[tuple[str, int], ...]] = {}
        self._receipts: dict[str, Receipt] = {}
        self._fingerprints: dict[str, str] = {}

    # -- the wire -----------------------------------------------------------

    def _call(self, method: str, path: str,
              json_body: dict | None = None) -> dict:
        status, body = self._transport(
            method=method, url=f"{self._API}{path}", json_body=json_body,
            headers={"Authorization": f"Bearer {self._token}"})
        if status >= 400:
            detail = (body or {}).get("detail")
            if isinstance(detail, list):  # FastAPI validation errors
                parts = []
                for item in detail:
                    if isinstance(item, dict):
                        # loc names the field; without it "Field required"
                        # is true and useless.
                        location = ".".join(
                            str(piece) for piece in item.get("loc", [])
                            if piece != "body")
                        message = str(item.get("msg", item))
                        parts.append(f"{location}: {message}" if location
                                     else message)
                    else:
                        parts.append(str(item))
                detail = "; ".join(parts)
            message = detail or (body or {}).get("message") \
                or f"Zinc returned HTTP {status}"
            raise OrderingError(str(message))
        return body or {}

    def _product_url(self, hit: dict) -> str:
        listed = str(hit.get("url") or "").strip()
        if listed:
            return listed
        return f"https://www.amazon.com/dp/{hit['product_id']}"

    # -- the protocol -------------------------------------------------------

    def draft(self, *, items: Sequence[tuple[str, int]],
              near: str | None = None) -> Cart:
        if not items:
            raise ValueError("cannot draft an empty cart")

        from urllib.parse import quote

        lines: list[LineItem] = []
        resolved: list[tuple[str, int]] = []
        for name, quantity in items:
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                raise ValueError(f"quantity for {name} must be a whole number")
            if quantity <= 0:
                raise ValueError(
                    f"quantity for {name} must be positive, got {quantity}")
            body = self._call(
                "GET",
                f"/products/search?query={quote(str(name))}"
                f"&retailer={quote(self._retailer)}")
            hit = next(
                (r for r in body.get("results") or []
                 if r.get("product_id")
                 and isinstance(r.get("price"), int) and r["price"] > 0),
                None)
            if hit is None:
                raise ItemUnavailable(
                    f"nothing matching {name!r} with a listed price is "
                    f"available on {self._retailer}")
            lines.append(LineItem(name=str(hit.get("title") or name)[:120],
                                  quantity=quantity,
                                  unit_price_cents=int(hit["price"])))
            resolved.append((self._product_url(hit), quantity))

        subtotal = sum(line.total_cents for line in lines)
        # The tax-and-shipping allowance, rounded up. Part of the total the
        # owner approves — a ceiling with a name, not a guess sold as a price.
        allowance = -(-subtotal * self._allowance // 100)
        cart = Cart(store_id=f"zinc:{self._retailer}",
                    store_name=f"{self._retailer.title()} (via Zinc)",
                    lines=tuple(lines), fees_cents=allowance)
        self._resolved[cart.fingerprint] = tuple(resolved)
        return cart

    def place(self, *, cart: Cart, idempotency_key: str) -> Receipt:
        import uuid

        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError(
                "idempotency_key is required; without one a retry would order "
                "twice"
            )
        seen = self._receipts.get(key)
        if seen is not None:
            if self._fingerprints.get(key) != cart.fingerprint:
                raise OrderingError(
                    f"idempotency key {key!r} was already used for a "
                    "different cart"
                )
            return seen

        resolved = self._resolved.get(cart.fingerprint)
        if resolved is None:
            raise OrderingError(
                "this cart was not priced by the Zinc adapter — draft it "
                "again so the product URLs are known rather than guessed"
            )

        request: dict = {
            "products": [{"url": url, "quantity": quantity}
                         for url, quantity in resolved],
            "shipping_address": self._address,
            #  The approved total is the ceiling. A price that moved past it
            #  fails loudly at Zinc rather than charging what nobody agreed to.
            "max_price": cart.total_cents,
            #  Zinc caps this field at 36 characters; UUID5 keeps it stable
            #  per logical key. The raw key travels in metadata.
            "idempotency_key": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                              f"brasstacks:{key}")),
            "metadata": {"idempotency_key": key, "source": "brasstacks"},
        }
        if self._credentials_id:
            request["retailer_credentials_id"] = self._credentials_id

        order = self._call("POST", "/orders", request)
        order_id = str(order.get("id") or "")
        if not order_id:
            raise OrderingError("Zinc accepted the order but returned no "
                                "order id to track it by")
        reference = f"zinc:{order_id}"

        for attempt in range(self._poll_attempts):
            if attempt:
                self._sleep(self._poll_delay)
                order = self._call("GET", f"/orders/{order_id}")
            status = str(order.get("status") or "")
            if status in ("pending", "in_progress"):
                continue
            if status == "order_placed":
                receipt = Receipt(external_reference=reference,
                                  total_cents=cart.total_cents,
                                  lines=cart.lines,
                                  placed_at=datetime.now(timezone.utc),
                                  store_name=cart.store_name)
                break
            failure = order.get("job_result") or {}
            raise OrderingError(
                f"the order failed at Zinc with status {status}"
                + (f" ({failure.get('code')})" if failure.get("code") else "")
                + f" (reference {reference})")
        else:
            receipt = Receipt(external_reference=reference,
                              total_cents=cart.total_cents,
                              lines=cart.lines,
                              placed_at=datetime.now(timezone.utc),
                              store_name=cart.store_name,
                              status="processing")

        self._receipts[key] = receipt
        self._fingerprints[key] = cart.fingerprint
        return receipt


class UnavailableOrderingTool:
    """A supplier slot that exists before its credentials do.

    DoorDash's ordering surface is waitlist-gated; the owner can still see
    and select the option, and what they get until access lands is an honest
    refusal with the reason — not a silent failure, not a simulation wearing
    the wrong name. The real adapter replaces this class and nothing above
    the seam changes.
    """

    def __init__(self, *, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def draft(self, *, items: Sequence[tuple[str, int]],
              near: str | None = None) -> Cart:
        raise OrderingError(self._reason)

    def place(self, *, cart: Cart, idempotency_key: str) -> Receipt:
        raise OrderingError(self._reason)


__all__ = [
    "Cart",
    "EmailOrderingTool",
    "FakeOrderingTool",
    "ItemUnavailable",
    "LineItem",
    "OrderingError",
    "OrderingTool",
    "Receipt",
    "UnavailableOrderingTool",
    "ZincOrderingTool",
    "zinc_shipping_address",
]
