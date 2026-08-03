"""Idempotent bootstrap for editable owner/business profiles.

``db/schema.sql`` remains the canonical migration. This additive bootstrap is a
safety net for serverless deployments where the new Lambda code may receive a
request before an operator has run the migration manually.

The three current demo businesses intentionally share one review mailbox while
Maker email is being exercised end to end. New signups persist the address the
owner entered; the legacy backfill touches only business-bound accounts whose
email is still missing.
"""

from __future__ import annotations

import threading
from typing import Any

LEGACY_OWNER_EMAIL = "peter.flp.2006@gmail.com"

_LOCK = threading.Lock()
_READY = False

# Execute schema changes one statement at a time in autocommit mode. CockroachDB
# can leave mixed DDL/DML transactions partially committed when one schema
# change fails, which is the opposite of what a request-time safety net needs.
PROFILE_SCHEMA_STATEMENTS = (
    "ALTER TABLE business ADD COLUMN IF NOT EXISTS profile_data JSONB NOT NULL DEFAULT '{}'::JSONB",
    "ALTER TABLE business ADD COLUMN IF NOT EXISTS profile_updated_at TIMESTAMPTZ",
    "ALTER TABLE owner_account ADD COLUMN IF NOT EXISTS display_name STRING",
    "ALTER TABLE owner_account ADD COLUMN IF NOT EXISTS email STRING",
    "ALTER TABLE business_fact ADD COLUMN IF NOT EXISTS profile_managed BOOL NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS owner_account_business_profile_idx ON owner_account (business_id, email)",
    (
        "CREATE INDEX IF NOT EXISTS business_fact_profile_managed_idx "
        "ON business_fact (business_id, profile_managed, superseded_by, learned_at DESC)"
    ),
    (
        "UPDATE owner_account SET display_name = username "
        "WHERE business_id IS NOT NULL "
        "AND (display_name IS NULL OR trim(display_name) = '')"
    ),
    (
        f"UPDATE owner_account SET email = '{LEGACY_OWNER_EMAIL}' "
        "WHERE business_id IS NOT NULL "
        "AND (email IS NULL OR trim(email) = '')"
    ),
)

# Kept as a readable/exportable migration receipt for tests and documentation.
PROFILE_SCHEMA_SQL = ";\n".join(PROFILE_SCHEMA_STATEMENTS) + ";\n"


def ensure_profile_schema(conn: Any) -> None:
    """Apply the additive profile schema once per warm execution process.

    The caller must invoke this before doing any other work on a non-autocommit
    connection, so changing ``autocommit`` does not interrupt an open
    transaction. All production call sites do that immediately after connect.
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
                for statement in PROFILE_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
        finally:
            if changed_autocommit:
                conn.autocommit = False
        _READY = True


def reset_profile_schema_cache_for_tests() -> None:
    global _READY
    _READY = False


__all__ = [
    "LEGACY_OWNER_EMAIL",
    "PROFILE_SCHEMA_SQL",
    "PROFILE_SCHEMA_STATEMENTS",
    "ensure_profile_schema",
    "reset_profile_schema_cache_for_tests",
]
