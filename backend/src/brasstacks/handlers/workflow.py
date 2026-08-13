"""Live operator workflow state for the Memory Engine.

The page still ships with a CockroachDB export as a fast, failure-tolerant first
paint. This GET endpoint is the authoritative overlay: it makes decisions from
other devices, later Maker artifacts, Meter verdicts, and current agent runs
visible without rebuilding the site.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID


from brasstacks.config import Settings
from brasstacks.profile_schema import ensure_profile_schema
from brasstacks.secrets import hydrate_environment
from brasstacks.workflow_snapshot import load_workspaces

POLL_AFTER_SECONDS = 15


def get_psycopg():
    """Import the database driver only on the live path.

    Unit tests and static-site work do not need platform-specific psycopg wheels.
    """
    import psycopg

    return psycopg


CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,If-None-Match,Authorization",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Access-Control-Expose-Headers": "ETag",
    "Cache-Control": "no-cache, max-age=0",
}


def respond(
    status: int,
    body: dict[str, Any] | None = None,
    *,
    etag: str | None = None,
) -> dict[str, Any]:
    headers = dict(CORS_HEADERS)
    if etag:
        headers["ETag"] = etag
    return {
        "statusCode": status,
        "headers": headers,
        "body": "" if status == 304 else json.dumps(body or {}, separators=(",", ":")),
    }


def configured_business_ids(
    settings: Any,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return the tenant allowlist; never accept an arbitrary request id."""
    source = os.environ if env is None else env
    raw = str(source.get("BRASSTACKS_OPERATOR_BUSINESS_IDS") or "").strip()
    candidates = [part.strip() for part in raw.split(",") if part.strip()]
    if not candidates and getattr(settings, "business_id", None):
        candidates = [str(settings.business_id)]
    if not candidates:
        raise ValueError(
            "BRASSTACKS_OPERATOR_BUSINESS_IDS or BRASSTACKS_BUSINESS_ID is required"
        )

    result = []
    seen = set()
    for candidate in candidates:
        try:
            canonical = str(UUID(candidate))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(
                "BRASSTACKS_OPERATOR_BUSINESS_IDS must contain UUID values"
            ) from None
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return tuple(result)


def _request_header(event: Any, name: str) -> str | None:
    headers = (event or {}).get("headers") or {}
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _etag(workspaces: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        workspaces,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f'"{hashlib.sha256(canonical).hexdigest()}"'


def _method(event: Any) -> str:
    request = (event or {}).get("requestContext") or {}
    return str(
        (request.get("http") or {}).get("method")
        or (event or {}).get("httpMethod")
        or "GET"
    ).upper()


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    if _method(event) == "OPTIONS":
        return respond(204, {})

    try:
        hydrate_environment()
        settings = Settings.load()
    except (ValueError, RuntimeError) as exc:
        # The detail names parameter paths and environment variables — a map
        # of the deployment. It goes to the log; the caller gets a sentence.
        print(f"workflow configuration failed: {exc}")
        return respond(500, {"error": "the workflow service is not configured"})

    from datetime import datetime, timezone

    from brasstacks.auth import token_fingerprint
    from brasstacks.handlers.login import bearer_token

    # The tenant comes from the caller's session, not from configuration and
    # never from the request. An allowlist in Parameter Store was right while
    # there was one seeded tenant; with businesses signing themselves up it
    # would show every owner the same board — someone else's.
    token = bearer_token(event)
    if not token:
        return respond(401, {"error": "sign in first"})

    driver = get_psycopg()
    try:
        with driver.connect(settings.cockroach_url, autocommit=True) as conn:
            from brasstacks.repository_pg import PostgresRepository

            ensure_profile_schema(conn)
            business_id = PostgresRepository(conn).business_for_session(
                token_fingerprint(token), now=datetime.now(timezone.utc))
            if business_id is None:
                # Expired, unknown, or registered without finishing onboarding.
                return respond(401, {"error": "sign in first"})
            business_ids = [business_id]
            # One transaction makes the several set-based reads one coherent
            # CockroachDB snapshot while preserving autocommit for the caller.
            transaction = getattr(conn, "transaction", None)
            if callable(transaction):
                with transaction():
                    workspaces = load_workspaces(conn, business_ids)
            else:  # test doubles and unusual drivers
                workspaces = load_workspaces(conn, business_ids)
    except driver.Error:
        return respond(503, {"error": "workflow state could not be read"})

    etag = _etag(workspaces)
    if _request_header(event, "If-None-Match") == etag:
        return respond(304, etag=etag)

    return respond(200, {
        "source": "cockroachdb",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "pollAfterSeconds": POLL_AFTER_SECONDS,
        # This endpoint is a read projection, not an agent turn. Keeping that
        # explicit lets the operator UI distinguish dashboard freshness from
        # model spend: the refresh consumes database reads and zero LLM tokens.
        "readMode": "sql-only",
        "modelTokens": 0,
        "workspaces": workspaces,
    }, etag=etag)


__all__ = [
    "handler", "respond", "configured_business_ids", "POLL_AFTER_SECONDS",
]
