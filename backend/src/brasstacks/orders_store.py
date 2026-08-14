"""The Quartermaster's memory: the rows behind the DoorDash screen.

Four kinds of row — standing orders, purchase authorities, stock items and
order records — all scoped by ``business_id`` on every read and write, because
the browser never gets to choose a tenant.

Two implementations of one interface, in the repository tradition of this
codebase: ``InMemoryOrdersStore`` is what the unit suite runs against, and
``PostgresOrdersStore`` mirrors it statement for statement against CockroachDB.
Rows cross the boundary as plain dicts rather than dataclasses so the handler
can hand them straight to JSON; the domain modules re-hydrate what they need.

Money is integer cents. The store enforces that at the door rather than
trusting callers, because a cap stored as 120.5 is a bug found in a receipt.
"""

from __future__ import annotations

import re as _re
import uuid
from datetime import date, datetime
from typing import Any, Iterable, Sequence

VALID_LEVELS = {"ask_always", "ask_if_over", "auto"}
ORDER_STATUSES = {"placed", "awaiting_approval", "failed", "rejected"}
#: Which shop the Quartermaster talks to. `simulated` is the default and the
#: only one that needs no credentials; `doordash` activates when the waitlist
#: clears; `zinc` places on Amazon once its Parameter Store entries land.
#: `email` is retired as a supplier mode — carts for humans travel as rep
#: messages now — but stays valid so legacy rows load rather than crash.
VALID_SUPPLIERS = {"simulated", "doordash", "zinc", "email"}


def _validate_supplier(supplier: str, supplier_email: str | None) -> str | None:
    if supplier not in VALID_SUPPLIERS:
        raise ValueError(
            f"supplier must be one of {sorted(VALID_SUPPLIERS)}, got "
            f"{supplier!r}")
    email = str(supplier_email or "").strip() or None
    if supplier == "email":
        if not email or "@" not in email:
            raise ValueError(
                "the email supplier needs your supplier's email address")
    return email

#: Channels a rep message can travel over. iMessage is deliberately absent:
#: Apple offers no API a server could call, so offering it would be a lie.
VALID_MESSAGE_CHANNELS = {"email", "whatsapp"}

#: A note carries the owner's words; an order carries a priced cart too.
VALID_MESSAGE_KINDS = {"note", "order"}


def _validate_contact(name: str, email: str | None, phone: str | None,
                      category: str | None
                      ) -> tuple[str, str | None, str | None, str | None]:
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        raise ValueError("a contact needs a name")
    cleaned_email = str(email or "").strip() or None
    if cleaned_email and "@" not in cleaned_email:
        raise ValueError(f"{cleaned_name} needs a real email address")
    raw_phone = str(phone or "").strip()
    cleaned_phone = None
    if raw_phone:
        compact = _re.sub(r"[\s().\-]", "", raw_phone)
        if not _re.fullmatch(r"\+?\d{7,15}", compact):
            raise ValueError(
                f"{cleaned_name}'s phone doesn't look like a phone number — "
                "digits and a country code, like +14155550134")
        cleaned_phone = compact
    if cleaned_email is None and cleaned_phone is None:
        raise ValueError(
            f"{cleaned_name} needs at least one way to be reached — an "
            "email address or a phone number")
    cleaned_category = str(category or "").strip().lower() or None
    return cleaned_name, cleaned_email, cleaned_phone, cleaned_category


#: The only fields update_order may touch. Anything else is a caller typo, and
#: silently dropping it would report an update that never happened.
ORDER_UPDATE_FIELDS = {
    "status", "cart", "total_cents", "reason", "fingerprint",
    "external_reference", "payment_reference", "payment_provider",
    "decided_at",
}


def _require_cents(value: object, label: str, *, optional: bool = False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{label} must be integer cents, got {type(value).__name__}. "
            "Money is never a float in this codebase."
        )
    if value < 0:
        raise ValueError(f"{label} must not be negative, got {value}")
    return value


def _validate_standing(name: str, items: Sequence[Sequence[Any]],
                       weekday: int | None, interval_days: int | None) -> None:
    if not str(name or "").strip():
        raise ValueError("a standing order needs a name")
    if not items:
        raise ValueError(f"standing order {name!r} has no items")
    if (weekday is None) == (interval_days is None):
        raise ValueError(
            f"standing order {name!r} needs exactly one of weekday or "
            "interval_days"
        )
    if weekday is not None and not (0 <= int(weekday) <= 6):
        raise ValueError(f"weekday must be 0-6, got {weekday}")
    if interval_days is not None and int(interval_days) <= 0:
        raise ValueError(f"interval_days must be positive, got {interval_days}")


def _validate_authority(scope: str, level: str, per_order_cap_cents: int,
                        auto_threshold_cents: int | None,
                        period_cap_cents: int | None) -> None:
    if not str(scope or "").strip():
        raise ValueError("an authority needs a scope (an item or a category)")
    if level not in VALID_LEVELS:
        raise ValueError(
            f"level must be one of {sorted(VALID_LEVELS)}, got {level!r}")
    _require_cents(per_order_cap_cents, "per_order_cap_cents")
    _require_cents(auto_threshold_cents, "auto_threshold_cents", optional=True)
    _require_cents(period_cap_cents, "period_cap_cents", optional=True)
    if level == "ask_if_over" and auto_threshold_cents is None:
        raise ValueError(
            "ask_if_over needs auto_threshold_cents — the number it asks above")
    if level == "auto" and period_cap_cents is None:
        # A per-order cap alone lets an auto-buy place that amount every day.
        # "Buy on its own" only gets a runaway-proof ceiling with a rolling
        # weekly cap, so it is required here rather than optional.
        raise ValueError(
            "an auto-buy limit needs a weekly cap so it can't spend without "
            "a ceiling")


class InMemoryOrdersStore:
    """The unit suite's store. Same contract, no cluster."""

    def __init__(self) -> None:
        self._standing: list[dict[str, Any]] = []
        self._authorities: list[dict[str, Any]] = []
        self._stock: list[dict[str, Any]] = []
        self._orders: list[dict[str, Any]] = []
        self._suppliers: dict[str, dict[str, Any]] = {}
        self._contacts: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []

    # -- supplier setting --------------------------------------------------

    def get_supplier(self, business_id: str) -> dict[str, Any]:
        row = self._suppliers.get(business_id)
        if row is None:
            return {"supplier": "simulated", "supplier_email": None}
        return dict(row)

    def set_supplier(self, business_id: str, *, supplier: str,
                     supplier_email: str | None = None) -> None:
        email = _validate_supplier(supplier, supplier_email)
        self._suppliers[business_id] = {
            "supplier": supplier, "supplier_email": email}

    # -- standing orders ---------------------------------------------------

    def add_standing(self, business_id: str, *, name: str,
                     items: Sequence[Sequence[Any]],
                     weekday: int | None = None,
                     interval_days: int | None = None,
                     category: str | None = None) -> str:
        _validate_standing(name, items, weekday, interval_days)
        row = {
            "id": str(uuid.uuid4()),
            "business_id": business_id,
            "name": str(name).strip(),
            "items": [[str(n), int(q)] for n, q in items],
            "weekday": weekday,
            "interval_days": interval_days,
            "last_run_on": None,
            "enabled": True,
            "paused_until": None,
            "category": category,
        }
        self._standing.append(row)
        return row["id"]

    def list_standing(self, business_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._standing
                if r["business_id"] == business_id]

    def set_standing_enabled(self, business_id: str, standing_id: str,
                             enabled: bool) -> bool:
        for row in self._standing:
            if row["id"] == standing_id and row["business_id"] == business_id:
                row["enabled"] = bool(enabled)
                return True
        return False

    def mark_standing_ran(self, business_id: str, standing_id: str, *,
                          on: date) -> bool:
        for row in self._standing:
            if row["id"] == standing_id and row["business_id"] == business_id:
                row["last_run_on"] = on
                return True
        return False

    # -- purchase authorities ----------------------------------------------

    def add_authority(self, business_id: str, *, scope: str, level: str,
                      per_order_cap_cents: int,
                      auto_threshold_cents: int | None = None,
                      period_cap_cents: int | None = None,
                      period_days: int = 7) -> str:
        _validate_authority(scope, level, per_order_cap_cents,
                            auto_threshold_cents, period_cap_cents)
        row = {
            "id": str(uuid.uuid4()),
            "business_id": business_id,
            "scope": " ".join(str(scope).strip().lower().split()),
            "level": level,
            "per_order_cap_cents": per_order_cap_cents,
            "auto_threshold_cents": auto_threshold_cents,
            "period_cap_cents": period_cap_cents,
            "period_days": int(period_days),
            "enabled": True,
        }
        self._authorities.append(row)
        return row["id"]

    def list_authorities(self, business_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._authorities
                if r["business_id"] == business_id]

    # -- stock ---------------------------------------------------------------

    def add_stock(self, business_id: str, *, name: str, reorder_at: int,
                  usage_per_week: int, reorder_quantity: int = 1,
                  last_purchased_on: date | None = None,
                  last_purchased_quantity: int | None = None,
                  category: str | None = None) -> str:
        if not str(name or "").strip():
            raise ValueError("a stock item needs a name")
        row = {
            "id": str(uuid.uuid4()),
            "business_id": business_id,
            "name": " ".join(str(name).strip().lower().split()),
            "reorder_at": int(reorder_at),
            "usage_per_week": int(usage_per_week),
            "reorder_quantity": int(reorder_quantity),
            "last_purchased_on": last_purchased_on,
            "last_purchased_quantity": last_purchased_quantity,
            "category": category,
        }
        self._stock.append(row)
        return row["id"]

    def list_stock(self, business_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._stock
                if r["business_id"] == business_id]

    def record_purchase(self, business_id: str,
                        items: Iterable[tuple[str, int]], *, on: date) -> None:
        """A placed order feeds the next depletion estimate. Untracked items
        are ignored — buying saffron once does not make it stock."""
        bought = {str(n).strip().lower(): int(q) for n, q in items}
        for row in self._stock:
            if row["business_id"] != business_id:
                continue
            quantity = bought.get(row["name"])
            if quantity is not None:
                row["last_purchased_on"] = on
                row["last_purchased_quantity"] = quantity

    # -- order records -------------------------------------------------------

    def create_order(self, business_id: str, *, title: str, trigger: str,
                     status: str, items: Sequence[Sequence[Any]],
                     category: str | None, cart: dict[str, Any] | None,
                     total_cents: int | None, reason: str,
                     fingerprint: str | None,
                     external_reference: str | None = None,
                     payment_reference: str | None = None,
                     payment_provider: str | None = None,
                     now: datetime) -> str:
        if status not in ORDER_STATUSES:
            raise ValueError(f"unknown order status {status!r}")
        _require_cents(total_cents, "total_cents", optional=True)
        row = {
            "id": str(uuid.uuid4()),
            "business_id": business_id,
            "title": title,
            "trigger": trigger,
            "status": status,
            "items": [[str(n), int(q)] for n, q in items],
            "category": category,
            "cart": cart,
            "total_cents": total_cents,
            "reason": reason,
            "fingerprint": fingerprint,
            "external_reference": external_reference,
            "payment_reference": payment_reference,
            "payment_provider": payment_provider,
            "created_at": now,
            "decided_at": None,
        }
        self._orders.append(row)
        return row["id"]

    def get_order(self, business_id: str,
                  order_id: str) -> dict[str, Any] | None:
        for row in self._orders:
            if row["id"] == order_id and row["business_id"] == business_id:
                return dict(row)
        return None

    def list_orders(self, business_id: str, *, status: str | None = None,
                    limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self._orders if r["business_id"] == business_id
                and (status is None or r["status"] == status)]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    def update_order(self, business_id: str, order_id: str,
                     **fields: Any) -> bool:
        unknown = set(fields) - ORDER_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unknown order fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in ORDER_STATUSES:
            raise ValueError(f"unknown order status {fields['status']!r}")
        for row in self._orders:
            if row["id"] == order_id and row["business_id"] == business_id:
                row.update(fields)
                return True
        return False

    def spent_since(self, business_id: str, *, since: datetime) -> int:
        return sum(int(r["total_cents"] or 0) for r in self._orders
                   if r["business_id"] == business_id
                   and r["status"] == "placed"
                   and r["created_at"] >= since)

    # -- supplier contacts ---------------------------------------------------

    def add_contact(self, business_id: str, *, name: str,
                    email: str | None = None, phone: str | None = None,
                    category: str | None = None) -> dict[str, Any]:
        cleaned_name, cleaned_email, cleaned_phone, cleaned_category =             _validate_contact(name, email, phone, category)
        row = {
            "id": str(uuid.uuid4()),
            "business_id": business_id,
            "name": cleaned_name,
            "email": cleaned_email,
            "phone": cleaned_phone,
            "category": cleaned_category,
        }
        self._contacts.append(row)
        return {k: v for k, v in row.items() if k != "business_id"}

    def list_contacts(self, business_id: str) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "business_id"}
                for row in self._contacts
                if row["business_id"] == business_id]

    def remove_contact(self, business_id: str, contact_id: str) -> bool:
        for index, row in enumerate(self._contacts):
            if row["id"] == contact_id and row["business_id"] == business_id:
                del self._contacts[index]
                return True
        return False

    # -- rep messages ----------------------------------------------------------

    def create_message(self, business_id: str, *, contact_id: str,
                       subject: str, body: str,
                       channel: str = "email",
                       kind: str = "note",
                       cart: dict[str, Any] | None = None,
                       total_cents: int | None = None) -> dict[str, Any]:
        if channel not in VALID_MESSAGE_CHANNELS:
            raise ValueError(
                f"channel must be one of {sorted(VALID_MESSAGE_CHANNELS)}, "
                f"got {channel!r}")
        if kind not in VALID_MESSAGE_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(VALID_MESSAGE_KINDS)}, "
                f"got {kind!r}")
        _require_cents(total_cents, "total_cents", optional=True)
        row = {
            "id": str(uuid.uuid4()),
            "business_id": business_id,
            "contact_id": contact_id,
            "subject": str(subject or "").strip(),
            "body": str(body or ""),
            "channel": channel,
            "kind": kind,
            "cart": cart,
            "total_cents": total_cents,
            "status": "draft",
            "external_reference": None,
            "created_at": datetime.now(),
        }
        self._messages.append(row)
        return {k: v for k, v in row.items() if k != "business_id"}

    def list_messages(self, business_id: str,
                      *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self._messages if r["business_id"] == business_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [{k: v for k, v in row.items() if k != "business_id"}
                for row in rows[:limit]]

    def get_message(self, business_id: str,
                    message_id: str) -> dict[str, Any] | None:
        for row in self._messages:
            if row["id"] == message_id and row["business_id"] == business_id:
                return {k: v for k, v in row.items() if k != "business_id"}
        return None

    def mark_message_sent(self, business_id: str, message_id: str, *,
                          external_reference: str) -> bool:
        """Draft → sent, exactly once. A second call returns False so the
        caller can refuse instead of the rep receiving the email twice."""
        for row in self._messages:
            if row["id"] == message_id and row["business_id"] == business_id \
                    and row["status"] == "draft":
                row["status"] = "sent"
                row["external_reference"] = external_reference
                return True
        return False

    def discard_message(self, business_id: str, message_id: str) -> bool:
        for row in self._messages:
            if row["id"] == message_id and row["business_id"] == business_id \
                    and row["status"] == "draft":
                row["status"] = "discarded"
                return True
        return False


class PostgresOrdersStore:
    """The same contract against CockroachDB. One statement per method; the
    WHERE business_id clause on every row-touching statement is the tenant
    boundary, exactly as in ``PostgresRepository``."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # -- supplier setting --------------------------------------------------

    def get_supplier(self, business_id) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT supplier, supplier_email FROM orders_setting"
                " WHERE business_id = %s",
                (business_id,),
            )
            row = cur.fetchone()
        if row is None:
            return {"supplier": "simulated", "supplier_email": None}
        return {"supplier": row[0], "supplier_email": row[1]}

    def set_supplier(self, business_id, *, supplier,
                     supplier_email=None) -> None:
        email = _validate_supplier(supplier, supplier_email)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders_setting (business_id, supplier,
                                            supplier_email)
                VALUES (%s, %s, %s)
                ON CONFLICT (business_id) DO UPDATE
                SET supplier = EXCLUDED.supplier,
                    supplier_email = EXCLUDED.supplier_email,
                    updated_at = clock_timestamp()
                """,
                (business_id, supplier, email),
            )

    # -- standing orders ---------------------------------------------------

    def add_standing(self, business_id, *, name, items, weekday=None,
                     interval_days=None, category=None) -> str:
        from psycopg.types.json import Jsonb
        _validate_standing(name, items, weekday, interval_days)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO standing_order
                  (business_id, name, items, weekday, interval_days, category)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (business_id, str(name).strip(),
                 Jsonb([[str(n), int(q)] for n, q in items]),
                 weekday, interval_days, category),
            )
            return str(cur.fetchone()[0])

    def list_standing(self, business_id) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, items, weekday, interval_days, last_run_on,
                       enabled, paused_until, category
                FROM standing_order WHERE business_id = %s
                ORDER BY created_at
                """,
                (business_id,),
            )
            return [{
                "id": str(r[0]), "business_id": str(business_id),
                "name": r[1], "items": r[2], "weekday": r[3],
                "interval_days": r[4], "last_run_on": r[5], "enabled": r[6],
                "paused_until": r[7], "category": r[8],
            } for r in cur.fetchall()]

    def set_standing_enabled(self, business_id, standing_id, enabled) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE standing_order SET enabled = %s"
                " WHERE id = %s AND business_id = %s",
                (bool(enabled), standing_id, business_id),
            )
            return cur.rowcount > 0

    def mark_standing_ran(self, business_id, standing_id, *, on) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE standing_order SET last_run_on = %s"
                " WHERE id = %s AND business_id = %s",
                (on, standing_id, business_id),
            )
            return cur.rowcount > 0

    # -- purchase authorities ----------------------------------------------

    def add_authority(self, business_id, *, scope, level, per_order_cap_cents,
                      auto_threshold_cents=None, period_cap_cents=None,
                      period_days=7) -> str:
        _validate_authority(scope, level, per_order_cap_cents,
                            auto_threshold_cents, period_cap_cents)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO purchase_authority
                  (business_id, scope, level, per_order_cap_cents,
                   auto_threshold_cents, period_cap_cents, period_days)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (business_id,
                 " ".join(str(scope).strip().lower().split()), level,
                 per_order_cap_cents, auto_threshold_cents, period_cap_cents,
                 int(period_days)),
            )
            return str(cur.fetchone()[0])

    def list_authorities(self, business_id) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, scope, level, per_order_cap_cents,
                       auto_threshold_cents, period_cap_cents, period_days,
                       enabled
                FROM purchase_authority WHERE business_id = %s
                ORDER BY created_at
                """,
                (business_id,),
            )
            return [{
                "id": str(r[0]), "business_id": str(business_id),
                "scope": r[1], "level": r[2],
                "per_order_cap_cents": int(r[3]),
                "auto_threshold_cents": int(r[4]) if r[4] is not None else None,
                "period_cap_cents": int(r[5]) if r[5] is not None else None,
                "period_days": int(r[6]), "enabled": r[7],
            } for r in cur.fetchall()]

    # -- stock ---------------------------------------------------------------

    def add_stock(self, business_id, *, name, reorder_at, usage_per_week,
                  reorder_quantity=1, last_purchased_on=None,
                  last_purchased_quantity=None, category=None) -> str:
        if not str(name or "").strip():
            raise ValueError("a stock item needs a name")
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO stock_item
                  (business_id, name, reorder_at, usage_per_week,
                   reorder_quantity, last_purchased_on,
                   last_purchased_quantity, category)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (business_id,
                 " ".join(str(name).strip().lower().split()),
                 int(reorder_at), int(usage_per_week), int(reorder_quantity),
                 last_purchased_on, last_purchased_quantity, category),
            )
            return str(cur.fetchone()[0])

    def list_stock(self, business_id) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, reorder_at, usage_per_week, reorder_quantity,
                       last_purchased_on, last_purchased_quantity, category
                FROM stock_item WHERE business_id = %s
                ORDER BY created_at
                """,
                (business_id,),
            )
            return [{
                "id": str(r[0]), "business_id": str(business_id),
                "name": r[1], "reorder_at": int(r[2]),
                "usage_per_week": int(r[3]), "reorder_quantity": int(r[4]),
                "last_purchased_on": r[5], "last_purchased_quantity": r[6],
                "category": r[7],
            } for r in cur.fetchall()]

    def record_purchase(self, business_id, items, *, on) -> None:
        with self._conn.cursor() as cur:
            for name, quantity in items:
                cur.execute(
                    """
                    UPDATE stock_item
                    SET last_purchased_on = %s, last_purchased_quantity = %s
                    WHERE business_id = %s AND name = %s
                    """,
                    (on, int(quantity), business_id,
                     str(name).strip().lower()),
                )

    # -- order records -------------------------------------------------------

    def create_order(self, business_id, *, title, trigger, status, items,
                     category, cart, total_cents, reason, fingerprint,
                     external_reference=None, payment_reference=None,
                     payment_provider=None, now) -> str:
        from psycopg.types.json import Jsonb
        if status not in ORDER_STATUSES:
            raise ValueError(f"unknown order status {status!r}")
        _require_cents(total_cents, "total_cents", optional=True)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO supply_order
                  (business_id, title, trigger, status, items, category, cart,
                   total_cents, reason, fingerprint, external_reference,
                   payment_reference, payment_provider, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (business_id, title, trigger, status,
                 Jsonb([[str(n), int(q)] for n, q in items]), category,
                 Jsonb(cart) if cart is not None else None, total_cents,
                 reason, fingerprint, external_reference, payment_reference,
                 payment_provider, now),
            )
            return str(cur.fetchone()[0])

    _ORDER_COLUMNS = (
        "id, title, trigger, status, items, category, cart, total_cents,"
        " reason, fingerprint, external_reference, payment_reference,"
        " payment_provider, created_at, decided_at"
    )

    def _row_to_order(self, business_id, r) -> dict[str, Any]:
        return {
            "id": str(r[0]), "business_id": str(business_id), "title": r[1],
            "trigger": r[2], "status": r[3], "items": r[4], "category": r[5],
            "cart": r[6],
            "total_cents": int(r[7]) if r[7] is not None else None,
            "reason": r[8], "fingerprint": r[9], "external_reference": r[10],
            "payment_reference": r[11], "payment_provider": r[12],
            "created_at": r[13], "decided_at": r[14],
        }

    def get_order(self, business_id, order_id) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._ORDER_COLUMNS} FROM supply_order"
                " WHERE id = %s AND business_id = %s",
                (order_id, business_id),
            )
            row = cur.fetchone()
        return self._row_to_order(business_id, row) if row else None

    def list_orders(self, business_id, *, status=None,
                    limit=50) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            if status is None:
                cur.execute(
                    f"SELECT {self._ORDER_COLUMNS} FROM supply_order"
                    " WHERE business_id = %s"
                    " ORDER BY created_at DESC LIMIT %s",
                    (business_id, int(limit)),
                )
            else:
                cur.execute(
                    f"SELECT {self._ORDER_COLUMNS} FROM supply_order"
                    " WHERE business_id = %s AND status = %s"
                    " ORDER BY created_at DESC LIMIT %s",
                    (business_id, status, int(limit)),
                )
            return [self._row_to_order(business_id, r)
                    for r in cur.fetchall()]

    def update_order(self, business_id, order_id, **fields) -> bool:
        from psycopg.types.json import Jsonb
        unknown = set(fields) - ORDER_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unknown order fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in ORDER_STATUSES:
            raise ValueError(f"unknown order status {fields['status']!r}")
        if not fields:
            return False
        columns, values = [], []
        for key, value in fields.items():
            columns.append(f"{key} = %s")
            values.append(Jsonb(value) if key == "cart" and value is not None
                          else value)
        values.extend([order_id, business_id])
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE supply_order SET {', '.join(columns)}"
                " WHERE id = %s AND business_id = %s",
                values,
            )
            return cur.rowcount > 0

    def spent_since(self, business_id, *, since) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(total_cents), 0) FROM supply_order
                WHERE business_id = %s AND status = 'placed'
                  AND created_at >= %s
                """,
                (business_id, since),
            )
            return int(cur.fetchone()[0])

    # -- supplier contacts ---------------------------------------------------

    def add_contact(self, business_id, *, name, email=None, phone=None,
                    category=None) -> dict[str, Any]:
        cleaned_name, cleaned_email, cleaned_phone, cleaned_category = (
            _validate_contact(name, email, phone, category))
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO supplier_contact (business_id, name, email,
                                              phone, category)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (business_id, cleaned_name, cleaned_email, cleaned_phone,
                 cleaned_category),
            )
            contact_id = str(cur.fetchone()[0])
        self._conn.commit()
        return {"id": contact_id, "name": cleaned_name,
                "email": cleaned_email, "phone": cleaned_phone,
                "category": cleaned_category}

    def list_contacts(self, business_id) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, email, phone, category
                FROM supplier_contact
                WHERE business_id = %s
                ORDER BY created_at
                """,
                (business_id,),
            )
            return [{"id": str(row[0]), "name": row[1], "email": row[2],
                     "phone": row[3], "category": row[4]}
                    for row in cur.fetchall()]

    def remove_contact(self, business_id, contact_id) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM supplier_contact
                WHERE business_id = %s AND id = %s
                """,
                (business_id, contact_id),
            )
            removed = cur.rowcount > 0
        self._conn.commit()
        return removed

    # -- rep messages ----------------------------------------------------------

    def create_message(self, business_id, *, contact_id, subject,
                       body, channel="email", kind="note", cart=None,
                       total_cents=None) -> dict[str, Any]:
        if channel not in VALID_MESSAGE_CHANNELS:
            raise ValueError(
                f"channel must be one of {sorted(VALID_MESSAGE_CHANNELS)}, "
                f"got {channel!r}")
        if kind not in VALID_MESSAGE_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(VALID_MESSAGE_KINDS)}, "
                f"got {kind!r}")
        _require_cents(total_cents, "total_cents", optional=True)
        import json as _json
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO supplier_message (business_id, contact_id,
                                              subject, body, channel, kind,
                                              cart, total_cents, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                RETURNING id, created_at
                """,
                (business_id, contact_id,
                 str(subject or "").strip(), str(body or ""), channel, kind,
                 _json.dumps(cart) if cart is not None else None,
                 total_cents),
            )
            message_id, created_at = cur.fetchone()
        self._conn.commit()
        return {"id": str(message_id), "contact_id": contact_id,
                "subject": str(subject or "").strip(),
                "body": str(body or ""), "channel": channel,
                "kind": kind, "cart": cart, "total_cents": total_cents,
                "status": "draft",
                "external_reference": None, "created_at": created_at}

    def list_messages(self, business_id, *, limit=50) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, contact_id, subject, body, channel, kind,
                       cart, total_cents, status,
                       external_reference, created_at
                FROM supplier_message
                WHERE business_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (business_id, limit),
            )
            return [{"id": str(row[0]), "contact_id": str(row[1]),
                     "subject": row[2], "body": row[3], "channel": row[4],
                     "kind": row[5], "cart": row[6],
                     "total_cents": row[7], "status": row[8],
                     "external_reference": row[9], "created_at": row[10]}
                    for row in cur.fetchall()]

    def get_message(self, business_id, message_id) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, contact_id, subject, body, channel, kind,
                       cart, total_cents, status,
                       external_reference, created_at
                FROM supplier_message
                WHERE business_id = %s AND id = %s
                """,
                (business_id, message_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"id": str(row[0]), "contact_id": str(row[1]),
                "subject": row[2], "body": row[3], "channel": row[4],
                "kind": row[5], "cart": row[6],
                "total_cents": row[7], "status": row[8],
                "external_reference": row[9], "created_at": row[10]}

    def mark_message_sent(self, business_id, message_id, *,
                          external_reference) -> bool:
        #  The WHERE status = 'draft' clause is the once-only guarantee: a
        #  concurrent or repeated send matches zero rows instead of emailing
        #  the rep twice.
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE supplier_message
                SET status = 'sent', external_reference = %s,
                    sent_at = clock_timestamp()
                WHERE business_id = %s AND id = %s AND status = 'draft'
                """,
                (external_reference, business_id, message_id),
            )
            updated = cur.rowcount > 0
        self._conn.commit()
        return updated

    def discard_message(self, business_id, message_id) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE supplier_message
                SET status = 'discarded'
                WHERE business_id = %s AND id = %s AND status = 'draft'
                """,
                (business_id, message_id),
            )
            updated = cur.rowcount > 0
        self._conn.commit()
        return updated


__all__ = [
    "InMemoryOrdersStore",
    "ORDER_STATUSES",
    "ORDER_UPDATE_FIELDS",
    "PostgresOrdersStore",
    "VALID_LEVELS",
]
