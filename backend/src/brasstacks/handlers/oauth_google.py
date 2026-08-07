"""Sign in with Google: the three endpoints behind the button.

    GET  /auth/google/start     invite checked, then redirect to Google
    GET  /auth/google/callback  Google comes back; account found or created
    POST /auth/google/complete  the page trades a one-time code for a session

**Why three and not two.** The callback is a browser redirect, so anything it
returns lands in a URL — and a URL goes into history, into the `Referer` of the
next request, and into whatever logs the hop. CLAUDE.md rejected capability URLs
for exactly this reason. So the callback mints no session at all: it stores a
one-time code with a two-minute life, and the landing page POSTs that back for
the real token. The token is then returned in a response body, once, the same as
`/register` and `/login`.

**Where the invite gate lives.** In two places, because the button sits on two
pages and they are not the same request.

From `/register/` it is on `start`, before the redirect, so a wrong code never
reaches Google at all — and the decision is carried across the round trip in the
signed state rather than re-read from a query parameter the browser could edit.

From `/login/` there is no code to check: an owner coming back used their invite
once, months ago, and was never asked to keep it. So `?intent=login` skips the
check on `start` and the gate moves to `callback`, which will match an existing
Google account but refuses to create one — an uninvited sign-in leaves with
`/login/?error=no-account` and nothing written. The `invited` claim in the signed
state is what carries that decision; it is never re-read from the query.

Either way the gate is a spend control: every active tenant costs a Tavily
search, ~50 embeddings and a Claude call every night, and an OAuth button that
skipped it would be a hole in the budget.

**Accounts are keyed on Google's subject, never on the email.** Two reasons: the
address on an account here is not unique — `db/schema.sql` seeds several tenants
with one shared inbox — so it identifies nobody; and matching on it would mean an
address that changed hands could inherit someone's business. A Google sign-in
therefore always yields its own account, and never adopts a password account
that happens to share an email.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from brasstacks.auth import issue_session_token, token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import CORS_HEADERS, respond
from brasstacks.oauth import (
    PROVIDER,
    OAuthError,
    build_authorize_url,
    exchange_code,
    identity_from_id_token,
    issue_handoff_code,
    sign_state,
    suggest_username,
    verify_state,
)
from brasstacks.oauth_schema import ensure_oauth_schema
from brasstacks.secrets import hydrate_environment


@dataclass(frozen=True)
class GoogleOAuthConfig:
    """Everything the flow needs from the environment.

    All five are required together. A half-configured environment is treated as
    not configured at all — see `configured` — because the alternative is a 500
    from the middle of a redirect, which reads like a bug rather than a setup
    step nobody has done yet.
    """

    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    state_secret: str | None
    frontend_url: str | None

    @property
    def configured(self) -> bool:
        return all([self.client_id, self.client_secret, self.redirect_uri,
                    self.state_secret, self.frontend_url])

    @property
    def site(self) -> str:
        return (self.frontend_url or "").rstrip("/")

    @classmethod
    def from_env(cls) -> GoogleOAuthConfig:
        return cls(
            client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or None,
            client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or None,
            redirect_uri=os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or None,
            state_secret=os.environ.get("GOOGLE_OAUTH_STATE_SECRET") or None,
            frontend_url=os.environ.get("BRASSTACKS_SITE_URL") or None,
        )


NOT_CONFIGURED = {"error": "google sign-in is not available"}


def _redirect(location: str) -> dict[str, Any]:
    return {
        "statusCode": 302,
        "headers": {
            "Location": location,
            # A cached redirect would send the next person to a stale state, or
            # replay a spent handoff code.
            "Cache-Control": "no-store",
        },
        "body": "",
    }


def _query(event: Any) -> dict[str, Any]:
    return (event or {}).get("queryStringParameters") or {}


# ---------------------------------------------------------------------------
# Leg one
# ---------------------------------------------------------------------------

#: The two directions the button can be pressed from. `register` is somebody
#: signing up and must show an invite code; `login` is somebody coming back, who
#: has none — they used theirs once and were never asked to keep it.
SIGN_UP = "register"
SIGN_IN = "login"


def _intent(event: Any) -> str:
    """Which page the button was on. Anything unrecognised is a sign-up.

    Deliberately an exact match rather than a prefix or a truthiness check: this
    value decides whether the invite gate runs, so a typo or a hand-edited URL
    must fall on the guarded side of it.
    """
    return SIGN_IN if str(_query(event).get("intent") or "").strip() == SIGN_IN \
        else SIGN_UP


def _origin(query: dict[str, Any], config: GoogleOAuthConfig,
            moment: datetime) -> str:
    """The page this sign-in started on, for sending someone back to it.

    Used only on the cancel path, where the state may well be junk and the
    right answer to that is a form rather than an error. Nothing is authorised
    on the strength of this — it picks a URL.
    """
    try:
        claims = verify_state(query.get("state"), secret=config.state_secret,
                              now=moment)
    except OAuthError:
        return "/register/"
    return "/login/" if claims.get("intent") == SIGN_IN else "/register/"


def start(event: Any, *, config: GoogleOAuthConfig,
          invite_code: str | None = None,
          now: datetime | None = None) -> dict[str, Any]:
    """Check the invite, then hand the browser to Google.

    No database work at all: nothing is created until Google comes back, so a
    stranger clicking this endpoint costs a signature and nothing else.

    A sign-in skips the invite check, because an owner returning to `/login/`
    has no code to give. That is not a hole in the gate — it moves the gate to
    `callback`, which refuses to *create* an account for a sign-in state. The
    two halves only make sense together: loosen this one without that one and
    the sign-in page becomes a free workspace, nightly model spend included.
    """
    if not config.configured:
        return respond(404, dict(NOT_CONFIGURED))

    moment = now or datetime.now(timezone.utc)
    intent = _intent(event)
    if invite_code and intent == SIGN_UP:
        if str(_query(event).get("invite") or "").strip() != invite_code:
            return respond(403, {"error": "an invite code is required"})

    # The decision travels in the signature, not in a parameter the browser can
    # edit on the way back.
    state = sign_state({"invited": intent == SIGN_UP, "intent": intent},
                       secret=config.state_secret, now=moment)
    return _redirect(build_authorize_url(
        client_id=config.client_id, redirect_uri=config.redirect_uri,
        state=state))


# ---------------------------------------------------------------------------
# Leg two
# ---------------------------------------------------------------------------

def callback(event: Any, *, repo: Any, config: GoogleOAuthConfig,
             now: datetime | None = None,
             transport: Any = None) -> dict[str, Any]:
    """Google's redirect back. Finds or creates the account, then hands the
    browser a one-time code — never a session token."""
    if not config.configured:
        return respond(404, dict(NOT_CONFIGURED))

    moment = now or datetime.now(timezone.utc)
    query = _query(event)

    # `?error=access_denied` is what Google sends when someone clicks Cancel.
    # That is a change of mind, not a failure worth a stack trace. Send them
    # back to the page they left from — read out of the state when it is
    # readable, and assumed to be sign-up when it is not, because a cancel is
    # not the place to start reporting signature errors.
    if query.get("error"):
        return _redirect(f"{config.site}{_origin(query, config, moment)}"
                         "?error=cancelled")

    try:
        claims = verify_state(query.get("state"), secret=config.state_secret,
                              now=moment)
    except OAuthError as e:
        return respond(400, {"error": str(e)})

    # Only ever what the signature vouched for. A sign-in was never asked for an
    # invite code, so it may match an existing account and nothing more.
    invited = claims.get("invited") is True
    origin = "/login/" if claims.get("intent") == SIGN_IN else "/register/"

    code = str(query.get("code") or "").strip()
    if not code:
        return respond(400, {"error": "that sign-in did not complete"})

    try:
        identity = identity_from_id_token(
            exchange_code(code, client_id=config.client_id,
                          client_secret=config.client_secret,
                          redirect_uri=config.redirect_uri,
                          transport=transport),
            client_id=config.client_id, now=moment)
    except OAuthError as e:
        return respond(400, {"error": str(e)})

    account = repo.find_account_by_provider(PROVIDER, identity.subject)
    if account is None and not invited:
        # The sign-in direction, with nobody to sign in. Creating one here is
        # what the invite gate exists to stop: a workspace runs a Tavily search,
        # ~50 embeddings and a Claude call every night it stays active. Back to
        # the sign-in page with a reason, having written nothing.
        return _redirect(f"{config.site}/login/?error=no-account")

    if account is None:
        username = suggest_username(
            identity.email, subject=identity.subject,
            taken=lambda name: repo.find_account(name) is not None)
        account_id = repo.create_account(
            None, username=username, password_hash=None,
            auth_provider=PROVIDER, provider_subject=identity.subject,
            email=identity.email, display_name=identity.display_name)
    else:
        account_id = account["id"]

    handoff, fingerprint, expires_at = issue_handoff_code(now=moment)
    repo.create_handoff(fingerprint, account_id=account_id,
                        expires_at=expires_at)

    # In the fragment, not the query: a fragment is not sent to the server on
    # the next request and does not reach access logs.
    return _redirect(f"{config.site}/auth/complete/#code={quote(handoff)}")


# ---------------------------------------------------------------------------
# Leg three
# ---------------------------------------------------------------------------

def complete(event: Any, *, repo: Any,
             now: datetime | None = None) -> dict[str, Any]:
    """Redeem the one-time code for a session token."""
    moment = now or datetime.now(timezone.utc)

    raw = (event or {}).get("body")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(payload, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        return respond(400, {"error": "send a JSON body"})

    code = str(payload.get("code") or "").strip()
    account_id = (repo.consume_handoff(token_fingerprint(code), now=moment)
                  if code else None)
    if not account_id:
        # One message for expired, spent and never-issued alike. Telling them
        # apart would confirm which codes were once real.
        return respond(401, {"error": "that sign-in has expired — please try again"})

    account = repo.find_account_by_id(account_id)
    if account is None:
        return respond(401, {"error": "that sign-in has expired — please try again"})

    token, fingerprint, expires_at = issue_session_token(now=moment)
    repo.create_session(fingerprint, business_id=account["business_id"],
                        account_id=account_id, expires_at=expires_at)

    return respond(200, {
        "token": token,
        "username": account["username"],
        # Null until onboarding creates one. The page uses this to decide
        # between the profile wizard and the board.
        "business_id": account["business_id"],
        "expires_at": expires_at.isoformat(),
        "is_admin": bool(account.get("is_admin")),
    })


# ---------------------------------------------------------------------------
# Lambda entry points
# ---------------------------------------------------------------------------

def start_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    # No database: see `start`.
    return start(event, config=GoogleOAuthConfig.from_env(),
                 invite_code=os.environ.get("BRASSTACKS_INVITE_CODE") or None)


def _with_repo(event: Any, action: str) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        # Before any other work: the columns this flow writes may not exist yet
        # on a cluster nobody has migrated, and finding that out *after* the
        # round trip to Google means the person already approved the consent
        # screen for nothing.
        ensure_oauth_schema(conn)
        repo = PostgresRepository(conn)
        if action == "callback":
            return callback(event, repo=repo,
                            config=GoogleOAuthConfig.from_env())
        return complete(event, repo=repo)


def callback_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    return _with_repo(event, "callback")


def complete_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    return _with_repo(event, "complete")


def request_path(event: Any) -> str:
    raw = (event or {}).get("rawPath")
    if not raw:
        http = ((event or {}).get("requestContext") or {}).get("http") or {}
        raw = http.get("path")
    return str(raw or "")


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    """One Lambda, three routes, dispatched on path.

    Together rather than split because they are one flow with one config, and
    three functions would be three cold starts and three copies of the same
    image. Dispatched rather than merged because `start` needs no database and
    should not open a connection to find that out.
    """
    path = request_path(event).rstrip("/")
    if path.endswith("/start"):
        return start_handler(event, context)
    if path.endswith("/callback"):
        return callback_handler(event, context)
    if path.endswith("/complete"):
        return complete_handler(event, context)
    return respond(404, {"error": "no such endpoint"})


__all__ = ["CORS_HEADERS", "GoogleOAuthConfig", "callback", "callback_handler",
           "complete", "complete_handler", "handler", "request_path", "start",
           "start_handler"]
