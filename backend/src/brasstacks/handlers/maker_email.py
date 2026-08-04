"""Notify the configured owner/test inbox that a Maker draft is ready.

This Step Functions state is intentionally non-generative. It reads the durable
artifact and invokes one constrained SES tool. A missing or disabled email
configuration produces a recorded ``skipped`` receipt rather than failing the
Maker task.
"""

from __future__ import annotations

import os
from typing import Any

from brasstacks.config import Settings
from brasstacks.profile_schema import ensure_profile_schema
from brasstacks.secrets import hydrate_environment
from brasstacks.tools import ToolContext, build_email_tool


def notify(
    *,
    repo: Any,
    ses_client: Any,
    task_id: str,
    env: dict[str, str] | os._Environ[str],
) -> dict[str, Any]:
    task = repo.get_task(task_id)
    if task is None:
        return {"status": "ignored", "reason": "task_not_found", "task_id": task_id}
    if task.superseded_at is not None or task.cancelled_at is not None:
        return {
            "status": "skipped",
            "reason": "task_superseded",
            "task_id": task_id,
            "task_status": task.status,
        }
    if task.status != "completed" or not task.output_artifact_id or not task.find_id:
        return {
            "status": "skipped",
            "reason": "draft_not_ready",
            "task_id": task_id,
            "task_status": task.status,
        }

    artifact = next(
        (item for item in repo.get_artifacts(task.find_id)
         if item.artifact_id == task.output_artifact_id),
        None,
    )
    if artifact is None:
        return {"status": "skipped", "reason": "artifact_not_found", "task_id": task_id}
    if artifact.superseded_at is not None:
        return {
            "status": "skipped",
            "reason": "artifact_superseded",
            "task_id": task_id,
        }

    # Resolve the destination inside the authenticated tenant. New tasks prefer
    # the account that approved the recommendation; imported/system tasks fall
    # back to the first owner email for that same business. The global setting
    # remains only a last-resort test inbox.
    owner_recipient = repo.owner_email_for_business(
        task.business_id, preferred_account_id=task.requested_by_account_id,
    )

    result = build_email_tool(env, recipient=owner_recipient).execute(
        ToolContext(
            repo=repo,
            business_id=task.business_id,
            task_id=task.task_id,
            user_id=task.requested_by_account_id,
            clients={"ses": ses_client},
        ),
        {
            "title": artifact.title,
            "body": artifact.body or artifact.preview or "",
            "summary": artifact.summary or artifact.preview or "",
            "owner_action": artifact.owner_action,
            "review_state": artifact.review_state,
            "owner_questions": list(artifact.metadata.get("owner_questions") or []),
            "artifact_type": artifact.metadata.get("artifact_type"),
            "revision": artifact.revision,
            "find_id": task.find_id,
            "artifact_id": artifact.artifact_id,
        },
    )
    return {
        "status": result.status,
        "task_id": task.task_id,
        "tool": result.tool_name,
        "tool_execution_id": result.execution_id,
        "external_reference": result.external_reference,
        "message": result.message,
        "data": dict(result.data),
    }


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    task_id = str((event or {}).get("task_id") or "").strip()
    if not task_id:
        return {"status": "ignored", "reason": "task_id_required"}

    hydrate_environment()
    settings = Settings.load()

    import boto3
    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        ensure_profile_schema(conn)
        return notify(
            repo=PostgresRepository(conn),
            ses_client=boto3.client("ses", region_name=settings.aws_region),
            task_id=task_id,
            env=os.environ,
        )


__all__ = ["handler", "notify"]
