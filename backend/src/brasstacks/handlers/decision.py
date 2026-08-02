"""Owner decisions from the For You feed.

The board may ship a CockroachDB snapshot for fast first paint, but Do it / Pass
are live writes. Every write is resolved from the caller's authenticated session
and scoped to that business. The browser never gets to choose a tenant id.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.maker_dispatch import MAKER_FUNCTION_VAR, dispatch_maker
from brasstacks.repository import RepositoryError
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Cache-Control": "no-store",
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


def record_decision(
    event: Any,
    *,
    repo: Any,
    invoker: Any | None = None,
    maker_function: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authorize and persist one owner decision.

    The business id is taken from the session row. This is deliberately the
    same tenant boundary used by /workflow and /run; using the deployment's
    BRASSTACKS_BUSINESS_ID here made every newer owner write against the seeded
    demo tenant and produced the misleading "inaccessible find" error.
    """
    try:
        find_id, decision = parse_request(event)
    except ValueError as exc:
        return respond(400, {"error": str(exc)})

    moment = now or datetime.now(timezone.utc)
    token = bearer_token(event)
    account = repo.account_for_session(
        token_fingerprint(token), now=moment
    ) if token else None
    if account is None:
        return respond(401, {"error": "sign in first"})

    business_id = account.get("business_id")
    if not business_id:
        return respond(409, {"error": "finish setting up your business first"})

    try:
        transition = repo.set_find_status(
            find_id,
            status=UI_TO_DB[decision],
            decided_at=moment,
            business_id=business_id,
        )
    except RepositoryError as exc:
        # Do not reveal whether a UUID belongs to another tenant. The repository
        # scopes its lookup before producing this message.
        return respond(409, {"error": str(exc)})

    maker = "not_requested"
    if decision == "approved":
        maker = dispatch_maker(
            invoker=invoker,
            function_name=maker_function,
            business_id=business_id,
            find_id=find_id,
        )

    return respond(200, {
        "find_id": find_id,
        "decision": decision,
        "status": transition.status,
        "previous_status": transition.previous_status,
        "changed": transition.changed,
        "decided_at": transition.decided_at.isoformat(),
        "previous_decided_at": (
            transition.previous_decided_at.isoformat()
            if transition.previous_decided_at else None
        ),
        "maker": maker,
    })


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    method = str(((event or {}).get("requestContext") or {})
                 .get("http", {}).get("method") or "").upper()
    if method == "OPTIONS":
        return respond(204, {})

    hydrate_environment()
    settings = Settings.load()

    import boto3
    import psycopg
    from brasstacks.repository_pg import PostgresRepository

    try:
        with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
            return record_decision(
                event,
                repo=PostgresRepository(conn),
                invoker=boto3.client("lambda"),
                maker_function=os.environ.get(MAKER_FUNCTION_VAR),
            )
    except psycopg.Error:
        return respond(503, {"error": "decision could not be saved"})


__all__ = [
    "handler", "record_decision", "parse_request", "respond", "UI_TO_DB",
    "MAKER_FUNCTION_VAR",
]
