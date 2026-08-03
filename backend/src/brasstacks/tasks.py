"""Durable multi-tenant task primitives for Brass Tacks.

The agent model decides *what* to create.  This module defines the deterministic
control-plane vocabulary that decides who owns the work, whether it may run,
which retry it belongs to, and whether an external side effect already happened.

CockroachDB is the source of truth.  SQS and Step Functions are delivery and
orchestration layers; a duplicate message or workflow start is harmless because
workers must atomically claim the task row before spending model tokens.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_WAITING_USER = "waiting_user"
TASK_COMPLETED = "completed"
TASK_RETRY = "retry"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

TASK_STATUSES = frozenset({
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_WAITING_USER,
    TASK_COMPLETED,
    TASK_RETRY,
    TASK_FAILED,
    TASK_CANCELLED,
})

ACTIVE_TASK_STATUSES = frozenset({TASK_QUEUED, TASK_RUNNING, TASK_RETRY})
DISPATCHABLE_TASK_STATUSES = frozenset({TASK_QUEUED, TASK_RETRY})
TERMINAL_TASK_STATUSES = frozenset({TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED})

MAKER_DRAFT_TASK = "maker.generate_draft"
MAKER_AGENT = "maker"
MAKER_TASK_VERSION = 1


@dataclass(frozen=True)
class TaskRecord:
    """One durable unit of agent work.

    ``dispatch_count`` identifies a queue/workflow attempt.  Duplicate SQS
    deliveries for the same dispatch use the same Step Functions execution name;
    a later reconciliation retry increments the count and receives a new name.
    """

    task_id: str
    business_id: str
    find_id: str | None
    requested_by_account_id: str | None
    agent: str
    task_type: str
    status: str
    priority: int
    idempotency_key: str
    resource_key: str
    approval_state: str
    decision_cycle: int
    attempt_count: int
    dispatch_count: int
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancelled_at: datetime | None = None
    superseded_at: datetime | None = None
    next_attempt_at: datetime | None = None
    lease_expires_at: datetime | None = None
    claimed_by: str | None = None
    claim_token: str | None = None
    workflow_execution_arn: str | None = None
    output_artifact_id: str | None = None
    last_error: str | None = None
    input_data: Mapping[str, Any] = field(default_factory=dict)
    output_data: Mapping[str, Any] = field(default_factory=dict)
    # Runtime-only receipt metadata. It is not a database column.
    created: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def is_dispatchable(self) -> bool:
        return self.status in DISPATCHABLE_TASK_STATUSES


@dataclass(frozen=True)
class TaskEvent:
    event_id: str
    task_id: str
    business_id: str
    event_type: str
    created_at: datetime
    actor_type: str = "system"
    actor_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolExecutionRecord:
    execution_id: str
    task_id: str
    business_id: str
    tool_name: str
    status: str
    idempotency_key: str
    started_at: datetime
    finished_at: datetime | None = None
    external_reference: str | None = None
    error: str | None = None
    input_data: Mapping[str, Any] = field(default_factory=dict)
    output_data: Mapping[str, Any] = field(default_factory=dict)
    # Runtime-only receipt metadata. True only when this call inserted the row.
    created: bool = False


def maker_task_idempotency_key(
    find_id: str,
    *,
    decision_cycle: int = 1,
    version: int = MAKER_TASK_VERSION,
) -> str:
    """Return a stable task key for one recommendation decision cycle.

    Cycle one deliberately keeps the original key so already-deployed tasks do
    not duplicate after this migration. A reopened recommendation increments
    the cycle and therefore receives a fresh, independently idempotent task.
    """
    cycle = max(1, int(decision_cycle))
    if cycle == 1:
        return f"maker:draft:{find_id}:v{int(version)}"
    return f"maker:draft:{find_id}:cycle:{cycle}:v{int(version)}"


def maker_artifact_idempotency_key(task_id: str, *, kind: str, version: int = 1) -> str:
    return f"task:{task_id}:artifact:{kind}:v{int(version)}"


def maker_resource_key(
    business_id: str,
    find_id: str,
    *,
    decision_cycle: int = 1,
) -> str:
    """Order retries for one recommendation while unrelated work runs in parallel.

    Draft generation does not mutate a shared external account, so two different
    approved moves for the same business may safely run at the same time. Future
    publishing tools use their own resource keys (for example, one Google profile)
    when ordering is required.
    """
    cycle = max(1, int(decision_cycle))
    suffix = "" if cycle == 1 else f":cycle:{cycle}"
    return f"maker:find:{business_id}:{find_id}{suffix}"


def execution_name(task_id: str, dispatch_count: int) -> str:
    """A Step Functions-safe deterministic name for one dispatch attempt."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", str(task_id)).strip("-")
    # StartExecution allows 80 chars. Preserve uniqueness even for long ids.
    suffix = f"-d{int(dispatch_count)}"
    if len(cleaned) + len(suffix) > 80:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
        cleaned = f"task-{digest}"
    return f"{cleaned}{suffix}"


def queue_message(task: TaskRecord, *, source: str) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "business_id": task.business_id,
        "find_id": task.find_id,
        "agent": task.agent,
        "task_type": task.task_type,
        "dispatch_count": task.dispatch_count,
        "execution_name": execution_name(task.task_id, task.dispatch_count),
        "source": source,
    }


def compact_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, separators=(",", ":"), sort_keys=True, default=str)


__all__ = [
    "TaskRecord",
    "TaskEvent",
    "ToolExecutionRecord",
    "TASK_QUEUED",
    "TASK_RUNNING",
    "TASK_WAITING_USER",
    "TASK_COMPLETED",
    "TASK_RETRY",
    "TASK_FAILED",
    "TASK_CANCELLED",
    "TASK_STATUSES",
    "ACTIVE_TASK_STATUSES",
    "DISPATCHABLE_TASK_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "MAKER_DRAFT_TASK",
    "MAKER_AGENT",
    "MAKER_TASK_VERSION",
    "maker_task_idempotency_key",
    "maker_artifact_idempotency_key",
    "maker_resource_key",
    "execution_name",
    "queue_message",
    "compact_json",
]
