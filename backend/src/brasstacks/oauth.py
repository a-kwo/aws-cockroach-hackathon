"""Sign in with Google, on the standard library.

The authorization code flow, and nothing else. What this file is responsible for:

* **The state parameter.** It is the only thing that survives the trip to Google
  and back, so it is where the invite decision has to be carried — signed with
  HMAC-SHA256 so the browser cannot edit it, and time-limited so a stale one is
  not a permanent pass.
* **Refusing the ID token we should not accept.** Wrong audience, wrong issuer,
  expired, no subject, unverified email.
* **Inventing a username** for someone who arrived without choosing one, in a
  shape the rest of the system already accepts.

Deliberately no new dependency, for the same reason `auth.py` hashes passwords
with `hashlib.scrypt`: the Lambda image installs `psycopg`, `anthropic`, `boto3`
and `certifi`, and an OAuth client library is not worth widening that for one
provider and one flow. Everything here is `hmac`, `json`, `base64` and
`urllib.parse`; the single outbound call borrows `httpx` lazily, the same way
`signals.py` does, so the unit suite never needs it.

**On not verifying the ID token's signature.** We do not, and that is safe only
because of where the token comes from: `exchange_code` receives it in the body
of a server-to-server POST to Google's token endpoint, over TLS, authenticated
with our client secret. Nothing in that path is attacker-controlled, so there is
no forged token to catch. The claim checks in `identity_from_id_token` are
defence in depth against a misconfigured client, not the primary control.

If an ID token ever arrives by any other route — from the browser, from a
redirect fragment, from a mobile client — that argument collapses entirely and
the signature must be verified against Google's JWKS before anything else. There
is no code path here that does that, so do not add one that skips it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from brasstacks.auth import (
    MAX_USERNAME_CHARS,
    MIN_USERNAME_CHARS,
    normalise_username,
)

#: The value stored in `owner_account.auth_provider` for these accounts.
PROVIDER = "google"

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

#: Identity and nothing else. Every extra scope is another line in the consent
#: screen the owner has to read and another thing the token can do if it leaks.
SCOPE = "openid email profile"

#: Google has used both spellings over the years and still does.
ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

#: How long the signed state is good for — long enough to type a password into
#: Google and pick an account, short enough that a link left in a chat window
#: stops working.
STATE_TTL = timedelta(minutes=10)

#: How long the one-time handoff code is good for. The page redeems it on load,
#: so this only has to cover a redirect.
HANDOFF_TTL = timedelta(minutes=2)
HANDOFF_BYTES = 32

#: Allowed clock skew when checking `exp`. Google's clock and Lambda's are both
#: NTP-disciplined; this is for the seconds, not the minutes.
CLOCK_LEEWAY = timedelta(minutes=5)

_DISALLOWED = re.compile(r"[^a-z0-9._-]")
_ALPHANUMERIC = re.compile(r"[a-z0-9]")


class OAuthError(ValueError):
    """A sign-in that cannot proceed. The message is safe to show."""


# ---------------------------------------------------------------------------
# base64url without padding, which is what JWT and everything around it uses
# ---------------------------------------------------------------------------

def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# State: what we hand Google and then have to trust back
# ---------------------------------------------------------------------------

def _signature(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"),
                    hashlib.sha256).hexdigest()


def sign_state(claims: dict[str, Any], *, secret: str, now: datetime) -> str:
    """Sign a small claim set into an opaque, tamper-evident string.

    A nonce goes in unconditionally, so two sign-ins started in two tabs produce
    different states and neither can be replayed as the other.
    """
    body = dict(claims)
    body["iat"] = int(now.timestamp())
    body["nonce"] = secrets.token_urlsafe(16)
    payload = _b64u_encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_signature(payload, secret)}"


def verify_state(state: Any, *, secret: str, now: datetime,
                 ttl: timedelta = STATE_TTL) -> dict[str, Any]:
    """Return the claims, or raise. Never returns unverified content."""
    if not isinstance(state, str) or state.count(".") != 1:
        raise OAuthError("that sign-in link is not valid")

    payload, signature = state.split(".", 1)
    # compare_digest, not ==: a short-circuiting comparison on a MAC leaks how
    # much of it matched, one byte at a time.
    if not hmac.compare_digest(_signature(payload, secret), signature):
        raise OAuthError("that sign-in link is not valid")

    try:
        claims = json.loads(_b64u_decode(payload))
        issued = datetime.fromtimestamp(int(claims["iat"]), timezone.utc)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OAuthError("that sign-in link is not valid") from exc
    if not isinstance(claims, dict):
        raise OAuthError("that sign-in link is not valid")

    if issued + ttl <= now:
        raise OAuthError("that sign-in link has expired — please try again")
    return claims


# ---------------------------------------------------------------------------
# Leg one: where we send the browser
# ---------------------------------------------------------------------------

def build_authorize_url(*, client_id: str, redirect_uri: str,
                        state: str) -> str:
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        # So a second sign-in on a shared machine offers the chooser rather than
        # silently reusing whoever is already signed in to Google.
        "prompt": "select_account",
    })
    return f"{AUTHORIZE_ENDPOINT}?{query}"


# ---------------------------------------------------------------------------
# Leg two: the code, exchanged server to server
# ---------------------------------------------------------------------------

Transport = Callable[[str, dict[str, str]], dict[str, Any]]


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    # Lazy, like signals.py, so the unit suite never needs the dependency.
    import httpx

    response = httpx.post(url, data=form, timeout=15.0)
    response.raise_for_status()
    return response.json()


def exchange_code(code: str, *, client_id: str, client_secret: str,
                  redirect_uri: str,
                  transport: Transport | None = None) -> str:
    """Trade the authorization code for an ID token. Returns the raw JWT.

    Only the ID token is kept. The access token Google also returns would let us
    call its APIs on the owner's behalf, and we have no reason to — storing one
    would be taking a capability we do not use.
    """
    payload = (transport or _post_form)(TOKEN_ENDPOINT, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })

    id_token = (payload or {}).get("id_token")
    if not id_token:
        raise OAuthError("Google did not complete that sign-in")
    return str(id_token)


# ---------------------------------------------------------------------------
# Leg three: who the token says this is
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoogleIdentity:
    #: Google's stable identifier. This, not the email, is the account key:
    #: people change their email address and keep being themselves.
    subject: str
    email: str | None
    display_name: str | None


def identity_from_id_token(id_token: Any, *, client_id: str,
                           now: datetime) -> GoogleIdentity:
    """Read and check an ID token. See the module docstring on the signature."""
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        raise OAuthError("Google did not complete that sign-in")

    try:
        claims = json.loads(_b64u_decode(id_token.split(".")[1]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OAuthError("Google did not complete that sign-in") from exc
    if not isinstance(claims, dict):
        raise OAuthError("Google did not complete that sign-in")

    if claims.get("iss") not in ISSUERS:
        raise OAuthError("that sign-in did not come from Google")

    # The audience check is what stops a token issued to some *other* Google
    # application from signing someone in here.
    audience = claims.get("aud")
    if not audience or audience != client_id:
        raise OAuthError("that sign-in was not meant for this app")

    try:
        expires = datetime.fromtimestamp(int(claims["exp"]), timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise OAuthError("that sign-in has expired") from exc
    if expires + CLOCK_LEEWAY <= now:
        raise OAuthError("that sign-in has expired — please try again")

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise OAuthError("Google did not identify that account")

    email = (claims.get("email") or "").strip() or None
    # An unverified address is one Google will hand over without the person
    # having proved they own it. Taking it would let someone sign up as anybody.
    if email and not claims.get("email_verified"):
        raise OAuthError("please verify your email address with Google first")

    name = (claims.get("name") or "").strip() or None
    return GoogleIdentity(subject=subject, email=email, display_name=name)


# ---------------------------------------------------------------------------
# The username nobody chose
# ---------------------------------------------------------------------------

def _fallback_username(subject: str) -> str:
    digits = _DISALLOWED.sub("", normalise_username(subject)) or "account"
    return f"{PROVIDER}.{digits}"[:MAX_USERNAME_CHARS]


def suggest_username(email: str | None, *, subject: str,
                     taken: Callable[[str], bool]) -> str:
    """A free username derived from the email address, or from the subject.

    These names never pass through the signup form, so nothing else in the
    system validates them — every return value here has to be one
    `validate_username` already accepts.
    """
    local = normalise_username(str(email or "").split("@")[0])
    base = _DISALLOWED.sub("", local).strip(".-_")

    # Too short to be a username, or nothing but punctuation. Either way there
    # is no name in here worth keeping.
    if len(base) < MIN_USERNAME_CHARS or not _ALPHANUMERIC.search(base):
        base = _fallback_username(subject)
    base = base[:MAX_USERNAME_CHARS]

    if not taken(base):
        return base

    # Someone already has it. Count up rather than appending randomness, so the
    # second Sam gets `sam-2` and not a name they will never recognise.
    for suffix in range(2, 1000):
        tail = f"-{suffix}"
        candidate = f"{base[:MAX_USERNAME_CHARS - len(tail)]}{tail}"
        if not taken(candidate):
            return candidate

    # A thousand collisions on one name is not a real signup pattern, but
    # returning something taken would be a 500 at the database.
    tail = f"-{secrets.token_hex(4)}"
    return f"{base[:MAX_USERNAME_CHARS - len(tail)]}{tail}"


# ---------------------------------------------------------------------------
# The one-time code that stands in for a session across a redirect
# ---------------------------------------------------------------------------

def issue_handoff_code(*, now: datetime) -> tuple[str, str, datetime]:
    """Mint a handoff code. Returns `(code, fingerprint, expires_at)`.

    Same discipline as `issue_session_token`: only the fingerprint is stored, so
    a read of the table does not yield anything redeemable.
    """
    from brasstacks.auth import token_fingerprint

    code = secrets.token_urlsafe(HANDOFF_BYTES)
    return code, token_fingerprint(code), now + HANDOFF_TTL


__all__ = [
    "AUTHORIZE_ENDPOINT",
    "CLOCK_LEEWAY",
    "HANDOFF_TTL",
    "PROVIDER",
    "SCOPE",
    "STATE_TTL",
    "TOKEN_ENDPOINT",
    "GoogleIdentity",
    "OAuthError",
    "build_authorize_url",
    "exchange_code",
    "identity_from_id_token",
    "issue_handoff_code",
    "sign_state",
    "suggest_username",
    "verify_state",
]
