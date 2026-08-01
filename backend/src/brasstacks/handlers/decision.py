"""Owner decisions from the For You feed.

The static board can read at build time, but Do it / Pass are writes and must go
through a live endpoint. The write is tenant-scoped and only transitions an
undecided proposal, making repeated clicks idempotently reject rather than
silently rewriting history.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from brasstacks.config import Settings
from brasstacks.repository import RepositoryError
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}

UI_TO_DB = {"approved": "accepted", "rejected": "rejected"}


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS), "body": json.dumps(body)}


def parse_request(event: Any) -> tuple[str, str]:
    event = event or {}
    params = event.get("pathParameters") or {}
    find_id = str(params.get("find_id") or params.get("id") or "").strip()
    if not find_id:
        raise ValueError("find_id is required in the request path")

    raw = event.get("body")
    if raw is None:
        raise ValueError("send a JSON body with a 'decision' field")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"body is not valid JSON ({exc})") from None
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in UI_TO_DB:
        raise ValueError("decision must be 'approved' or 'rejected'")
    return find_id, decision


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    if (event or {}).get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return respond(204, {})
    try:
        find_id, decision = parse_request(event)
    except ValueError as exc:
        return respond(400, {"error": str(exc)})

    hydrate_environment()
    settings = Settings.load()
    if not settings.business_id:
        return respond(500, {"error": "no tenant configured"})

    import psycopg
    from brasstacks.repository_pg import PostgresRepository

    # Generate the timestamp once and pass the same value to CockroachDB and
    # the browser receipt.  The operator trace should use the authoritative
    # server write time rather than a potentially skewed client clock.
    decided_at = datetime.now(timezone.utc)
    try:
        with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
            PostgresRepository(conn).set_find_status(
                find_id,
                status=UI_TO_DB[decision],
                decided_at=decided_at,
                business_id=settings.business_id,
            )
    except RepositoryError as exc:
        return respond(409, {"error": str(exc)})
    except psycopg.Error:
        return respond(503, {"error": "decision could not be saved"})

    return respond(200, {
        "find_id": find_id,
        "decision": decision,
        "status": UI_TO_DB[decision],
        "decided_at": decided_at.isoformat(),
        "maker": "queued" if decision == "approved" else "not_requested",
    })


__all__ = ["handler", "parse_request", "respond", "UI_TO_DB"]
