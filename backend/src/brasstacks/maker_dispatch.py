"""Create and enqueue one durable Maker task.

A user action is first committed to CockroachDB and only then delivered through
SQS FIFO.  The task's idempotency key collapses repeated clicks.  The queue's
deduplication id collapses repeated delivery attempts for one dispatch, while a
later reconciliation retry receives a new dispatch number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from brasstacks.repository import RepositoryError
from brasstacks.tasks import TaskRecord, queue_message

MAKER_QUEUE_URL_VAR = "BRASSTACKS_MAKER_QUEUE_URL"
# Kept as an alias for older imports while the deployment moves from direct
# Lambda invocation to the queue-backed control plane.
MAKER_FUNCTION_VAR = MAKER_QUEUE_URL_VAR


@dataclass(frozen=True)
class MakerDispatchResult:
    status: str
    task_id: str | None = None
    dispatch_count: int | None = None
    task_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "task_id": self.task_id,
            "dispatch_count": self.dispatch_count,
            "task_status": self.task_status,
        }


def _existing_result(task: TaskRecord) -> MakerDispatchResult:
    status = {
        "completed": "completed",
        "running": "already_running",
        "waiting_user": "waiting_user",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(task.status, "queued")
    return MakerDispatchResult(
        status=status,
        task_id=task.task_id,
        dispatch_count=task.dispatch_count,
        task_status=task.status,
    )


def dispatch_task(
    *,
    repo: Any,
    queue_client: Any | None,
    queue_url: str | None,
    task: TaskRecord,
    source: str,
) -> MakerDispatchResult:
    if task.status not in {"queued", "retry"}:
        return _existing_result(task)
    if queue_client is None or not str(queue_url or "").strip():
        return MakerDispatchResult(
            status="not_configured", task_id=task.task_id,
            dispatch_count=task.dispatch_count, task_status=task.status,
        )

    dispatch = repo.prepare_task_dispatch(task.task_id)
    if dispatch is None:
        refreshed = repo.get_task(task.task_id, business_id=task.business_id) or task
        return _existing_result(refreshed)

    message = queue_message(dispatch, source=source)
    try:
        queue_client.send_message(
            QueueUrl=str(queue_url),
            MessageBody=json.dumps(message, separators=(",", ":"), sort_keys=True),
            MessageGroupId=dispatch.resource_key,
            MessageDeduplicationId=f"{dispatch.task_id}:{dispatch.dispatch_count}",
            MessageAttributes={
                "business_id": {"DataType": "String", "StringValue": dispatch.business_id},
                "task_type": {"DataType": "String", "StringValue": dispatch.task_type},
            },
        )
    except Exception:
        try:
            repo.record_task_event(
                dispatch.task_id, event_type="queue.send_failed",
                actor_type="dispatcher",
                data={"source": source, "dispatch_count": dispatch.dispatch_count},
            )
        except RepositoryError:
            pass
        return MakerDispatchResult(
            status="dispatch_failed", task_id=dispatch.task_id,
            dispatch_count=dispatch.dispatch_count, task_status=dispatch.status,
        )

    repo.record_task_event(
        dispatch.task_id, event_type="queue.sent", actor_type="dispatcher",
        data={"source": source, "dispatch_count": dispatch.dispatch_count},
    )
    return MakerDispatchResult(
        status="queued", task_id=dispatch.task_id,
        dispatch_count=dispatch.dispatch_count, task_status=dispatch.status,
    )


def dispatch_maker(
    *,
    repo: Any,
    queue_client: Any | None,
    queue_url: str | None,
    business_id: str,
    find_id: str,
    requested_by_account_id: str | None = None,
    approved_at: datetime | None = None,
    source: str = "owner_decision",
) -> MakerDispatchResult:
    """Create the task and place one dispatch on the FIFO queue.

    If queue delivery fails, the task remains ``queued`` in CockroachDB and the
    reconciliation worker will deliver it later. No accepted recommendation is
    lost merely because SQS was briefly unavailable.
    """
    task = repo.create_or_get_maker_task(
        business_id,
        find_id=find_id,
        requested_by_account_id=requested_by_account_id,
        approved_at=approved_at,
    )

    # A repeated click may arrive after the first request committed and queued
    # the task but before the browser rendered its receipt. Do not create a new
    # dispatch attempt for that duplicate owner action. If the original queue
    # send was lost, the reconciler owns recovery from the durable queued row.
    if not task.created and task.status == "queued" and task.dispatch_count > 0:
        return _existing_result(task)

    return dispatch_task(
        repo=repo, queue_client=queue_client, queue_url=queue_url,
        task=task, source=source,
    )


__all__ = [
    "dispatch_maker",
    "dispatch_task",
    "MakerDispatchResult",
    "MAKER_QUEUE_URL_VAR",
    "MAKER_FUNCTION_VAR",
]
