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
    # Not a decision column, but the same `find` table and the same hazard: the
    # Analyst names it in every INSERT, so a cluster the migration has not
    # reached would store no finds at all rather than one without a rejected
    # alternative.
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS alternative_explanation STRING",
    # Same hazard again, at the other end of the loop. The Meter now writes NULL
    # into `actual_daily_cents` for a verdict with no measurement, because
    # storing the prediction there was the ledger reporting a forecast back as
    # the thing it was supposed to check. Against a cluster still carrying the
    # old NOT NULL DEFAULT 0 that INSERT raises, the Meter counts the find as
    # failed, and nothing is ever judged. Nine finds fall due between
    # 2026-08-22 and 2026-09-01, so the window where deploy order matters is
    # this month.
    "ALTER TABLE ledger_entry ALTER COLUMN actual_daily_cents DROP NOT NULL",
    "ALTER TABLE ledger_entry ALTER COLUMN actual_daily_cents DROP DEFAULT",
    # Radar labels what a row asserts, not just where it came from, so a
    # staleness rule can tell "This menu isn't available right now" from
    # "Monday 11:30 am - 8:30 pm". Nullable and undefaulted: the 147 rows
    # written before typing existed keep NULL, meaning "we never looked".
    "ALTER TABLE observation ADD COLUMN IF NOT EXISTS statement_type STRING",
    # The tier a find was published at, after any demotion. Required of the
    # model and computed by the claim standards, and until it had a column it
    # died with the process — nothing downstream could tell a checkable
    # listing_fact from a current_state claim about the owner's storefront.
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS claim_type STRING",
    "CREATE INDEX IF NOT EXISTS observation_statement_idx "
    "ON observation (business_id, statement_type, observed_at DESC)",
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
    # A find the pipeline declines to show. Two statements, one hazard: the
    # night writes the status and the reason in a single INSERT INTO find, so a
    # cluster missing either stores no finds at all — the same failure that cost
    # us `alternative_explanation` and the ledger.
    #
    # The ALTER TYPE runs here rather than only in db/schema.sql because
    # CREATE TYPE IF NOT EXISTS does nothing to a type that already exists, and
    # all three live tenants' find_status predates 'withheld'. It is safe in this
    # bootstrap and not inside schema.sql's transaction: CockroachDB refuses
    # ALTER TYPE ... ADD VALUE in a multi-statement transaction, and
    # ensure_decision_schema deliberately runs with autocommit on. The app user
    # owns find_status, which is what ALTER TYPE requires.
    #
    # Deliberately the last two statements in this tuple. The loop has no error
    # isolation and `_READY` is only set once every statement has run, so a
    # statement that fails takes the whole request path with it. Anything that
    # has never executed against the live cluster goes at the end, where the
    # already-proven migrations above it have applied first.
    "ALTER TABLE find ADD COLUMN IF NOT EXISTS withheld_reason STRING",
    "ALTER TYPE find_status ADD VALUE IF NOT EXISTS 'withheld'",
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
