"""The /orders API: the Quartermaster, live.

The DoorDash screen stops being a preview here. Standing orders, spending
limits, stock estimates and order records are per-tenant CockroachDB rows;
asking for something prices a real cart against the owner's real limits; an
approval places and pays. Every route resolves its tenant from the caller's
session, exactly as /decision does — the browser never chooses a tenant.

Two things remain simulated, and the payload says so, so the screen can label
them honestly rather than imply a connection that does not exist:

* **The store.** DoorDash's ordering surface is waitlist-gated and macOS-only
  (docs/ORDERS_AGENT.md §2), so carts price against a fixed catalogue and
  "placing" an order creates no delivery. The seam is ``OrderingTool``; when
  access lands, the adapter swaps and this handler does not change.
* **The card, sometimes.** With STRIPE_SECRET_KEY and STRIPE_PAYMENT_METHOD in
  SSM the charge is a real Stripe PaymentIntent (test keys make test charges,
  visible in the Stripe dashboard); without them the fake pays. The receipt
  row records which one did.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from brasstacks.agents.quartermaster import (
    OrderRequest,
    Status,
    TRIGGER_OWNER_INSTRUCTION,
    TRIGGER_STANDING_ORDER,
    TRIGGER_STOCK_THRESHOLD,
    place_approved_order,
    plan_order,
)
from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.order_intent import NotAnOrderRequest, parse_order_request
from brasstacks.ordering import Cart, FakeOrderingTool
from brasstacks.purchase_authority import Level, PurchaseAuthority
from brasstacks.secrets import hydrate_environment
from brasstacks.stock import StockItem, estimate_remaining

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Cache-Control": "no-store",
}

#: What the simulated store sells, in integer cents. Shown on the screen so
#: the owner knows what an ask can match instead of guessing at a catalogue.
CATALOGUE: dict[str, int] = {
    "tomatoes": 4_50,
    "olive oil": 12_00,
    "flour": 3_25,
    "saffron": 42_00,
    "onions": 2_75,
    "rice": 6_50,
    "chicken": 9_80,
    "coffee beans": 14_00,
    "napkins": 3_99,
    "cleaning spray": 5_25,
}

STORE_NAME = "Simulated store"
FEES_CENTS = 2_99

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS),
            "body": json.dumps(body)}


def simple_parse(text: str, catalogue: dict[str, int]) -> list[tuple[str, int]]:
    """The no-model fallback for reading an ask.

    Keyword-matches the catalogue and refuses everything else — the same
    contract as ``order_intent``: unknown text becomes nothing, never a guess.
    Used when no ANTHROPIC key is configured, so the product still works
    without a model in the loop.
    """
    lower = " ".join(str(text or "").lower().split())
    items: list[tuple[str, int]] = []
    for name in catalogue:
        head = name.split(" ")[0]
        if head not in lower:
            continue
        near = re.search(rf"(\d+)[^.]{{0,18}}?{re.escape(head)}", lower)
        quantity = max(1, int(near.group(1))) if near else 1
        items.append((name, quantity))
    return items


def _cart_json(cart: Cart) -> dict[str, Any]:
    return {
        "store_name": cart.store_name,
        "lines": [{"name": line.name, "quantity": line.quantity,
                   "unit_price_cents": line.unit_price_cents,
                   "total_cents": line.total_cents} for line in cart.lines],
        "fees_cents": cart.fees_cents,
        "subtotal_cents": cart.subtotal_cents,
        "total_cents": cart.total_cents,
    }


def _authorities(store, business_id) -> list[PurchaseAuthority]:
    rows = store.list_authorities(business_id)
    return [PurchaseAuthority(
        scope=r["scope"], level=Level(r["level"]),
        per_order_cap_cents=int(r["per_order_cap_cents"]),
        period_cap_cents=(int(r["period_cap_cents"])
                          if r["period_cap_cents"] is not None else None),
        period_days=int(r["period_days"]),
        enabled=bool(r["enabled"]),
        auto_threshold_cents=(int(r["auto_threshold_cents"])
                              if r["auto_threshold_cents"] is not None
                              else None),
    ) for r in rows]


def _next_run(row: dict[str, Any], *, today: date) -> date | None:
    if not row.get("enabled"):
        return None
    weekday = row.get("weekday")
    if weekday is not None:
        ahead = (int(weekday) - today.weekday()) % 7
        candidate = today + timedelta(days=ahead)
        last = row.get("last_run_on")
        if last is not None and candidate <= last:
            candidate += timedelta(days=7)
        return candidate
    interval = row.get("interval_days")
    last = row.get("last_run_on")
    if last is None:
        return today
    return last + timedelta(days=int(interval or 0))


def _schedule_text(row: dict[str, Any]) -> str:
    if row.get("weekday") is not None:
        return f"Every {WEEKDAYS[int(row['weekday'])]}"
    return f"Every {int(row['interval_days'])} days"


def _order_json(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "decided_at"):
        value = out.get(key)
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _state(store, business_id, *, payment_provider: str,
           now: datetime) -> dict[str, Any]:
    today = now.date()
    week_ago = now - timedelta(days=7)
    spent = store.spent_since(business_id, since=week_ago)

    standing = []
    for row in store.list_standing(business_id):
        next_on = _next_run(row, today=today)
        standing.append({
            "id": row["id"], "name": row["name"], "items": row["items"],
            "category": row["category"], "enabled": row["enabled"],
            "schedule": _schedule_text(row),
            "next": next_on.isoformat() if next_on else None,
            "last_run_on": (row["last_run_on"].isoformat()
                            if row["last_run_on"] else None),
        })

    stock = []
    for row in store.list_stock(business_id):
        estimate = estimate_remaining(StockItem(
            name=row["name"], reorder_at=row["reorder_at"],
            usage_per_week=row["usage_per_week"],
            reorder_quantity=row["reorder_quantity"],
            last_purchased_on=row["last_purchased_on"],
            last_purchased_quantity=row["last_purchased_quantity"],
            category=row["category"],
        ), today=today)
        stock.append({
            "id": row["id"], "name": row["name"],
            "remaining": estimate.remaining, "known": estimate.known,
            "is_low": estimate.is_low, "basis": estimate.basis,
            "reorder_at": row["reorder_at"],
            "reorder_quantity": row["reorder_quantity"],
            "gauge_full": row["last_purchased_quantity"],
            "category": row["category"],
        })

    limits = [{
        "id": r["id"], "scope": r["scope"], "level": r["level"],
        "per_order_cap_cents": r["per_order_cap_cents"],
        "auto_threshold_cents": r["auto_threshold_cents"],
        "period_cap_cents": r["period_cap_cents"],
        "period_days": r["period_days"], "enabled": r["enabled"],
        "spent_in_period_cents": spent,
    } for r in store.list_authorities(business_id)]

    return {
        "standing": standing,
        "stock": stock,
        "limits": limits,
        "pending": [_order_json(r) for r in store.list_orders(
            business_id, status="awaiting_approval")],
        "history": [_order_json(r) for r in store.list_orders(
            business_id, limit=20)
            if r["status"] != "awaiting_approval"],
        "spent_week_cents": spent,
        "payment": {"provider": payment_provider},
        "store": {
            "name": STORE_NAME,
            "simulated": True,
            "catalogue": [{"name": name, "unit_cents": cents}
                          for name, cents in sorted(CATALOGUE.items())],
        },
    }


def _record_plan(store, business_id, *, plan, title, trigger, items, category,
                 payment_provider, now) -> dict[str, Any]:
    """Persist a plan's outcome and shape the response the screen renders."""
    if plan.status is Status.PLACED:
        order_id = store.create_order(
            business_id, title=title, trigger=trigger, status="placed",
            items=items, category=category, cart=_cart_json(plan.cart),
            total_cents=plan.cart.total_cents, reason=plan.reason,
            fingerprint=plan.cart.fingerprint,
            external_reference=plan.receipt.external_reference,
            payment_reference=(plan.charge.external_reference
                               if plan.charge else None),
            payment_provider=payment_provider if plan.charge else None,
            now=now)
        store.update_order(business_id, order_id, decided_at=now)
        store.record_purchase(business_id, [tuple(i) for i in items],
                              on=now.date())
        return {"kind": "placed", "reason": plan.reason,
                "order": _order_json(store.get_order(business_id, order_id))}

    if plan.status is Status.AWAITING_APPROVAL:
        order_id = store.create_order(
            business_id, title=title, trigger=trigger,
            status="awaiting_approval", items=items, category=category,
            cart=_cart_json(plan.cart), total_cents=plan.cart.total_cents,
            reason=plan.reason, fingerprint=plan.cart.fingerprint, now=now)
        return {"kind": "needs_approval", "reason": plan.reason,
                "order": _order_json(store.get_order(business_id, order_id))}

    return {"kind": "failed", "reason": plan.reason}


def _title(items) -> str:
    return ", ".join(f"{name} ×{quantity}" for name, quantity in items)


def _payload(event) -> dict[str, Any]:
    raw = (event or {}).get("body")
    if raw is None:
        return {}
    parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(parsed, dict):
        raise ValueError("body must be a JSON object")
    return parsed


def run_orders(event: Any, *, repo: Any, store: Any, tool: Any,
               payment_tool: Any = None, payment_provider: str = "simulated",
               reasoner: Any = None, now: datetime | None = None,
               ) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    today = moment.date()

    token = bearer_token(event)
    account = repo.account_for_session(
        token_fingerprint(token), now=moment) if token else None
    if account is None:
        return respond(401, {"error": "sign in first"})
    business_id = account.get("business_id")
    if not business_id:
        return respond(409, {"error": "finish setting up your business first"})

    method = str(((event or {}).get("requestContext") or {})
                 .get("http", {}).get("method") or "GET").upper()
    action = str(((event or {}).get("pathParameters") or {})
                 .get("proxy") or "").strip("/")

    def state():
        return _state(store, business_id, payment_provider=payment_provider,
                      now=moment)

    if method == "GET":
        return respond(200, state())

    try:
        payload = _payload(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return respond(400, {"error": f"body is not valid JSON ({exc})"})

    authorities = _authorities(store, business_id)
    spent = store.spent_since(business_id, since=moment - timedelta(days=7))

    if action == "ask":
        text = str(payload.get("text") or "").strip()
        if not text:
            return respond(400, {"error": "say what you would like to order"})

        if reasoner is not None:
            try:
                request = parse_order_request(text, reasoner=reasoner)
            except NotAnOrderRequest as exc:
                return respond(200, {"kind": "not_an_order",
                                     "reason": str(exc), "state": state()})
            except ValueError as exc:
                return respond(400, {"error": str(exc)})
        else:
            items = simple_parse(text, CATALOGUE)
            if not items:
                return respond(200, {
                    "kind": "not_an_order",
                    "reason": ("That didn't read as a request to buy "
                               "anything the store sells. Try naming an "
                               "item from the catalogue."),
                    "state": state()})
            request = OrderRequest(items=tuple(items),
                                   trigger=TRIGGER_OWNER_INSTRUCTION,
                                   note=text)

        plan = plan_order(request=request, tool=tool, authorities=authorities,
                          spent_in_period_cents=spent,
                          payment_tool=payment_tool)
        answer = _record_plan(store, business_id, plan=plan,
                              title=_title(request.items),
                              trigger=request.trigger,
                              items=[list(i) for i in request.items],
                              category=request.category,
                              payment_provider=payment_provider, now=moment)
        answer["state"] = state()
        return respond(200, answer)

    if action in {"approve", "reject"}:
        order_id = str(payload.get("order_id") or "").strip()
        row = store.get_order(business_id, order_id) if order_id else None
        if row is None:
            return respond(404, {"error": "no such order waits for you"})
        if row["status"] != "awaiting_approval":
            return respond(409, {"error": f"that order is already "
                                          f"{row['status']}"})

        if action == "reject":
            store.update_order(business_id, order_id, status="rejected",
                               decided_at=moment)
            return respond(200, {"kind": "rejected", "state": state()})

        request = OrderRequest(items=tuple((n, q) for n, q in row["items"]),
                               trigger=row["trigger"],
                               category=row["category"])
        plan = place_approved_order(
            request=request, tool=tool,
            approved_fingerprint=row["fingerprint"],
            idempotency_key=f"approve:{order_id}",
            payment_tool=payment_tool)

        if plan.status is Status.PLACED:
            store.update_order(
                business_id, order_id, status="placed",
                cart=_cart_json(plan.cart), total_cents=plan.cart.total_cents,
                reason=plan.reason,
                external_reference=plan.receipt.external_reference,
                payment_reference=(plan.charge.external_reference
                                   if plan.charge else None),
                payment_provider=(payment_provider if plan.charge else None),
                decided_at=moment)
            store.record_purchase(business_id,
                                  [tuple(i) for i in row["items"]],
                                  on=today)
            return respond(200, {"kind": "placed", "reason": plan.reason,
                                 "state": state()})
        if plan.status is Status.AWAITING_APPROVAL:
            # The price moved. The approval died with the old price; store the
            # cart the owner would actually be charged for.
            store.update_order(business_id, order_id,
                               cart=_cart_json(plan.cart),
                               total_cents=plan.cart.total_cents,
                               fingerprint=plan.cart.fingerprint,
                               reason=plan.reason)
            return respond(200, {"kind": "price_moved", "reason": plan.reason,
                                 "state": state()})
        store.update_order(business_id, order_id, status="failed",
                           reason=plan.reason, decided_at=moment)
        return respond(200, {"kind": "failed", "reason": plan.reason,
                             "state": state()})

    if action == "standing":
        try:
            store.add_standing(
                business_id, name=str(payload.get("name") or ""),
                items=[(str(n), int(q))
                       for n, q in (payload.get("items") or [])],
                weekday=payload.get("weekday"),
                interval_days=payload.get("interval_days"),
                category=payload.get("category") or None)
        except (ValueError, TypeError) as exc:
            return respond(400, {"error": str(exc)})
        return respond(200, {"kind": "standing_added", "state": state()})

    if action == "standing/run":
        sid = str(payload.get("standing_id") or "").strip()
        row = next((r for r in store.list_standing(business_id)
                    if r["id"] == sid), None)
        if row is None:
            return respond(404, {"error": "no such standing order"})
        request = OrderRequest(
            items=tuple((n, q) for n, q in row["items"]),
            trigger=TRIGGER_STANDING_ORDER, category=row["category"],
            note=f"Standing order: {row['name']}.")
        plan = plan_order(request=request, tool=tool, authorities=authorities,
                          spent_in_period_cents=spent,
                          payment_tool=payment_tool)
        if plan.status is not Status.FAILED:
            store.mark_standing_ran(business_id, sid, on=today)
        answer = _record_plan(store, business_id, plan=plan,
                              title=f"Standing order: {row['name']}",
                              trigger=TRIGGER_STANDING_ORDER,
                              items=[list(i) for i in row["items"]],
                              category=row["category"],
                              payment_provider=payment_provider, now=moment)
        answer["state"] = state()
        return respond(200, answer)

    if action == "standing/pause":
        sid = str(payload.get("standing_id") or "").strip()
        row = next((r for r in store.list_standing(business_id)
                    if r["id"] == sid), None)
        if row is None:
            return respond(404, {"error": "no such standing order"})
        store.set_standing_enabled(business_id, sid, not row["enabled"])
        return respond(200, {"kind": "standing_toggled", "state": state()})

    if action == "limits":
        try:
            store.add_authority(
                business_id, scope=str(payload.get("scope") or ""),
                level=str(payload.get("level") or ""),
                per_order_cap_cents=payload.get("per_order_cap_cents"),
                auto_threshold_cents=payload.get("auto_threshold_cents"),
                period_cap_cents=payload.get("period_cap_cents"))
        except (ValueError, TypeError) as exc:
            return respond(400, {"error": str(exc)})
        return respond(200, {"kind": "limit_added", "state": state()})

    if action == "stock":
        try:
            store.add_stock(
                business_id, name=str(payload.get("name") or ""),
                reorder_at=int(payload.get("reorder_at")),
                usage_per_week=int(payload.get("usage_per_week")),
                reorder_quantity=int(payload.get("reorder_quantity") or 1),
                category=payload.get("category") or None)
        except (ValueError, TypeError) as exc:
            return respond(400, {"error": str(exc)})
        return respond(200, {"kind": "stock_added", "state": state()})

    if action == "stock/draft":
        sid = str(payload.get("stock_id") or "").strip()
        row = next((r for r in store.list_stock(business_id)
                    if r["id"] == sid), None)
        if row is None:
            return respond(404, {"error": "no such stock item"})
        request = OrderRequest(
            items=((row["name"], int(row["reorder_quantity"])),),
            trigger=TRIGGER_STOCK_THRESHOLD, category=row["category"])
        plan = plan_order(request=request, tool=tool, authorities=authorities,
                          spent_in_period_cents=spent,
                          payment_tool=payment_tool)
        answer = _record_plan(store, business_id, plan=plan,
                              title=f"Low stock: {row['name']}",
                              trigger=TRIGGER_STOCK_THRESHOLD,
                              items=[[row["name"],
                                      int(row["reorder_quantity"])]],
                              category=row["category"],
                              payment_provider=payment_provider, now=moment)
        answer["state"] = state()
        return respond(200, answer)

    return respond(404, {"error": f"unknown orders action {action!r}"})


def _payment(settings_env) -> tuple[Any, str]:
    """Stripe when configured, the fake otherwise — and say which."""
    import os
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    method = os.environ.get("STRIPE_PAYMENT_METHOD", "").strip()
    if key and method:
        from brasstacks.payments import StripePaymentTool
        customer = os.environ.get("STRIPE_CUSTOMER", "").strip() or None
        return StripePaymentTool(api_key=key, payment_method=method,
                                 customer=customer), "stripe"
    from brasstacks.payments import FakePaymentTool
    return FakePaymentTool(), "simulated"


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    method = str(((event or {}).get("requestContext") or {})
                 .get("http", {}).get("method") or "").upper()
    if method == "OPTIONS":
        return respond(204, {})

    hydrate_environment()
    settings = Settings.load()

    import os
    import psycopg
    from brasstacks.orders_store import PostgresOrdersStore
    from brasstacks.repository_pg import PostgresRepository

    reasoner = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from brasstacks.providers import build_reasoner
            reasoner = build_reasoner(settings)
        except Exception:
            # The keyword fallback still reads asks; a broken model config
            # must not take the whole screen down with it.
            reasoner = None

    payment_tool, payment_provider = _payment(settings)

    tool = FakeOrderingTool(catalogue=dict(CATALOGUE), store_name=STORE_NAME,
                            fees_cents=FEES_CENTS)

    try:
        with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
            return run_orders(
                event,
                repo=PostgresRepository(conn),
                store=PostgresOrdersStore(conn),
                tool=tool,
                payment_tool=payment_tool,
                payment_provider=payment_provider,
                reasoner=reasoner,
            )
    except psycopg.Error:
        return respond(503, {"error": "orders are unavailable right now"})


__all__ = [
    "CATALOGUE",
    "FEES_CENTS",
    "STORE_NAME",
    "handler",
    "respond",
    "run_orders",
    "simple_parse",
]
