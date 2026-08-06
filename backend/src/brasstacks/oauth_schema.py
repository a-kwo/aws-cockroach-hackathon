"""Idempotent bootstrap for Sign in with Google.

``db/schema.sql`` remains the canonical migration. This additive bootstrap is
the same safety net ``profile_schema`` provides: the OAuth Lambda can receive a
request before an operator has run the migration by hand, and the first person
to click the button should not be the one who finds out.

It matters more here than usual. The columns below are the difference between
creating an account and raising an undefined-column error *after* the round trip
to Google — by which point the person has already approved the consent screen.
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_READY = False

# One statement at a time in autocommit, for the reason profile_schema gives:
# CockroachDB can leave mixed DDL/DML transactions partially committed when one
# schema change fails, which is the opposite of what a safety net needs.
OAUTH_SCHEMA_STATEMENTS = (
    # 'password' for everyone who typed one, 'google' for everyone who did not.
    # The default is what makes this additive — every account that already
    # exists got here with a password.
    "ALTER TABLE owner_account ADD COLUMN IF NOT EXISTS auth_provider STRING NOT NULL DEFAULT 'password'",
    # Google's `sub`. This and not the email is the account key: people change
    # their address, and owner_account.email is not unique anyway.
    "ALTER TABLE owner_account ADD COLUMN IF NOT EXISTS provider_subject STRING",
    # An account that signs in through Google has no password to store. NULL
    # rather than a placeholder: a placeholder is a value something could hash
    # to, and NULL is the honest statement that there is nothing here.
    "ALTER TABLE owner_account ALTER COLUMN password_hash DROP NOT NULL",
    # Partial, so the many rows with a NULL subject do not collide.
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS owner_account_provider_idx "
        "ON owner_account (auth_provider, provider_subject) "
        "WHERE provider_subject IS NOT NULL"
    ),
    (
        "CREATE TABLE IF NOT EXISTS oauth_handoff ("
        "code_hash STRING PRIMARY KEY, "
        "account_id UUID NOT NULL REFERENCES owner_account(id) ON DELETE CASCADE, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
        "expires_at TIMESTAMPTZ NOT NULL, "
        "INDEX (expires_at))"
    ),
)

#: Readable migration receipt, for tests and documentation.
OAUTH_SCHEMA_SQL = ";\n".join(OAUTH_SCHEMA_STATEMENTS) + ";\n"


def ensure_oauth_schema(conn: Any) -> None:
    """Apply the additive OAuth schema once per warm execution process.

    Call before any other work on a non-autocommit connection, so toggling
    ``autocommit`` does not interrupt an open transaction.
    """
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        original_autocommit = getattr(conn, "autocommit", None)
        changed_autocommit = original_autocommit is False
        if changed_autocommit:
            conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                for statement in OAUTH_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
        finally:
            if changed_autocommit:
                conn.autocommit = False
        _READY = True


def reset_oauth_schema_cache_for_tests() -> None:
    global _READY
    _READY = False


__all__ = [
    "OAUTH_SCHEMA_SQL",
    "OAUTH_SCHEMA_STATEMENTS",
    "ensure_oauth_schema",
    "reset_oauth_schema_cache_for_tests",
]
