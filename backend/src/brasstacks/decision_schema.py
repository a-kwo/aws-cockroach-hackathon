"""Additive bootstrap for decision cycles and immutable decision history.

``db/schema.sql`` remains the canonical migration. This request-time bootstrap
lets a rolling serverless deployment accept the new reconsider action before an
operator runs the explicit migration from a workstation.
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_READY = False

DECISION_SCHEMA_STATEMENTS = (
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS decision_cycle INT NOT NULL DEFAULT 1",
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS reopened_at TIMESTAMPTZ",
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS reopen_reason_code STRING",
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS reopen_reason_note STRING",
    "CREATE INDEX IF NOT EXISTS find_reopened_idx ON find (business_id, reopened_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS decision_event (
      id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      business_id        UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
      find_id            UUID NOT NULL REFERENCES find(id) ON DELETE CASCADE,
      decision_cycle     INT NOT NULL,
      event_type         STRING NOT NULL,
      previous_status    STRING,
      new_status         STRING NOT NULL,
      actor_account_id   UUID REFERENCES owner_account(id) ON DELETE SET NULL,
      reason_code        STRING,
      reason_note        STRING,
      source             STRING NOT NULL DEFAULT 'owner_decision',
      data               JSONB NOT NULL DEFAULT '{}'::JSONB,
      created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      INDEX decision_event_find_idx (find_id, created_at DESC),
      INDEX decision_event_business_idx (business_id, created_at DESC)
    )
    """,
)

DECISION_SCHEMA_SQL = ";\n".join(statement.strip() for statement in DECISION_SCHEMA_STATEMENTS) + ";\n"


def ensure_decision_schema(conn: Any) -> None:
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
                for statement in DECISION_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
        finally:
            if changed_autocommit:
                conn.autocommit = False
        _READY = True


def reset_decision_schema_cache_for_tests() -> None:
    global _READY
    _READY = False


__all__ = [
    "DECISION_SCHEMA_SQL",
    "DECISION_SCHEMA_STATEMENTS",
    "ensure_decision_schema",
    "reset_decision_schema_cache_for_tests",
]
