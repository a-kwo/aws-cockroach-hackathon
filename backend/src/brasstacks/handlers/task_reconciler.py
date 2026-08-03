"""Recover and dispatch durable agent work without invoking a model.

This schedule is a safety net, not a second Maker implementation. It creates a
missing task for legacy accepted recommendations, expires abandoned worker
leases, and sends dispatchable rows to SQS FIFO. When nothing is waiting the
entire invocation is SQL-only and consumes zero LLM tokens.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from brasstacks.config import Settings
from brasstacks.maker_dispatch import MAKER_QUEUE_URL_VAR, dispatch_task
from brasstacks.secrets import hydrate_environment

MAX_BUSINESSES = 100
MAX_FINDS_PER_BUSINESS = 200
MAX_DISPATCHES = 100
MAX_ATTEMPTS = 3


def reconcile(
    *,
    repo: Any,
    queue_client: Any,
    queue_url: str,
    now: datetime,
) -> dict[str, Any]:
    recovered = repo.recover_stale_tasks(now=now, max_attempts=MAX_ATTEMPTS)
    created = 0
    for business_id in repo.active_business_ids(limit=MAX_BUSINESSES):
        for find in repo.recent_finds(business_id, limit=MAX_FINDS_PER_BUSINESS):
            if find.status != "accepted":
                continue
            # Legacy artifacts already represent completed Maker work. Do not
            # create a task merely to reproduce them.
            if repo.get_artifacts(find.find_id):
                continue
            before = repo.create_or_get_maker_task(
                business_id, find_id=find.find_id,
                approved_at=find.created_at,
            )
            if before.dispatch_count == 0 and before.attempt_count == 0:
                created += 1

    results = []
    for task in repo.list_dispatchable_tasks(limit=MAX_DISPATCHES):
        result = dispatch_task(
            repo=repo,
            queue_client=queue_client,
            queue_url=queue_url,
            task=task,
            source="reconciliation",
        )
        results.append(result.as_dict())

    return {
        "status": "idle" if not results and not recovered and not created else "completed",
        "tasks_created": created,
        "leases_recovered": recovered,
        "dispatches": results,
        "dispatched": sum(1 for result in results if result["status"] == "queued"),
        "llm_tokens": 0,
    }


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()
    queue_url = str(os.environ.get(MAKER_QUEUE_URL_VAR) or "").strip()
    if not queue_url:
        return {"status": "not_configured", "llm_tokens": 0}

    import boto3
    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        return reconcile(
            repo=PostgresRepository(conn),
            queue_client=boto3.client("sqs", region_name=settings.aws_region),
            queue_url=queue_url,
            now=datetime.now(timezone.utc),
        )


__all__ = ["handler", "reconcile"]
