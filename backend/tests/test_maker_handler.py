"""Atomic Maker tasks are idempotent across duplicate deliveries and retries."""

from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.artifacts import FakeArtifactStore
from brasstacks.handlers.maker import process_task
from brasstacks.providers import FakeReasoner, ModelUsage, ProviderError
from brasstacks.repository import EvidenceRef, InMemoryRepository

TODAY = date(2026, 8, 2)
VECTOR = [1.0] + [0.0] * 1023
DRAFT = {
    "title": "Draft launch plan",
    "body": "Review this draft, fill in the final price, and publish only after approval.",
}


def make_find(repo, business_id, title="Approved move", *, status="accepted"):
    observation_id = repo.insert_observation(
        business_id,
        content=f"Evidence for {title}",
        kind="review",
        embedding=VECTOR,
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    return repo.insert_find_with_evidence(
        business_id,
        title=title,
        rationale="A citable opportunity exists.",
        move="Prepare the owner-ready draft.",
        emoji="↗",
        predicted_daily_cents=1000,
        confidence=.7,
        verify_after=TODAY + timedelta(days=14),
        status=status,
        evidence=[EvidenceRef(observation_id, .9)],
    )


def make_task(repo):
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    find_id = make_find(repo, business_id)
    task = repo.create_or_get_maker_task(
        business_id,
        find_id=find_id,
        requested_by_account_id=None,
    )
    return business_id, find_id, task


def test_one_task_claim_creates_one_draft_and_a_traceable_receipt():
    repo = InMemoryRepository()
    business_id, find_id, task = make_task(repo)
    reasoner = FakeReasoner(
        [DRAFT], usage=ModelUsage(input_tokens=240, output_tokens=80)
    )
    store = FakeArtifactStore()

    result = process_task(
        repo=repo,
        reasoner_factory=lambda: reasoner,
        store_factory=lambda: store,
        task_id=task.task_id,
        worker_id="worker-a",
        workflow_execution_arn="arn:aws:states:us-east-1:123:execution:maker:task-d1",
        model_id="claude-opus-5",
    )

    assert result["status"] == "completed"
    assert result["business_id"] == business_id
    assert result["find_id"] == find_id
    assert result["artifact_id"]
    assert len(reasoner.calls) == 1
    assert len(store.puts) == 1
    assert store.puts[0]["key"] == f"tasks/{task.task_id}/review_reply.md"

    stored = repo.get_task(task.task_id)
    assert stored.status == "completed"
    assert stored.output_artifact_id == result["artifact_id"]
    assert stored.workflow_execution_arn.endswith("task-d1")
    assert len(repo.get_artifacts(find_id)) == 1
    assert {event.event_type for event in repo.task_events(task.task_id)} >= {
        "task.created", "workflow.started", "task.claimed", "task.completed",
    }


def test_duplicate_delivery_returns_the_completed_task_without_building_clients():
    repo = InMemoryRepository()
    _, find_id, task = make_task(repo)
    first_reasoner = FakeReasoner([DRAFT])
    first_store = FakeArtifactStore()
    process_task(
        repo=repo,
        reasoner_factory=lambda: first_reasoner,
        store_factory=lambda: first_store,
        task_id=task.task_id,
        worker_id="worker-a",
    )

    factories = {"reasoner": 0, "store": 0}

    def reasoner_factory():
        factories["reasoner"] += 1
        raise AssertionError("duplicate delivery must not construct a reasoner")

    def store_factory():
        factories["store"] += 1
        raise AssertionError("duplicate delivery must not construct a store")

    duplicate = process_task(
        repo=repo,
        reasoner_factory=reasoner_factory,
        store_factory=store_factory,
        task_id=task.task_id,
        worker_id="worker-b",
    )

    assert duplicate["status"] == "completed"
    assert duplicate["idempotent"] is True
    assert factories == {"reasoner": 0, "store": 0}
    assert len(repo.get_artifacts(find_id)) == 1


def test_only_one_worker_can_claim_the_same_task():
    repo = InMemoryRepository()
    _, _, task = make_task(repo)
    claim = repo.claim_task(task.task_id, worker_id="worker-a")
    assert claim is not None

    factory_calls = 0

    def reasoner_factory():
        nonlocal factory_calls
        factory_calls += 1
        return FakeReasoner([DRAFT])

    result = process_task(
        repo=repo,
        reasoner_factory=reasoner_factory,
        store_factory=FakeArtifactStore,
        task_id=task.task_id,
        worker_id="worker-b",
    )

    assert result["status"] == "running"
    assert result["reason"] == "already_claimed_or_not_dispatchable"
    assert factory_calls == 0


def test_retryable_model_failure_is_persisted_before_the_lambda_fails():
    repo = InMemoryRepository()
    _, _, task = make_task(repo)
    reasoner = FakeReasoner([ProviderError("provider unavailable")])

    with pytest.raises(RuntimeError, match="provider unavailable"):
        process_task(
            repo=repo,
            reasoner_factory=lambda: reasoner,
            store_factory=FakeArtifactStore,
            task_id=task.task_id,
            worker_id="worker-a",
        )

    stored = repo.get_task(task.task_id)
    assert stored.status == "retry"
    assert stored.attempt_count == 1
    assert stored.next_attempt_at is not None
    assert "provider unavailable" in stored.last_error
    assert len(repo.get_artifacts(task.find_id)) == 0


def test_task_creation_rejects_a_passed_recommendation():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    find_id = make_find(repo, business_id, status="rejected")

    with pytest.raises(Exception, match="approved recommendation"):
        repo.create_or_get_maker_task(business_id, find_id=find_id)
