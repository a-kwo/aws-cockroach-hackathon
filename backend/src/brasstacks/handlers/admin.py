"""The operator view's data, and the gate in front of it.

Every other read in this system is scoped to one tenant by the caller's session.
This one is not: it returns every active business's finds, revenue figures and
business facts in a single response, because that is what an operator console
is for. It is therefore the one endpoint where getting authorisation wrong
exposes everybody rather than somebody.

Two properties worth stating plainly:

* **Hiding the tab is not a gate.** The page can conceal the Memory Engine from
  a non-admin all it likes; anyone can open devtools and call this URL. The
  refusal has to live here, and it does.
* **The admin flag is read from the account on every request**, never copied
  onto the session at login. Revoking admin therefore takes effect on the next
  call rather than in two weeks when the session happens to expire.

Read-only by construction: it runs the same `load_workspaces` SQL the owner
board uses, with a different set of business ids. No model is involved, which is
why the response says so.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.profile_schema import ensure_profile_schema
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Cache-Control": "no-store",
}

#: How many tenants one operator response may carry. A console that quietly
#: truncates is worse than one that says it did, so the count is reported.
MAX_TENANTS = 200


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS),
            "body": json.dumps(body)}


def list_workspaces(event: Any, *, repo: Any, load: Any,
                    now: datetime | None = None) -> dict[str, Any]:
    """Every active tenant, for an operator. Dependencies handed in.

    `load` takes a sequence of business ids and returns workspace dicts — the
    same `load_workspaces` the owner board uses, called with a different set.
    """
    moment = now or datetime.now(timezone.utc)

    token = bearer_token(event)
    account = repo.account_for_session(
        token_fingerprint(token), now=moment) if token else None
    if account is None:
        return respond(401, {"error": "sign in first"})

    if not account.get("is_admin"):
        # A valid session that does not entitle the holder to anyone else's
        # data. Deliberately distinct from 401: they are signed in, and telling
        # them so is not a leak — every owner knows their own account exists.
        return respond(403, {"error": "this view is for operators"})

    business_ids = repo.active_business_ids(limit=MAX_TENANTS)
    workspaces = load(business_ids) if business_ids else []

    return respond(200, {
        "workspaces": workspaces,
        "tenants": len(workspaces),
        # Same disclosure the owner-facing endpoint makes: this is SQL over
        # CockroachDB with no model in the loop.
        "source": "cockroachdb",
        "readMode": "sql-only",
        "modelTokens": 0,
        "truncated": len(business_ids) >= MAX_TENANTS,
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
    from brasstacks.workflow_snapshot import load_workspaces

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        ensure_profile_schema(conn)
        return list_workspaces(
            event,
            repo=PostgresRepository(conn),
            load=lambda ids: load_workspaces(conn, ids),
        )


__all__ = ["handler", "list_workspaces", "respond", "MAX_TENANTS"]
