"""Step one of signup: create the account, and sign the owner in.

Signup is two pages — credentials, then the business profile — and this is the
first. It exists in this shape for one reason: **the password must not survive a
page navigation.**

The obvious alternative is one page that collects everything and posts it at the
end, or two pages with the password parked in `sessionStorage` between them. The
second puts a plaintext password in browser storage for as long as the owner
takes to fill in a form, readable from any devtools console, and recoverable
afterwards if the tab is never closed. Creating the account first avoids the
question: the password is posted here, hashed, and forgotten, and the owner
arrives at the profile page holding a session token instead.

The business does not exist yet, so the account is created with no business and
`onboarding` attaches one when the profile is submitted. That is why
`owner_account.business_id` and `owner_session.business_id` are nullable.

The invite code lives here rather than on the profile page: this is the endpoint
that lets a stranger create rows, so it is the one that has to be gated.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from brasstacks.auth import (
    AuthError,
    hash_password,
    issue_session_token,
    validate_password,
    validate_username,
)
from brasstacks.config import Settings
from brasstacks.handlers.login import CORS_HEADERS, respond
from brasstacks.secrets import hydrate_environment


def register(event: Any, *, repo: Any, invite_code: str | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """Create an account and return a session token.

    Dependencies handed in, so the logic is testable without AWS or a database.
    """
    moment = now or datetime.now(timezone.utc)

    raw = (event or {}).get("body")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(payload, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        return respond(400, {"error": "send a JSON body"})

    # Before anything is created, so a rejected attempt leaves no account.
    if invite_code:
        if str(payload.get("inviteCode") or "").strip() != invite_code:
            return respond(403, {"error": "an invite code is required"})

    try:
        username = validate_username(str(payload.get("username") or ""))
        password = validate_password(payload.get("password"))
    except AuthError as e:
        return respond(400, {"error": str(e)})

    if repo.find_account(username) is not None:
        # Unlike login, this one has to distinguish: the owner cannot pick a
        # different username without being told this one is gone. Username
        # availability is inherently public on any site with signup.
        return respond(409, {"error": f"the username {username!r} is taken"})

    account_id = repo.create_account(None, username=username,
                                     password_hash=hash_password(password))

    token, fingerprint, expires_at = issue_session_token(now=moment)
    repo.create_session(fingerprint, business_id=None, account_id=account_id,
                        expires_at=expires_at)

    return respond(201, {
        "token": token,
        "username": username,
        # Null until the profile page creates one. The client uses this to know
        # whether to send the owner to onboarding or to the board.
        "business_id": None,
        "expires_at": expires_at.isoformat(),
    })


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        return register(
            event,
            repo=PostgresRepository(conn),
            invite_code=os.environ.get("BRASSTACKS_INVITE_CODE") or None,
        )


__all__ = ["handler", "register", "CORS_HEADERS"]
