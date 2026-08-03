"""Durable task-ledger invariants for multi-user, multi-agent execution."""

from datetime import date, datetime, timedelta, timezone

from brasstacks.repository import EvidenceRef, InMemoryRepository
from brasstacks.tasks import execution_name, maker_resource_key, maker_task_idempotency_key

NOW = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


def accepted_find(repo, business_id, title):
    obs = repo.insert_observation(
        business_id, content=f"Evidence for {title}", kind="review",
        embedding=VECTOR, observed_at=NOW,
    )
    return repo.insert_find_with_evidence(
        business_id, title=title, summary=title,
        rationale="Evidence exists.", move="Create the deliverable.", emoji="↗",
        predicted_daily_cents=1000, confidence=.7,
        verify_after=date(2026, 8, 20), status="accepted",
        evidence=[EvidenceRef(obs, .9)],
    )


def test_repeated_task_creation_returns_one_idempotent_row():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="A", category="restaurant")
    find_id = accepted_find(repo, business_id, "One move")

    first = repo.create_or_get_maker_task(business_id, find_id=find_id)
    second = repo.create_or_get_maker_task(business_id, find_id=find_id)

    assert first.task_id == second.task_id
    assert first.created is True
    assert second.created is False
    assert first.idempotency_key == maker_task_idempotency_key(find_id)
    assert first.resource_key == maker_resource_key(business_id, find_id)
    assert len(repo.list_tasks(business_id)) == 1


def test_unrelated_tasks_for_one_owner_can_run_in_parallel():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="A", category="restaurant")
    first = repo.create_or_get_maker_task(
        business_id, find_id=accepted_find(repo, business_id, "First")
    )
    second = repo.create_or_get_maker_task(
        business_id, find_id=accepted_find(repo, business_id, "Second")
    )

    assert first.resource_key != second.resource_key
    assert repo.claim_task(first.task_id, worker_id="worker-1") is not None
    assert repo.claim_task(second.task_id, worker_id="worker-2") is not None


def test_dispatch_selection_is_fair_across_businesses():
    repo = InMemoryRepository()
    heavy = repo.create_business(name="Heavy", category="restaurant")
    light = repo.create_business(name="Light", category="salon")
    for index in range(6):
        repo.create_or_get_maker_task(
            heavy, find_id=accepted_find(repo, heavy, f"Heavy {index}")
        )
    repo.create_or_get_maker_task(
        light, find_id=accepted_find(repo, light, "Light one")
    )

    selected = repo.list_dispatchable_tasks(limit=10, per_business_limit=2)
    counts = {}
    for task in selected:
        counts[task.business_id] = counts.get(task.business_id, 0) + 1

    assert counts[heavy] == 2
    assert counts[light] == 1


def test_expired_worker_lease_becomes_a_retry():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="A", category="restaurant")
    task = repo.create_or_get_maker_task(
        business_id, find_id=accepted_find(repo, business_id, "Move")
    )
    claim = repo.claim_task(task.task_id, worker_id="worker", lease_seconds=30)

    changed = repo.recover_stale_tasks(
        now=claim.lease_expires_at + timedelta(seconds=1), max_attempts=3
    )

    recovered = repo.get_task(task.task_id)
    assert changed == 1
    assert recovered.status == "retry"
    assert recovered.claim_token is None
    assert recovered.next_attempt_at is not None


def test_tool_execution_idempotency_reports_whether_the_row_was_created():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="A", category="restaurant")
    task = repo.create_or_get_maker_task(
        business_id, find_id=accepted_find(repo, business_id, "Move")
    )

    first = repo.start_tool_execution(
        task_id=task.task_id, business_id=business_id,
        tool_name="ses.send_review_email", idempotency_key="email-once",
    )
    duplicate = repo.start_tool_execution(
        task_id=task.task_id, business_id=business_id,
        tool_name="ses.send_review_email", idempotency_key="email-once",
    )

    assert first.execution_id == duplicate.execution_id
    assert first.created is True
    assert duplicate.created is False
    assert [e.event_type for e in repo.task_events(task.task_id)].count("tool.started") == 1


def test_workflow_execution_names_are_deterministic_and_bounded():
    task_id = "task/" + ("x" * 200)
    first = execution_name(task_id, 4)
    second = execution_name(task_id, 4)

    assert first == second
    assert first.endswith("-d4")
    assert len(first) <= 80
