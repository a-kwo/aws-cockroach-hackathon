"""Authenticated owner decisions create one durable, tenant-scoped Maker task."""

import inspect
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.auth import token_fingerprint
from brasstacks.handlers import decision as decision_handler
from brasstacks.handlers.decision import UI_TO_DB, parse_request, respond
from brasstacks.repository import EvidenceRef, InMemoryRepository

NOW = datetime(2026, 8, 2, 19, 4, 5, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


def event(find_id="00000000-0000-0000-0000-000000000001", decision="approved"):
    return {
        "pathParameters": {"find_id": find_id},
        "body": json.dumps({"decision": decision}),
    }


def authenticated_event(token, *, find_id, decision="approved"):
    payload = event(find_id=find_id, decision=decision)
    payload["headers"] = {"Authorization": f"Bearer {token}"}
    return payload


class FakeQueue:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("SQS unavailable")
        return {"MessageId": f"message-{len(self.calls)}"}


def owner_repo(*, username="owner"):
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    account_id = repo.create_account(
        business_id, username=username, password_hash="not-used"
    )
    token = f"{username}-session-token"
    repo.create_session(
        token_fingerprint(token), business_id=business_id,
        account_id=account_id, expires_at=NOW + timedelta(days=1),
    )
    observation_id = repo.insert_observation(
        business_id, content="Customers want a fixed group menu", kind="review",
        embedding=VECTOR, observed_at=NOW - timedelta(days=1),
    )
    find_id = repo.insert_find_with_evidence(
        business_id,
        title="Create a group package",
        summary="Package the group meal.",
        rationale="Repeated group enquiries are being lost.",
        move="Write the package menu and booking message.",
        emoji="↗",
        predicted_daily_cents=2200,
        confidence=.72,
        verify_after=date(2026, 8, 20),
        evidence=[EvidenceRef(observation_id, .9)],
    )
    return repo, business_id, account_id, token, find_id


def test_parse_approved_decision():
    assert parse_request(event()) == (
        "00000000-0000-0000-0000-000000000001",
        "approved",
    )


def test_parse_rejected_decision():
    _, decision = parse_request(event(decision="rejected"))
    assert decision == "rejected"
    assert UI_TO_DB[decision] == "rejected"


@pytest.mark.parametrize("decision", ["", "later", "accept", "yes"])
def test_rejects_unknown_decision(decision):
    with pytest.raises(ValueError, match="approved.*rejected"):
        parse_request(event(decision=decision))


def test_requires_find_id():
    payload = event()
    payload["pathParameters"] = {}
    with pytest.raises(ValueError, match="find_id"):
        parse_request(payload)


def test_response_includes_cors_and_authorization():
    result = respond(200, {"ok": True})
    assert result["statusCode"] == 200
    assert result["headers"]["Access-Control-Allow-Origin"] == "*"
    assert "Authorization" in result["headers"]["Access-Control-Allow-Headers"]
    assert json.loads(result["body"]) == {"ok": True}


def test_handler_uses_one_server_timestamp_for_the_write_and_receipt():
    source = inspect.getsource(decision_handler.record_decision)
    assert "datetime.now(timezone.utc)" in source
    assert "decided_at=moment" in source
    assert '"decided_at": transition.decided_at.isoformat()' in source


def test_record_decision_requires_a_session():
    repo, _, _, _, find_id = owner_repo()
    result = decision_handler.record_decision(event(find_id=find_id), repo=repo)
    assert result["statusCode"] == 401
    assert json.loads(result["body"])["error"] == "sign in first"
    assert repo.list_dispatchable_tasks() == []


def test_approved_decision_creates_one_task_and_one_fifo_message():
    repo, business_id, account_id, token, find_id = owner_repo()
    queue = FakeQueue()

    result = decision_handler.record_decision(
        authenticated_event(token, find_id=find_id),
        repo=repo,
        queue_client=queue,
        maker_queue_url="https://sqs.example/maker.fifo",
        now=NOW,
    )

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["decision"] == "approved"
    assert body["status"] == "accepted"
    assert body["changed"] is True
    assert body["maker"] == "queued"
    assert body["maker_task"]["task_id"]

    tasks = repo.list_tasks(business_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.find_id == find_id
    assert task.requested_by_account_id == account_id
    assert task.dispatch_count == 1
    assert len(queue.calls) == 1
    queued = queue.calls[0]
    assert queued["QueueUrl"].endswith("maker.fifo")
    assert queued["MessageGroupId"] == task.resource_key
    message = json.loads(queued["MessageBody"])
    assert message["task_id"] == task.task_id
    assert message["business_id"] == business_id
    assert message["find_id"] == find_id


def test_repeated_do_it_is_idempotent_and_does_not_enqueue_again():
    repo, business_id, _, token, find_id = owner_repo()
    queue = FakeQueue()
    kwargs = dict(
        repo=repo,
        queue_client=queue,
        maker_queue_url="https://sqs.example/maker.fifo",
        now=NOW,
    )

    first = decision_handler.record_decision(
        authenticated_event(token, find_id=find_id), **kwargs
    )
    second = decision_handler.record_decision(
        authenticated_event(token, find_id=find_id), **kwargs
    )

    assert json.loads(first["body"])["maker"] == "queued"
    second_body = json.loads(second["body"])
    assert second_body["changed"] is False
    assert second_body["maker"] == "queued"
    assert len(repo.list_tasks(business_id)) == 1
    assert len(queue.calls) == 1


def test_queue_failure_keeps_a_durable_task_for_reconciliation():
    repo, business_id, _, token, find_id = owner_repo()
    queue = FakeQueue(fail=True)

    result = decision_handler.record_decision(
        authenticated_event(token, find_id=find_id),
        repo=repo,
        queue_client=queue,
        maker_queue_url="https://sqs.example/maker.fifo",
        now=NOW,
    )

    body = json.loads(result["body"])
    assert body["maker"] == "dispatch_failed"
    [task] = repo.list_tasks(business_id)
    assert task.status == "queued"
    assert task.dispatch_count == 1
    assert {event.event_type for event in repo.task_events(task.task_id)} >= {
        "queue.send_failed", "task.created",
    }


def test_pass_records_the_choice_without_creating_maker_work():
    repo, business_id, _, token, find_id = owner_repo()
    queue = FakeQueue()

    result = decision_handler.record_decision(
        authenticated_event(token, find_id=find_id, decision="rejected"),
        repo=repo,
        queue_client=queue,
        maker_queue_url="https://sqs.example/maker.fifo",
        now=NOW,
    )

    body = json.loads(result["body"])
    assert body["status"] == "rejected"
    assert body["maker"] == "not_requested"
    assert repo.list_tasks(business_id) == []
    assert queue.calls == []


def test_decision_is_scoped_to_the_authenticated_tenant():
    repo, _, _, token, find_id = owner_repo(username="first")
    other_business = repo.create_business(name="Other", category="restaurant")
    other_account = repo.create_account(
        other_business, username="second", password_hash="not-used"
    )
    other_token = "second-session"
    repo.create_session(
        token_fingerprint(other_token), business_id=other_business,
        account_id=other_account, expires_at=NOW + timedelta(days=1),
    )

    result = decision_handler.record_decision(
        authenticated_event(other_token, find_id=find_id),
        repo=repo,
        queue_client=FakeQueue(),
        maker_queue_url="https://sqs.example/maker.fifo",
        now=NOW,
    )

    assert result["statusCode"] == 409
    assert "no longer available" in json.loads(result["body"])["error"]
    assert repo.get_find_context(repo.account_for_session(
        token_fingerprint(token), now=NOW
    )["business_id"], find_id).status == "proposed"


def test_record_decision_does_not_fall_back_to_the_seeded_config_tenant():
    source = inspect.getsource(decision_handler.record_decision)
    assert "account_for_session" in source
    assert 'account.get("business_id")' in source
    assert "settings.business_id" not in source
