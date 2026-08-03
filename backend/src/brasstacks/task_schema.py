"""Idempotent bootstrap for the durable task tables.

The canonical migration remains ``db/schema.sql``.  This small bootstrap exists
so a deployment can begin accepting Do it events immediately even when the
operator has not yet run the schema command from a laptop.  It executes once per
warm Lambda process and uses only additive ``IF NOT EXISTS`` operations.

A production CI/CD pipeline should still run ``python db/migrate.py
--schema-only`` before traffic is shifted; the bootstrap is a safety net, not a
replacement for managed migrations.
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_READY = False

TASK_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS work_task (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id              UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  find_id                  UUID REFERENCES find(id) ON DELETE CASCADE,
  requested_by_account_id  UUID REFERENCES owner_account(id) ON DELETE SET NULL,
  agent                    STRING NOT NULL,
  task_type                STRING NOT NULL,
  status                   STRING NOT NULL DEFAULT 'queued'
                           CHECK (status IN (
                             'queued', 'running', 'waiting_user', 'completed',
                             'retry', 'failed', 'cancelled'
                           )),
  priority                 INT NOT NULL DEFAULT 100,
  idempotency_key          STRING NOT NULL,
  resource_key             STRING NOT NULL,
  approval_state           STRING NOT NULL DEFAULT 'approved'
                           CHECK (approval_state IN (
                             'not_required', 'pending', 'approved', 'rejected'
                           )),
  approved_at              TIMESTAMPTZ,
  attempt_count            INT NOT NULL DEFAULT 0,
  dispatch_count           INT NOT NULL DEFAULT 0,
  claimed_by               STRING,
  claim_token              UUID,
  lease_expires_at         TIMESTAMPTZ,
  next_attempt_at          TIMESTAMPTZ,
  workflow_execution_arn   STRING,
  output_artifact_id       UUID,
  input_data               JSONB NOT NULL DEFAULT '{}'::JSONB,
  output_data              JSONB NOT NULL DEFAULT '{}'::JSONB,
  last_error               STRING,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  started_at               TIMESTAMPTZ,
  completed_at             TIMESTAMPTZ,
  UNIQUE INDEX work_task_idempotency_idx (idempotency_key),
  INDEX work_task_business_status_idx (business_id, status, created_at DESC),
  INDEX work_task_dispatch_idx (status, next_attempt_at, priority, created_at),
  INDEX work_task_find_idx (find_id, task_type)
);
CREATE TABLE IF NOT EXISTS task_event (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID NOT NULL REFERENCES work_task(id) ON DELETE CASCADE,
  business_id  UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  event_type   STRING NOT NULL,
  actor_type   STRING NOT NULL DEFAULT 'system',
  actor_id     STRING,
  data         JSONB NOT NULL DEFAULT '{}'::JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  INDEX task_event_task_idx (task_id, created_at),
  INDEX task_event_business_idx (business_id, created_at DESC)
);
CREATE TABLE IF NOT EXISTS tool_execution (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id            UUID NOT NULL REFERENCES work_task(id) ON DELETE CASCADE,
  business_id        UUID NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  tool_name          STRING NOT NULL,
  status             STRING NOT NULL DEFAULT 'running'
                     CHECK (status IN (
                       'running', 'succeeded', 'failed', 'skipped', 'cancelled'
                     )),
  idempotency_key    STRING NOT NULL,
  input_data         JSONB NOT NULL DEFAULT '{}'::JSONB,
  output_data        JSONB NOT NULL DEFAULT '{}'::JSONB,
  external_reference STRING,
  error              STRING,
  started_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  finished_at        TIMESTAMPTZ,
  UNIQUE INDEX tool_execution_idempotency_idx (idempotency_key),
  INDEX tool_execution_task_idx (task_id, started_at DESC),
  INDEX tool_execution_business_idx (business_id, started_at DESC)
);
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS task_id UUID REFERENCES work_task(id) ON DELETE SET NULL;
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS idempotency_key STRING;
ALTER TABLE artifact ADD COLUMN IF NOT EXISTS body STRING;
CREATE UNIQUE INDEX IF NOT EXISTS artifact_idempotency_idx
  ON artifact (idempotency_key);
CREATE INDEX IF NOT EXISTS artifact_task_idx ON artifact (task_id, created_at DESC);
"""


def ensure_task_schema(conn: Any) -> None:
    """Apply the additive task schema once in the current process."""
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        with conn.cursor() as cur:
            cur.execute(TASK_SCHEMA_SQL)
        _READY = True


def reset_task_schema_cache_for_tests() -> None:
    global _READY
    _READY = False


__all__ = ["ensure_task_schema", "reset_task_schema_cache_for_tests", "TASK_SCHEMA_SQL"]
