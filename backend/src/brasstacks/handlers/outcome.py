"""What the owner measured, as a Lambda behind API Gateway.

    POST /finds/{find_id}/outcome   {"amount": "210.00", "basis": "week"}

This is the endpoint the product was missing. `NoOutcomeSource` is the honest
default and it can never produce a verified verdict, so until something wrote
real measurements the Meter could only ever publish estimates and the hit rate
was permanently undefined. A small business has no payments API to integrate;
what it has is an owner who can count. This is that.

Three properties worth stating plainly, because each one is a claim about
somebody's money:

* **The business comes from the session, never from the request.** A tenant id
  in the body would let any signed-in owner write measurements onto any other
  business's ledger — and unlike a find, a measurement is the thing the public
  hit rate is computed from.
* **Money is parsed to integer cents here.** The page sends the string the owner
  typed. It never multiplies by 100 itself, because `12.34 * 100` is
  1233.9999999999998 in JavaScript and that is exactly how a cent goes missing.
* **Reporting is not scoring.** This writes a row and nothing else. The Meter
  judges it when the measurement window closes, which is the whole point of
  having a window — a figure reported on day two of fourteen is not a result.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.meter import MEASUREMENT_BASES
from brasstacks.repository import RepositoryError
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Cache-Control": "no-store",
}

#: Roughly a million dollars over one period. Not a business rule — a guard so a
#: slipped keyboard cannot put a nine-figure sum on the ledger and drag the
#: verified total with it.
MAX_AMOUNT_CENTS = 100_000_000

#: Long enough for "counted the lunch covers for two weeks", short enough that
#: the column is not a paste target.
MAX_NOTE_CHARS = 280


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS),
            "body": json.dumps(body)}


def parse_amount_cents(payload: Any) -> int:
    """Integer cents from what the owner typed, or ValueError.

    `amountCents` is accepted for callers that already have cents (curl, a
    future integration). `amount` is a decimal string of currency and is parsed
    with `Decimal`, never `float`: binary floating point cannot represent 0.10,
    and money that cannot be represented exactly has no business being stored.
    """
    payload = payload if isinstance(payload, dict) else {}

    if "amountCents" in payload:
        raw = payload["amountCents"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("amountCents must be a whole number of cents")
        return _within_bounds(raw)

    raw = payload.get("amount")
    if isinstance(raw, bool):
        raise ValueError("amount must be a number")
    if isinstance(raw, int):
        return _within_bounds(raw * 100)
    if not isinstance(raw, str):
        raise ValueError("send the amount this move earned")

    # Whatever a currency field collects: symbols, thousands separators, spaces.
    text = raw.strip().replace(",", "").replace("$", "").replace(" ", "")
    if not text:
        raise ValueError("send the amount this move earned")
    try:
        cents = (Decimal(text) * 100).quantize(Decimal(1),
                                               rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError):
        raise ValueError(f"{raw!r} is not an amount") from None
    return _within_bounds(int(cents))


def _within_bounds(cents: int) -> int:
    if abs(cents) > MAX_AMOUNT_CENTS:
        raise ValueError("that amount is larger than this can record")
    return cents


def parse_request(event: Any) -> tuple[str, int, str, str | None]:
    """`(find_id, amount_cents, basis, note)` or ValueError."""
    event = event or {}
    params = event.get("pathParameters") or {}
    find_id = str(params.get("find_id") or params.get("id") or "").strip()
    if not find_id:
        raise ValueError("find_id is required in the request path")

    raw = event.get("body")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(payload, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("send a JSON body") from None

    amount_cents = parse_amount_cents(payload)

    basis = str(payload.get("basis") or "").strip().lower()
    if basis not in MEASUREMENT_BASES:
        raise ValueError(
            "basis must be one of " + ", ".join(sorted(MEASUREMENT_BASES))
        )

    note = " ".join(str(payload.get("note") or "").split()) or None
    if note and len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"note is too long ({MAX_NOTE_CHARS} character limit)")

    return find_id, amount_cents, basis, note


def record_outcome(event: Any, *, repo: Any,
                   now: datetime | None = None) -> dict[str, Any]:
    """Store one measured outcome. Dependencies handed in, as everywhere here."""
    moment = now or datetime.now(timezone.utc)

    try:
        find_id, amount_cents, basis, note = parse_request(event)
    except ValueError as exc:
        return respond(400, {"error": str(exc)})

    token = bearer_token(event)
    account = repo.account_for_session(
        token_fingerprint(token), now=moment) if token else None
    if account is None:
        return respond(401, {"error": "sign in first"})

    business_id = account.get("business_id")
    if not business_id:
        return respond(409, {"error": "finish setting up your business first"})

    try:
        report = repo.record_find_outcome(
            business_id,
            find_id=find_id,
            amount_cents=amount_cents,
            basis=basis,
            note=note,
            reported_by_account_id=account.get("account_id"),
            reported_at=moment,
        )
    except RepositoryError as exc:
        # Another tenant's find and a find that was never accepted answer the
        # same way. Telling them apart would confirm which ids are real.
        return respond(409, {"error": str(exc)})
    except (TypeError, ValueError) as exc:
        return respond(400, {"error": str(exc)})

    return respond(200, {
        "findId": report.find_id,
        "amountCents": report.amount_cents,
        "basis": report.basis,
        # Echoed so the page can show the figure the ledger will be judged on
        # rather than recomputing it and risking a different answer.
        "dailyCents": report.daily_cents,
        "note": report.note,
        "reportedAt": report.reported_at.isoformat(),
        # Deliberately not a verdict. Nothing here scores anything; the Meter
        # does that when the measurement window closes.
        "scored": False,
    })


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    method = str(((event or {}).get("requestContext") or {})
                 .get("http", {}).get("method") or "").upper()
    if method == "OPTIONS":
        return respond(204, {})

    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    try:
        with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
            return record_outcome(event, repo=PostgresRepository(conn))
    except psycopg.Error:
        return respond(503, {"error": "that figure could not be saved"})


__all__ = ["handler", "record_outcome", "parse_request", "parse_amount_cents",
           "respond", "MAX_AMOUNT_CENTS", "MAX_NOTE_CHARS"]
