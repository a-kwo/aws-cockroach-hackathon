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
from brasstacks.rep_messages import (
    compose_rep_message,
    compose_rep_order,
    compose_rep_order_text,
    compose_rep_text,
    match_rep_message,
    wants_cart,
)
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
    # The first thing a real owner asked for was milk, and the store did not
    # stock it. A simulated catalogue should stock what a kitchen runs out of.
    "milk": 4_49,
    "sugar": 3_49,
    "eggs": 5_75,
    "butter": 6_25,
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
        # Whole-word match with an optional plural, trying the full name first
        # then its head word. Word boundaries are the point: a substring match
        # ordered "rice" from "what's the price?" and "milk" from "milkshake".
        term = None
        for candidate in (name, head):
            if re.search(rf"\b{re.escape(candidate)}s?\b", lower):
                term = re.escape(candidate)
                break
        if term is None:
            continue
        # A quantity just before the item — "2 cases of tomatoes" — but never
        # across an "and" or a comma, so "3 milks and some eggs" gives the 3 to
        # milk and leaves eggs at one rather than bleeding the count over.
        near = re.search(
            rf"(\d+)(?:(?!\band\b)(?!,)\D){{0,18}}?{term}s?\b", lower)
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
        "contacts": store.list_contacts(business_id),
        "messages": [_message_json(row)
                     for row in store.list_messages(business_id)],
    }


def _message_json(row: dict[str, Any]) -> dict[str, Any]:
    fields = dict(row)
    created = fields.get("created_at")
    if isinstance(created, datetime):
        fields["created_at"] = created.isoformat()
    return fields


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


#: Why DoorDash is a slot rather than a shop, until the waitlist clears.
DOORDASH_WAITLIST_REASON = (
    "DoorDash hasn't granted API access yet — the application is on the "
    "waitlist, and orders will flow through it the moment access lands."
)

#: Why Amazon-via-Zinc is a slot rather than a shop, until its parameters land.
ZINC_UNCONFIGURED_REASON = (
    "Amazon ordering via Zinc isn't configured yet — add ZINC_CLIENT_TOKEN "
    "to Parameter Store and the option switches on."
)


def run_orders(event: Any, *, repo: Any, store: Any, tool: Any,
               payment_tool: Any = None, payment_provider: str = "simulated",
               reasoner: Any = None, now: datetime | None = None,
               email_sender: Any = None, email_source: str | None = None,
               whatsapp_sender: Any = None,
               doordash_tool: Any = None,
               zinc_factory: Any = None,
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

    # -- which shop, and which of the three are genuinely on offer ---------
    setting = store.get_supplier(business_id)
    supplier = setting["supplier"]
    email_ok = bool(email_sender and email_source)
    whatsapp_ok = bool(whatsapp_sender)

    from brasstacks.ordering import (
        UnavailableOrderingTool,
        zinc_shipping_address,
    )

    #  Zinc ships to the business's own signup address — the owner never
    #  types it twice, and each tenant's orders go to that tenant's door.
    #  No deliverable address means the option stays off with the remedy in
    #  the note, never a failure at checkout with an approved order in hand.
    zinc_tool = None
    zinc_reason = ZINC_UNCONFIGURED_REASON
    if zinc_factory is not None:
        tenant = repo.get_business(business_id) or {}
        try:
            zinc_tool = zinc_factory(zinc_shipping_address(
                business_name=tenant.get("name") or "",
                address_text=tenant.get("city"),
                phone=(tenant.get("profile_data") or {}).get("phone")))
        except ValueError as exc:
            zinc_reason = str(exc)

    #  The email supplier mode folded into rep messaging: a cart for a human
    #  goes out as a rep message ("email my rep: order …"), behind the same
    #  single Send as every other message. A legacy 'email' setting prices
    #  against the simulated store like the default.
    if supplier == "doordash":
        active_tool = doordash_tool or UnavailableOrderingTool(
            name="doordash", reason=DOORDASH_WAITLIST_REASON)
        active_payment = payment_tool if doordash_tool else None
    elif supplier == "zinc":
        active_tool = zinc_tool or UnavailableOrderingTool(
            name="zinc", reason=zinc_reason)
        #  Zinc draws on its own funded balance (or the Amazon account's gift
        #  balance); charging the Stripe card as well would be paying twice.
        active_payment = None
    else:
        active_tool = tool
        active_payment = payment_tool

    supplier_block = {
        "active": supplier,
        "email": setting["supplier_email"],
        "options": [
            {"id": "simulated", "label": "Simulated store", "available": True,
             "note": ("Practice mode — carts price against a fixed catalogue "
                      "and nothing is delivered.")},
            {"id": "doordash", "label": "DoorDash",
             "available": doordash_tool is not None,
             "note": ("Connected." if doordash_tool is not None
                      else DOORDASH_WAITLIST_REASON)},
            {"id": "zinc", "label": "Amazon (via Zinc)",
             "available": zinc_tool is not None,
             "note": (("Connected — approved orders place on Amazon through "
                       "Zinc, shipped to your business address. The card is "
                       "never charged; the Zinc balance pays.")
                      if zinc_tool is not None else zinc_reason)},
        ],
    }

    def state():
        payload = _state(store, business_id,
                         payment_provider=payment_provider, now=moment)
        payload["supplier"] = supplier_block
        return payload

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

        #  A named rep outranks the order parser: "ask my produce rep …" is
        #  unambiguous without a model, and the wrong guess in either
        #  direction costs an email a human actually reads.
        intent = match_rep_message(text, store.list_contacts(business_id))
        if intent is not None:
            if intent.get("channel") == "imessage":
                return respond(200, {
                    "kind": "failed",
                    "reason": ("iMessage doesn't offer an API a server can "
                               "call, so I can't send one. I can WhatsApp "
                               "or email your rep instead."),
                    "state": state()})
            if intent.get("contact") is None:
                return respond(200, {
                    "kind": "failed",
                    "reason": (f"There's no {intent['unknown_target']} rep "
                               "on file — add them under Reps on the "
                               "Supplies board first."),
                    "state": state()})
            contact = intent["contact"]
            if not intent["gist"]:
                return respond(200, {
                    "kind": "failed",
                    "reason": f"Tell me what to say to {contact['name']}.",
                    "state": state()})
            #  The verb picks the channel; a plain verb takes what the
            #  contact has, preferring email. Asking for a channel the
            #  contact lacks is an honest miss, never a silent fallback.
            channel = intent.get("channel")
            if channel is None:
                channel = "email" if contact.get("email") else "whatsapp"
            if channel == "whatsapp" and not contact.get("phone"):
                return respond(200, {
                    "kind": "failed",
                    "reason": (f"{contact['name']} has no phone number on "
                               "file — add one under Your reps to send "
                               "WhatsApp messages."),
                    "state": state()})
            if channel == "email" and not contact.get("email"):
                return respond(200, {
                    "kind": "failed",
                    "reason": (f"{contact['name']} has no email address on "
                               "file — add one under Your reps, or say "
                               "\"whatsapp my rep …\" instead."),
                    "state": state()})
            business = repo.get_business(business_id) or {}
            business_name = str(business.get("name") or "")
            #  A gist that OPENS with an order verb is a cart. It is priced
            #  at last known catalogue prices — estimates, since the rep
            #  invoices directly — and it still waits for Send: a human
            #  supplier is never emailed automatically, whatever standing
            #  authority exists.
            if wants_cart(intent["gist"]):
                items = simple_parse(intent["gist"], CATALOGUE)
                if not items:
                    return respond(200, {
                        "kind": "failed",
                        "reason": ("Nothing in that order matched the "
                                   "catalogue, so there is nothing I can "
                                   "price. Name items from the catalogue."),
                        "state": state()})
                lines = [{"name": name, "quantity": quantity,
                          "unit_price_cents": CATALOGUE[name],
                          "total_cents": quantity * CATALOGUE[name]}
                         for name, quantity in items]
                total = sum(line["total_cents"] for line in lines)
                if channel == "whatsapp":
                    body_text = compose_rep_order_text(
                        lines=lines, total_cents=total,
                        business_name=business_name)
                    subject = ""
                else:
                    composed = compose_rep_order(
                        contact_name=contact["name"], lines=lines,
                        total_cents=total, business_name=business_name)
                    body_text = composed["body"]
                    subject = composed["subject"]
                store.create_message(
                    business_id, contact_id=contact["id"], subject=subject,
                    body=body_text, channel=channel, kind="order",
                    cart={"lines": lines}, total_cents=total)
                return respond(200, {
                    "kind": "message_drafted",
                    "reason": (f"Drafted a {channel} order to "
                               f"{contact['name']} — it's waiting under "
                               "Needs you on the Supplies board. Nothing "
                               "goes out until you press Send."),
                    "state": state()})
            if channel == "whatsapp":
                body_text = compose_rep_text(
                    gist=intent["gist"], business_name=business_name)
                store.create_message(business_id, contact_id=contact["id"],
                                     subject="", body=body_text,
                                     channel="whatsapp")
            else:
                composed = compose_rep_message(
                    contact_name=contact["name"], gist=intent["gist"],
                    business_name=business_name)
                store.create_message(business_id, contact_id=contact["id"],
                                     subject=composed["subject"],
                                     body=composed["body"])
            return respond(200, {
                "kind": "message_drafted",
                "reason": (f"Drafted a {channel} message to "
                           f"{contact['name']} — it's waiting under Needs "
                           "you on the Supplies board. Nothing goes out "
                           "until you press Send."),
                "state": state()})

        request = None
        if reasoner is not None:
            try:
                request = parse_order_request(text, reasoner=reasoner)
            except NotAnOrderRequest as exc:
                # A trustworthy negative: the model read the message and it
                # was not an order. Different from the model failing.
                return respond(200, {"kind": "not_an_order",
                                     "reason": str(exc), "state": state()})
            except Exception:
                # The model being down, misconfigured, or returning a shape
                # order_intent refuses must cost the fancy parse, not the
                # screen. The first live deployment 500'd exactly here.
                request = None

        if request is None:
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

        plan = plan_order(request=request, tool=active_tool,
                          authorities=authorities,
                          spent_in_period_cents=spent,
                          payment_tool=active_payment)
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
            request=request, tool=active_tool,
            approved_fingerprint=row["fingerprint"],
            idempotency_key=f"approve:{order_id}",
            payment_tool=active_payment)

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
        plan = plan_order(request=request, tool=active_tool,
                          authorities=authorities,
                          spent_in_period_cents=spent,
                          payment_tool=active_payment)
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
        plan = plan_order(request=request, tool=active_tool,
                          authorities=authorities,
                          spent_in_period_cents=spent,
                          payment_tool=active_payment)
        answer = _record_plan(store, business_id, plan=plan,
                              title=f"Low stock: {row['name']}",
                              trigger=TRIGGER_STOCK_THRESHOLD,
                              items=[[row["name"],
                                      int(row["reorder_quantity"])]],
                              category=row["category"],
                              payment_provider=payment_provider, now=moment)
        answer["state"] = state()
        return respond(200, answer)

    if action == "supplier":
        choice = str(payload.get("supplier") or "").strip()
        if choice == "doordash" and doordash_tool is None:
            return respond(400, {"error": DOORDASH_WAITLIST_REASON})
        if choice == "zinc" and zinc_tool is None:
            return respond(400, {"error": zinc_reason})
        if choice == "email":
            return respond(400, {
                "error": ("Emailing your supplier now goes through reps: "
                          "add them under Your reps, then say "
                          "\"email my rep: order \u2026\" in Chat.")})
        try:
            store.set_supplier(business_id, supplier=choice,
                               supplier_email=payload.get("email"))
        except ValueError as exc:
            return respond(400, {"error": str(exc)})
        # The tool block above already ran with the OLD setting; answer with
        # freshly-read state so the screen reflects the switch immediately.
        setting.update(store.get_supplier(business_id))
        supplier_block["active"] = setting["supplier"]
        supplier_block["email"] = setting["supplier_email"]
        return respond(200, {"kind": "supplier_set", "state": state()})

    # -- sales reps, and messages to them ----------------------------------
    #  The rep is the one supplier integration every business already has.
    #  The agent may draft a message at any time; nothing reaches a human
    #  inbox until the owner presses send — the same doctrine as ordering,
    #  with the same single dangerous verb.

    if action == "contacts":
        try:
            store.add_contact(
                business_id,
                name=str(payload.get("name") or ""),
                email=payload.get("email"),
                phone=payload.get("phone"),
                category=payload.get("category"),
            )
        except ValueError as exc:
            return respond(400, {"error": str(exc)})
        return respond(200, {"kind": "contact_added", "state": state()})

    if action == "contacts/remove":
        if not store.remove_contact(business_id,
                                    str(payload.get("id") or "")):
            return respond(404, {"error": "no such contact"})
        return respond(200, {"kind": "contact_removed", "state": state()})

    if action == "message":
        text = str(payload.get("text") or "").strip()
        if not text:
            return respond(400, {"error": "say what the message should say"})
        contact = next(
            (row for row in store.list_contacts(business_id)
             if row["id"] == str(payload.get("contact_id") or "")), None)
        if contact is None:
            return respond(404, {"error": "no such contact"})
        business = repo.get_business(business_id) or {}
        channel = str(payload.get("channel") or "").strip() or (
            "email" if contact.get("email") else "whatsapp")
        if channel == "whatsapp":
            if not contact.get("phone"):
                return respond(400, {
                    "error": f"{contact['name']} has no phone number on file"})
            body_text = compose_rep_text(
                gist=text, business_name=str(business.get("name") or ""))
            store.create_message(business_id, contact_id=contact["id"],
                                 subject="", body=body_text,
                                 channel="whatsapp")
        else:
            if not contact.get("email"):
                return respond(400, {
                    "error": f"{contact['name']} has no email address on "
                             "file"})
            composed = compose_rep_message(
                contact_name=contact["name"], gist=text,
                business_name=str(business.get("name") or ""))
            store.create_message(business_id, contact_id=contact["id"],
                                 subject=composed["subject"],
                                 body=composed["body"])
        return respond(200, {"kind": "message_drafted", "state": state()})

    if action == "message/send":
        message_id = str(payload.get("id") or "")
        message = store.get_message(business_id, message_id)
        if message is None:
            return respond(404, {"error": "no such message"})
        if message["status"] != "draft":
            return respond(409, {"error": "this message was already "
                                 + ("sent" if message["status"] == "sent"
                                    else "discarded")})
        if (message.get("channel") or "email") == "email" and not email_ok:
            return respond(400, {
                "error": ("Sending email isn't available on this deployment "
                          "yet — it needs a verified sending address.")})
        contact = next(
            (row for row in store.list_contacts(business_id)
             if row["id"] == message["contact_id"]), None)
        if contact is None:
            return respond(404, {"error": "that contact no longer exists"})
        channel = message.get("channel") or "email"
        if channel == "whatsapp":
            if not whatsapp_ok:
                return respond(400, {
                    "error": ("WhatsApp isn't configured on this deployment "
                              "yet — it needs a WhatsApp Business number "
                              "and access token.")})
            if not contact.get("phone"):
                return respond(400, {
                    "error": f"{contact['name']} has no phone number on "
                             "file"})
            try:
                provider_id = whatsapp_sender(
                    recipient=contact["phone"], body=message["body"])
            except Exception as exc:
                return respond(502, {
                    "error": f"the message could not be sent: {exc}"})
            reference = f"whatsapp:{provider_id}"
        else:
            #  From: the platform's verified identity wearing the business's
            #  name; Reply-To: the owner's signup address. The From can never
            #  truthfully be the owner's own mailbox — its domain's DNS would
            #  disown the message — but this way the rep sees the business
            #  and a reply reaches the owner, which is the part that matters.
            business = repo.get_business(business_id) or {}
            display = str(business.get("name") or "").strip()
            source = (f'"{display} (via Brass Tacks)" <{email_source}>'
                      if display else email_source)
            try:
                provider_id = email_sender(
                    source=source, recipient=contact["email"],
                    subject=message["subject"], body=message["body"],
                    reply_to=repo.owner_email_for_business(business_id))
            except Exception as exc:
                return respond(502, {
                    "error": f"the message could not be sent: {exc}"})
            reference = f"email:{provider_id}"
        store.mark_message_sent(business_id, message_id,
                                external_reference=reference)
        #  An order message is also an order: once it is genuinely on its
        #  way to the rep, it enters the books — Activity gets the receipt,
        #  the stock model learns what was bought, weekly spend counts it.
        if message.get("kind") == "order" and message.get("cart"):
            lines = message["cart"].get("lines") or []
            items = [[line["name"], int(line["quantity"])] for line in lines]
            store.create_order(
                business_id,
                title=_title([(line["name"], line["quantity"])
                              for line in lines]),
                trigger=TRIGGER_OWNER_INSTRUCTION,
                status="placed",
                items=items,
                category=None,
                cart=message["cart"],
                total_cents=message.get("total_cents"),
                reason=(f"Sent to {contact['name']} — they invoice "
                        "directly; the card was never charged."),
                fingerprint=None,
                external_reference=reference,
                now=moment)
            store.record_purchase(
                business_id,
                [(line["name"], int(line["quantity"])) for line in lines],
                on=today)
        return respond(200, {"kind": "message_sent", "state": state()})

    if action == "message/discard":
        if not store.discard_message(business_id,
                                     str(payload.get("id") or "")):
            return respond(404, {"error": "no draft to discard"})
        return respond(200, {"kind": "message_discarded", "state": state()})

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


def make_ses_sender(ses: Any):
    """The emailer the deployed Lambda actually uses, extracted so a test can
    pin its signature against what ``run_orders`` calls it with. It once
    lacked ``reply_to`` entirely — the injected fakes in the suite accepted
    it happily while the real closure would have raised TypeError on the
    first live send."""

    def send(*, source, recipient, subject, body, reply_to=None):
        request = {
            "Source": source,
            "Destination": {"ToAddresses": [recipient]},
            "Message": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        }
        #  Reply-To routes the rep's answer to the owner's inbox. SES only
        #  requires the *source* to be a verified identity.
        if str(reply_to or "").strip():
            request["ReplyToAddresses"] = [str(reply_to).strip()]
        response = ses.send_email(**request)
        return str(response.get("MessageId") or "")

    return send


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

    # The email supplier rides the Maker's verified SES sender. No sender, no
    # email option — the state payload says so rather than failing at place.
    email_source = os.environ.get("MAKER_EMAIL_FROM", "").strip() or None
    email_sender = None
    if email_source:
        import boto3
        email_sender = make_ses_sender(
            boto3.client("ses", region_name=settings.aws_region))

    #  WhatsApp Business Cloud API, spoken directly over httpx like the
    #  Stripe adapter — no SDK dependency. Both parameters hydrate from SSM
    #  (/brasstacks/WHATSAPP_ACCESS_TOKEN, /brasstacks/WHATSAPP_PHONE_NUMBER_ID)
    #  the day the Meta business verification lands; until then the sender is
    #  None and message/send refuses with the reason.
    whatsapp_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    whatsapp_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    whatsapp_sender = None
    if whatsapp_token and whatsapp_number_id:
        def whatsapp_sender(*, recipient, body):
            import httpx
            response = httpx.post(
                f"https://graph.facebook.com/v20.0/{whatsapp_number_id}"
                "/messages",
                headers={"Authorization": f"Bearer {whatsapp_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": str(recipient).lstrip("+"),
                    "type": "text",
                    "text": {"body": body},
                },
                timeout=15.0,
            )
            payload = {}
            try:
                payload = response.json()
            except ValueError:
                pass
            if response.status_code >= 400:
                detail = ((payload.get("error") or {}).get("message")
                          or f"WhatsApp API returned {response.status_code}")
                raise RuntimeError(detail)
            messages = payload.get("messages") or [{}]
            return str(messages[0].get("id") or "")

    # The DoorDash adapter lands here when the waitlist clears; None keeps
    # the option visible on screen and honestly refused at runtime.
    doordash_tool = None

    #  Amazon through Zinc (api.zinc.com): one Parameter Store entry
    #  switches it on (/brasstacks/ZINC_CLIENT_TOKEN). A zn_test_ key routes
    #  to Zinc's sandbox automatically — same lifecycle, nothing ships. The
    #  ship-to is NOT configuration — run_orders derives it per tenant from
    #  the business's own signup address, which is why this is a factory
    #  rather than a tool. ZINC_RETAILER_CREDENTIALS_ID optionally pins a
    #  managed retailer account instead of the default wallet checkout.
    zinc_factory = None
    zinc_token = os.environ.get("ZINC_CLIENT_TOKEN", "").strip()
    if zinc_token:
        from brasstacks.ordering import ZincOrderingTool
        zinc_retailer = os.environ.get("ZINC_RETAILER",
                                       "amazon").strip() or "amazon"
        zinc_credentials_id = os.environ.get(
            "ZINC_RETAILER_CREDENTIALS_ID", "").strip() or None
        #  Zinc requires a delivery contact number and signup doesn't collect
        #  one (yet): a tenant phone in the profile wins, this deployment
        #  fallback covers the rest, and with neither the option stays off
        #  with the reason rather than inventing a number.
        zinc_phone = os.environ.get("ZINC_PHONE_NUMBER", "").strip()

        def zinc_factory(shipping_address):
            if zinc_phone and not shipping_address.get("phone_number"):
                shipping_address = dict(shipping_address,
                                        phone_number=zinc_phone)
            return ZincOrderingTool(
                client_token=zinc_token,
                shipping_address=shipping_address,
                retailer=zinc_retailer,
                retailer_credentials_id=zinc_credentials_id,
            )

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
                email_sender=email_sender,
                email_source=email_source,
                whatsapp_sender=whatsapp_sender,
                doordash_tool=doordash_tool,
                zinc_factory=zinc_factory,
            )
    except psycopg.Error:
        return respond(503, {"error": "orders are unavailable right now"})
    except Exception:
        # A raw Lambda crash reaches the owner as a bare 500 with no body and
        # reaches nobody else at all. Log the traceback where CloudWatch keeps
        # it, and answer with a sentence the screen can show.
        import traceback
        traceback.print_exc()
        return respond(500, {"error": "the Quartermaster hit an unexpected "
                                      "error; it has been logged"})


__all__ = [
    "CATALOGUE",
    "FEES_CENTS",
    "STORE_NAME",
    "handler",
    "respond",
    "run_orders",
    "simple_parse",
]
