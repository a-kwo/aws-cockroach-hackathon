"""Logging an owner in, as a Lambda behind API Gateway.

Unauthenticated by definition and the way into every tenant's data, so the
refusals are the interesting part:

* **A wrong password and an unknown username return the same thing.** Different
  responses would tell an attacker which usernames exist, which is the first
  half of the work.
* **The password is verified even when the user does not exist**, against a
  throwaway hash. Skipping the KDF for unknown usernames makes them measurably
  faster to probe, which leaks the same fact through timing instead of text.
* **Nothing sensitive is returned or logged.** The token is handed over once;
  only its fingerprint is stored.

Not here, and written down in the README rather than implied away: no lockout
after repeated failures. API Gateway throttles to 2 rps and scrypt makes each
guess expensive, but neither is a lock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from brasstacks.auth import (
    hash_password,
    issue_session_token,
    normalise_username,
    token_fingerprint,
    verify_password,
)
from brasstacks.config import Settings
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}

#: One message for every failure. See the module docstring.
REFUSAL = {"error": "that username or password is not right"}

#: Verified against when the username does not exist, so an unknown user costs
#: the same scrypt work as a known one. The value is irrelevant; only the time
#: it takes to reject it matters.
_DECOY_HASH = hash_password("decoy for constant work on unknown usernames")


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS),
            "body": json.dumps(body)}


def bearer_token(event: Any) -> str | None:
    headers = (event or {}).get("headers") or {}
    for key, value in headers.items():
        if str(key).lower() == "authorization":
            raw = str(value or "").strip()
            if raw.lower().startswith("bearer "):
                return raw[7:].strip() or None
            return raw or None
    return None


def login(event: Any, *, repo: Any, now: datetime | None = None) -> dict[str, Any]:
    """Exchange credentials for a session token.

    Dependencies handed in, like every other agent here, so the logic is
    testable without AWS, a database or a network.
    """
    moment = now or datetime.now(timezone.utc)

    raw = (event or {}).get("body")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(payload, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        return respond(400, {"error": "send a JSON body"})

    username = normalise_username(str(payload.get("username") or ""))
    password = payload.get("password")
    if not username or not isinstance(password, str) or not password:
        return respond(400, {"error": "username and password are both required"})

    account = repo.find_account(username)
    # Verify regardless. An early return for an unknown username would skip the
    # KDF and make non-existent users measurably faster to probe.
    #
    # The decoy also covers accounts with no password at all — the ones created
    # by Sign in with Google, where `password_hash` is NULL. `verify_password`
    # already refuses a null hash, but it refuses it *instantly*, which would
    # make "this username signs in with Google" measurable from outside.
    stored = (account or {}).get("password_hash") or _DECOY_HASH
    if not verify_password(password, stored) or account is None:
        return respond(401, dict(REFUSAL))

    token, fingerprint, expires_at = issue_session_token(now=moment)
    repo.create_session(fingerprint, business_id=account["business_id"],
                        account_id=account["id"], expires_at=expires_at)

    return respond(200, {
        "token": token,
        "business_id": account["business_id"],
        "expires_at": expires_at.isoformat(),
        # So the page knows whether to offer the operator view at all. Purely
        # cosmetic — /admin/workspaces checks the account itself on every
        # request, because a hidden tab is not a gate.
        "is_admin": bool(account.get("is_admin")),
    })


def logout(event: Any, *, repo: Any) -> dict[str, Any]:
    """End a session. Idempotent — logging out twice is not an error, and
    reporting "no such session" would confirm which tokens were once real."""
    token = bearer_token(event)
    if token:
        repo.delete_session(token_fingerprint(token))
    return respond(200, {"ok": True})


def _with_repo(event: Any, action: str) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)
        return login(event, repo=repo) if action == "login" else logout(event, repo=repo)


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    return _with_repo(event, "login")


def logout_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    return _with_repo(event, "logout")


__all__ = ["handler", "logout_handler", "login", "logout", "respond",
           "bearer_token"]
