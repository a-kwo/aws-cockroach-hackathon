"""Additive schema bootstrap for external connections and launch receipts.

The canonical migration remains ``db/schema.sql``.  This request-time bootstrap
keeps a rolling Lambda deployment safe when the migration job is delayed.
"""

from __future__ import annotations

import threading
from typing import Any

from brasstacks.task_schema import ensure_task_schema

_LOCK = threading.Lock()
_READY = False

EXECUTION_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS external_connection (
      id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      business_id            UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
      provider               STRING NOT NULL,
      account_id             UUID NOT NULL REFERENCES owner_account(id) ON DELETE CASCADE,
      status                 STRING NOT NULL DEFAULT 'pending_selection'
                             CHECK (status IN ('pending_selection', 'connected', 'error', 'revoked')),
      token_ciphertext       STRING NOT NULL,
      scopes                 JSONB NOT NULL DEFAULT '[]'::JSONB,
      external_account_name  STRING,
      external_location_name STRING,
      display_name           STRING,
      metadata               JSONB NOT NULL DEFAULT '{}'::JSONB,
      last_error             STRING,
      connected_at           TIMESTAMPTZ,
      created_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      updated_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      UNIQUE INDEX external_connection_provider_idx (business_id, provider),
      INDEX external_connection_account_idx (account_id, updated_at DESC)
    )
    """,
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ",
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS execution_tool_id UUID REFERENCES tool_execution(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS find_execution_idx ON find (business_id, executed_at DESC)",
)


def ensure_execution_schema(conn: Any) -> None:
    global _READY
    if _READY:
        return
    # execution_tool_id and external_connection both reference the durable task
    # receipt tables. Make this bootstrap safe no matter which Lambda reaches it
    # first during a rolling deployment.
    ensure_task_schema(conn)
    with _LOCK:
        if _READY:
            return
        original_autocommit = getattr(conn, "autocommit", None)
        changed = original_autocommit is False
        if changed:
            conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for statement in EXECUTION_SCHEMA_STATEMENTS:
                    cur.execute(statement)
        finally:
            if changed:
                conn.autocommit = False
        _READY = True


def reset_execution_schema_cache_for_tests() -> None:
    global _READY
    _READY = False


__all__ = [
    "EXECUTION_SCHEMA_STATEMENTS",
    "ensure_execution_schema",
    "reset_execution_schema_cache_for_tests",
]
