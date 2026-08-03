"""The SQL-only reconciler recovers legacy and abandoned work fairly."""

import json
from datetime import date, datetime, timedelta, timezone

from brasstacks.handlers.task_reconciler import reconcile
from brasstacks.repository import EvidenceRef, InMemoryRepository

NOW = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


class FakeQueue:
    def __init__(self):
        self.calls = []

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": str(len(self.calls))}


def accepted_find(repo, business_id, title):
    obs = repo.insert_observation(
        business_id, content=title, kind="review", embedding=VECTOR,
        observed_at=NOW,
    )
    return repo.insert_find_with_evidence(
        business_id, title=title, summary=title, rationale="Why", move="Draft it",
        emoji="↗", predicted_daily_cents=1000, confidence=.7,
        verify_after=date(2026, 8, 20), status="accepted",
        evidence=[EvidenceRef(obs, .9)],
    )


def test_reconciler_creates_and_dispatches_legacy_accepted_work_without_a_model():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="A", category="restaurant")
    find_id = accepted_find(repo, business_id, "Legacy approval")
    queue = FakeQueue()

    result = reconcile(
        repo=repo, queue_client=queue,
        queue_url="https://sqs.example/maker.fifo", now=NOW,
    )

    assert result["status"] == "completed"
    assert result["tasks_created"] == 1
    assert result["dispatched"] == 1
    assert result["llm_tokens"] == 0
    [task] = repo.list_tasks(business_id)
    assert task.find_id == find_id
    assert json.loads(queue.calls[0]["MessageBody"])["task_id"] == task.task_id


def test_reconciler_does_not_recreate_work_with_a_legacy_artifact():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="A", category="restaurant")
    find_id = accepted_find(repo, business_id, "Already drafted")
    repo.insert_artifact(find_id=find_id, kind="review_reply", title="Existing")

    result = reconcile(
        repo=repo, queue_client=FakeQueue(),
        queue_url="https://sqs.example/maker.fifo", now=NOW,
    )

    assert result["status"] == "idle"
    assert result["tasks_created"] == 0
    assert repo.list_tasks(business_id) == []
    assert result["llm_tokens"] == 0


def test_reconciler_recovers_an_expired_claim_and_dispatches_the_retry():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="A", category="restaurant")
    task = repo.create_or_get_maker_task(
        business_id, find_id=accepted_find(repo, business_id, "Recover me")
    )
    claim = repo.claim_task(task.task_id, worker_id="dead-worker", lease_seconds=30)
    queue = FakeQueue()

    result = reconcile(
        repo=repo, queue_client=queue,
        queue_url="https://sqs.example/maker.fifo",
        now=claim.lease_expires_at + timedelta(seconds=1),
    )

    assert result["leases_recovered"] == 1
    assert result["dispatched"] == 1
    assert repo.get_task(task.task_id).status == "retry"
    assert result["llm_tokens"] == 0
