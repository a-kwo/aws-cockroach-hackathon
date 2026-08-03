"""SQS FIFO -> Step Functions bridge for durable agent tasks.

The queue buffers bursts and preserves per-business ordering.  Each message
starts a Standard workflow using the deterministic execution name embedded by
the dispatcher. Re-delivery of the same SQS message therefore converges on the
same workflow execution instead of launching duplicate Maker work.
"""

from __future__ import annotations

import json
import os
from typing import Any

STATE_MACHINE_ARN_VAR = "BRASSTACKS_MAKER_STATE_MACHINE_ARN"


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("body") or "{}"
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict):
        raise ValueError("task message must be a JSON object")
    for required in ("task_id", "business_id", "execution_name"):
        if not str(payload.get(required) or "").strip():
            raise ValueError(f"task message is missing {required}")
    return payload


def start_records(
    event: Any,
    *,
    client: Any,
    state_machine_arn: str,
) -> dict[str, list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    for record in (event or {}).get("Records") or []:
        message_id = str(record.get("messageId") or "unknown")
        try:
            payload = _payload(record)
            client.start_execution(
                stateMachineArn=state_machine_arn,
                name=str(payload["execution_name"]),
                input=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )
        except Exception as exc:
            # StartExecution on a Standard workflow is idempotent only while the
            # execution is running. A duplicate delivery may still surface as
            # ExecutionAlreadyExists after the execution closes; that task is
            # already represented in CockroachDB and should be acknowledged.
            response = getattr(exc, "response", {}) or {}
            code = str((response.get("Error") or {}).get("Code") or "")
            if code == "ExecutionAlreadyExists":
                continue
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    state_machine_arn = str(os.environ.get(STATE_MACHINE_ARN_VAR) or "").strip()
    if not state_machine_arn:
        raise RuntimeError(f"{STATE_MACHINE_ARN_VAR} is not configured")

    import boto3

    return start_records(
        event,
        client=boto3.client("stepfunctions"),
        state_machine_arn=state_machine_arn,
    )


__all__ = ["handler", "start_records", "STATE_MACHINE_ARN_VAR"]
