"""Idempotent bootstrap for owner-reported outcomes.

``db/schema.sql`` remains the canonical migration. This is the same additive
safety net ``oauth_schema`` and ``profile_schema`` provide, for the same reason:
a deployed Lambda can take a request before anyone has run the migration, and
the person who finds that out should not be an owner trying to report what their
move earned.

It is cheap to be careful here. ``find_outcome`` is the only table that can turn
an ESTIMATED verdict into a VERIFIED one, so a cluster missing it does not fail
loudly — it silently keeps publishing estimates forever.
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_READY = False

# One statement at a time in autocommit, for the reason profile_schema gives:
# CockroachDB can leave mixed DDL/DML transactions partially committed when one
# schema change fails, which is the opposite of what a safety net needs.
OUTCOME_SCHEMA_STATEMENTS = (
    (
        "CREATE TABLE IF NOT EXISTS find_outcome ("
        "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
        "business_id UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE, "
        "find_id UUID NOT NULL REFERENCES find(id) ON DELETE CASCADE, "
        "reported_by_account_id UUID REFERENCES owner_account(id) ON DELETE SET NULL, "
        "amount_cents BIGINT NOT NULL, "
        "basis STRING NOT NULL, "
        "daily_cents BIGINT NOT NULL, "
        "note STRING, "
        "reported_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
        "INDEX find_outcome_find_idx (find_id, reported_at DESC), "
        "INDEX (business_id, reported_at DESC))"
    ),
)

#: Readable migration receipt, for tests and documentation.
OUTCOME_SCHEMA_SQL = ";\n".join(OUTCOME_SCHEMA_STATEMENTS) + ";\n"


def ensure_outcome_schema(conn: Any) -> None:
    """Apply the additive outcome schema once per warm execution process.

    Unlike its siblings this one is called from the repository rather than only
    from the top of a handler, because recording an outcome may be the first
    thing a cold Lambda does. That means it can arrive with a transaction
    already open, and psycopg refuses to toggle ``autocommit`` there — which is
    a `ProgrammingError` from the middle of an owner's save, not a migration
    step anyone would enjoy debugging.

    So: switch to autocommit when the connection will allow it, and otherwise
    run inside the caller's transaction, where `CREATE TABLE IF NOT EXISTS` is
    perfectly legal. `_READY` is only latched in the first case. In the second
    the caller may still roll back, and remembering a table that got rolled away
    would skip the creation forever after.
    """
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return

        original_autocommit = getattr(conn, "autocommit", None)
        own_transaction = original_autocommit is not False
        if original_autocommit is False:
            try:
                conn.autocommit = True
                own_transaction = True
            except Exception:
                own_transaction = False

        try:
            with conn.cursor() as cursor:
                for statement in OUTCOME_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
        finally:
            if own_transaction and original_autocommit is False:
                conn.autocommit = False

        if own_transaction:
            _READY = True


def reset_outcome_schema_cache_for_tests() -> None:
    global _READY
    _READY = False


__all__ = [
    "OUTCOME_SCHEMA_SQL",
    "OUTCOME_SCHEMA_STATEMENTS",
    "ensure_outcome_schema",
    "reset_outcome_schema_cache_for_tests",
]
