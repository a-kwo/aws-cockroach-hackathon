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

    result = build_email_tool(env).execute(
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
        return notify(
            repo=PostgresRepository(conn),
            ses_client=boto3.client("ses", region_name=settings.aws_region),
            task_id=task_id,
            env=os.environ,
        )


__all__ = ["handler", "notify"]
