"""Event-driven Maker worker with a scheduled reconciliation safety net.

Approving a recommendation should start work now, not wait for the next full
Radar/Analyst night.  Decision and Ask invoke this Lambda asynchronously with a
business and find id.  A lightweight schedule runs the same worker periodically
to recover any accepted rows whose invocation was missed or whose earlier draft
attempt failed.

The durable queue is CockroachDB itself: an accepted find with no artifact.
Before every model call the worker reads that state again, so duplicate Lambda
events are harmless and empty sweeps consume no reasoning tokens.
"""

from __future__ import annotations

from typing import Any

from brasstacks.agents.maker import run_maker
from brasstacks.artifacts import build_artifact_store
from brasstacks.config import Settings
from brasstacks.providers import build_reasoner
from brasstacks.repository import FindSummary
from brasstacks.secrets import hydrate_environment

MAX_BUSINESSES_PER_SWEEP = 50
MAX_DRAFTS_PER_INVOCATION = 10
FIND_SCAN_LIMIT = 100


def queued_finds(
    repo: Any,
    business_id: str,
    *,
    preferred_find_id: str | None = None,
    limit: int = MAX_DRAFTS_PER_INVOCATION,
) -> list[FindSummary]:
    """Return accepted, undrafted work, oldest first.

    A directly approved find is placed first so the owner sees that handoff as
    quickly as possible.  The remaining capacity drains older backlog in the
    same invocation.
    """
    accepted = [
        find for find in repo.recent_finds(business_id, limit=FIND_SCAN_LIMIT)
        if find.status == "accepted" and not repo.get_artifacts(find.find_id)
    ]
    accepted.sort(key=lambda find: find.created_at)
    if preferred_find_id:
        accepted.sort(key=lambda find: find.find_id != preferred_find_id)
    return accepted[: max(0, int(limit))]


def process_maker_queue(
    *,
    repo: Any,
    reasoner: Any,
    store: Any,
    business_id: str,
    preferred_find_id: str | None = None,
    limit: int = MAX_DRAFTS_PER_INVOCATION,
    model_id: str | None = None,
) -> list[dict[str, Any]]:
    """Draft every selected queue item and return compact run receipts."""
    results: list[dict[str, Any]] = []
    for find in queued_finds(
        repo,
        business_id,
        preferred_find_id=preferred_find_id,
        limit=limit,
    ):
        result = run_maker(
            repo=repo,
            reasoner=reasoner,
            store=store,
            business_id=business_id,
            find=find,
            model_id=model_id,
        )
        results.append({
            "business_id": business_id,
            "find_id": find.find_id,
            "run_id": result.run_id,
            "artifact_id": result.artifact_id,
            "status": "failed" if result.error else "completed",
            "note": result.note,
        })
    return results


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    requested_business = str((event or {}).get("business_id") or "").strip() or None
    requested_find = str((event or {}).get("find_id") or "").strip() or None

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)
        if requested_business:
            if repo.get_business(requested_business) is None:
                return {"status": "ignored", "reason": "business_not_found", "drafts": []}
            businesses = [requested_business]
        else:
            businesses = repo.active_business_ids(limit=MAX_BUSINESSES_PER_SWEEP)
            if not businesses and settings.business_id:
                businesses = [settings.business_id]

        work: list[tuple[str, str | None]] = []
        remaining = MAX_DRAFTS_PER_INVOCATION
        for business_id in businesses:
            if remaining <= 0:
                break
            preferred = requested_find if business_id == requested_business else None
            for find in queued_finds(
                repo,
                business_id,
                preferred_find_id=preferred,
                limit=remaining,
            ):
                work.append((business_id, find.find_id))
                remaining -= 1

        # An idle reconciliation is deliberately SQL-only: no reasoner is even
        # constructed, so zero LLM tokens are spent when there is no queue.
        if not work:
            return {"status": "idle", "drafts": [], "llm_tokens": 0}

        reasoner = build_reasoner(settings)
        store = build_artifact_store(settings)
        receipts: list[dict[str, Any]] = []
        for business_id, find_id in work:
            # Re-read before each model call.  A previous item or another worker
            # may already have produced the artifact since the work list formed.
            current = queued_finds(
                repo,
                business_id,
                preferred_find_id=find_id,
                limit=1,
            )
            if not current or current[0].find_id != find_id:
                continue
            receipts.extend(process_maker_queue(
                repo=repo,
                reasoner=reasoner,
                store=store,
                business_id=business_id,
                preferred_find_id=find_id,
                limit=1,
                model_id=settings.reasoning_model_id,
            ))

    return {
        "status": "completed",
        "drafts": receipts,
        "processed": len(receipts),
    }


__all__ = [
    "handler",
    "queued_finds",
    "process_maker_queue",
    "MAX_DRAFTS_PER_INVOCATION",
]
