"""Passwords and sessions for business owners.

The rules this file exists to keep:

* **The password is never stored, logged, or recoverable.** Only an scrypt
  digest with a per-hash salt, in a form that names its own parameters so the
  cost can be raised later without invalidating every account.
* **A database read is not enough to impersonate anyone.** Session tokens are
  random and only their SHA-256 is stored, so a dump of `owner_session` yields
  nothing that can be presented as a token.
* **Comparisons are constant-time**, because `==` on a digest leaks how much of
  it matched.

Deliberately no new dependency. `hashlib.scrypt` has been in the standard
library since 3.6 and the Lambda image installs only `psycopg`, `anthropic` and
`boto3`; a password KDF is not worth widening that, and a hand-rolled one would
be far worse than either.

What this is **not**: there is no email verification, no password reset, and no
lockout after repeated failures. Those are real gaps, they are written down in
the README rather than implied away, and none of them is a reason to store a
password badly in the meantime.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

#: scrypt cost. n=16384 with r=8 keeps a single hash around 60-100ms on Lambda —
#: slow enough to make offline guessing expensive, fast enough that a login does
#: not feel broken. Stored alongside the digest so this can be raised later and
#: old hashes still verify.
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16

#: Long enough that guessing is hopeless, short enough to paste. 32 bytes of
#: urandom is 256 bits before base64.
TOKEN_BYTES = 32

SESSION_TTL = timedelta(days=14)

#: No minimum length, by the owner's decision on 2026-08-02. A password must be
#: non-empty and nothing more. Recorded rather than silently dropped: it means a
#: one-character password is accepted on an account holding a business's revenue
#: figures, and scrypt's cost per guess does not help against a space that small.
#: Reinstating a floor is one constant and one branch.
#:
#: The maximum is not a strength rule and stays. scrypt will happily hash a
#: megabyte and take its time doing it, so this bounds what a signup can make
#: the function spend.
MAX_PASSWORD_CHARS = 256

MIN_USERNAME_CHARS = 2
MAX_USERNAME_CHARS = 64
USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]+$")


class AuthError(ValueError):
    """A credential the caller can fix. Safe to show them."""


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a password for storage.

    Returns `scrypt$n$r$p$salt$digest`, all hex. Self-describing so a later
    increase in cost can rehash on next login rather than locking everyone out.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_N * SCRYPT_R * 256,
    )
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                     salt.hex(), digest.hex()])


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. False for anything malformed.

    A corrupt or unrecognised row must fail the login rather than raise: a 500
    on the login endpoint is both a worse experience and a signal to whoever is
    probing it.
    """
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt, expected = bytes.fromhex(raw_salt), bytes.fromhex(raw_digest)
    except (AttributeError, ValueError):
        return False

    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=n, r=r, p=p, dklen=len(expected), maxmem=n * r * 256,
        )
    except ValueError:
        return False

    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# What a caller is allowed to choose
# ---------------------------------------------------------------------------

def normalise_username(username: str) -> str:
    """Casefolded and stripped. Two spellings of one name are one person."""
    return (username or "").strip().casefold()


def validate_username(username: str) -> str:
    name = normalise_username(username)
    if not MIN_USERNAME_CHARS <= len(name) <= MAX_USERNAME_CHARS:
        raise AuthError(
            f"username must be {MIN_USERNAME_CHARS}-{MAX_USERNAME_CHARS} characters"
        )
    if not USERNAME_PATTERN.match(name):
        raise AuthError(
            "username may use letters, numbers, dots, dashes and underscores"
        )
    return name


def validate_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise AuthError("a password is required")
    if len(password) > MAX_PASSWORD_CHARS:
        raise AuthError(f"password must be under {MAX_PASSWORD_CHARS} characters")
    return password


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def token_fingerprint(token: str) -> str:
    """What goes in the database. The token itself never does."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session_token(*, now: datetime) -> tuple[str, str, datetime]:
    """Mint a session.

    Returns `(token, fingerprint, expires_at)`. The token is handed to the
    owner once and never again — only the fingerprint is stored, so losing the
    database does not hand anyone a working session.

    `now` is injected rather than read from a clock, so expiry is testable.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, token_fingerprint(token), now + SESSION_TTL


__all__ = [
    "MAX_PASSWORD_CHARS",
    "SESSION_TTL",
    "AuthError",
    "hash_password",
    "issue_session_token",
    "normalise_username",
    "token_fingerprint",
    "validate_password",
    "validate_username",
    "verify_password",
]
