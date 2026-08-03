"""Atomic Maker worker invoked by the durable Step Functions workflow.

The worker never scans for work and never trusts queue delivery as ownership.
It receives one ``task_id``, atomically claims that CockroachDB row, and only
then constructs the reasoner. This is the boundary that prevents two Lambda
invocations from generating two drafts for one owner decision.
"""

from __future__ import annotations

from typing import Any

from brasstacks.agents.maker import ARTIFACT_KIND, run_maker
from brasstacks.artifacts import build_artifact_store
from brasstacks.config import Settings
from brasstacks.providers import build_reasoner
from brasstacks.repository import FindSummary, RepositoryError
from brasstacks.secrets import hydrate_environment
from brasstacks.tasks import MAKER_DRAFT_TASK, maker_artifact_idempotency_key

LEASE_SECONDS = 12 * 60
MAX_ATTEMPTS = 3


def _find_for_task(repo: Any, task: Any) -> FindSummary | None:
    if not task.find_id:
        return None
    context = repo.get_find_context(task.business_id, task.find_id)
    if context is None:
        return None
    return FindSummary(
        find_id=context.find_id,
        title=context.title,
        move=context.move,
        status=context.status,
        predicted_daily_cents=context.predicted_daily_cents,
        created_at=task.created_at,
    )


def process_task(
    *,
    repo: Any,
    reasoner_factory: Any,
    store_factory: Any,
    task_id: str,
    worker_id: str,
    workflow_execution_arn: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    task = repo.get_task(task_id)
    if task is None:
        return {"status": "ignored", "reason": "task_not_found", "task_id": task_id}
    if task.task_type != MAKER_DRAFT_TASK:
        return {"status": "ignored", "reason": "unsupported_task_type", "task_id": task_id}
    if workflow_execution_arn:
        repo.record_task_workflow(task_id, execution_arn=workflow_execution_arn)

    if task.status == "completed":
        return {
            "status": "completed",
            "task_id": task_id,
            "artifact_id": task.output_artifact_id,
            "idempotent": True,
        }
    if task.status in {"failed", "cancelled"}:
        return {"status": task.status, "task_id": task_id, "error": task.last_error}

    claim = repo.claim_task(task_id, worker_id=worker_id, lease_seconds=LEASE_SECONDS)
    if claim is None:
        current = repo.get_task(task_id)
        return {
            "status": current.status if current else "ignored",
            "task_id": task_id,
            "reason": "already_claimed_or_not_dispatchable",
            "artifact_id": current.output_artifact_id if current else None,
        }

    find = _find_for_task(repo, claim)
    if find is None or find.status != "accepted":
        error = "approved recommendation is no longer available"
        repo.fail_task(
            task_id, claim_token=claim.claim_token or "", error=error,
            retryable=False,
        )
        return {"status": "failed", "task_id": task_id, "error": error}

    # Expensive clients are deliberately constructed only after the atomic
    # claim succeeds. Duplicate deliveries and idle retries therefore use zero
    # model tokens.
    reasoner = reasoner_factory()
    store = store_factory()
    result = run_maker(
        repo=repo,
        reasoner=reasoner,
        store=store,
        business_id=claim.business_id,
        find=find,
        model_id=model_id,
        task_id=claim.task_id,
        artifact_idempotency_key=maker_artifact_idempotency_key(
            claim.task_id, kind=ARTIFACT_KIND
        ),
    )

    if result.error:
        retryable = claim.attempt_count < MAX_ATTEMPTS
        repo.fail_task(
            task_id,
            claim_token=claim.claim_token or "",
            error=result.error,
            retryable=retryable,
            retry_after_seconds=min(15 * (2 ** max(0, claim.attempt_count - 1)), 300),
        )
        # The CockroachDB row already contains the retry schedule before the
        # function fails. The ASL retries only Lambda service/invocation errors;
        # model or repository failures are redispatched later by the SQL
        # reconciler so a workflow retry cannot race the durable next-attempt time.
        if retryable:
            raise RuntimeError(result.error)
        return {"status": "failed", "task_id": task_id, "error": result.error}

    completed = repo.complete_task(
        task_id,
        claim_token=claim.claim_token or "",
        output_artifact_id=result.artifact_id,
        output_data={
            "artifact_id": result.artifact_id,
            "run_id": result.run_id,
            "location": result.location,
            "reused": result.reused,
        },
    )
    return {
        "status": "completed",
        "task_id": completed.task_id,
        "business_id": completed.business_id,
        "find_id": completed.find_id,
        "artifact_id": completed.output_artifact_id,
        "run_id": result.run_id,
        "location": result.location,
        "reused": result.reused,
    }


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    event = event or {}
    task_id = str(event.get("task_id") or "").strip()
    if not task_id:
        return {"status": "ignored", "reason": "task_id_required"}

    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    worker_id = str(getattr(context, "aws_request_id", None) or event.get("worker_id") or "maker")
    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)
        try:
            return process_task(
                repo=repo,
                reasoner_factory=lambda: build_reasoner(settings),
                store_factory=lambda: build_artifact_store(settings),
                task_id=task_id,
                worker_id=worker_id,
                workflow_execution_arn=(
                    str(event.get("workflow_execution_arn") or "").strip() or None
                ),
                model_id=settings.reasoning_model_id,
            )
        except RepositoryError as exc:
            return {"status": "failed", "task_id": task_id, "error": str(exc)}


__all__ = ["handler", "process_task", "LEASE_SECONDS", "MAX_ATTEMPTS"]
