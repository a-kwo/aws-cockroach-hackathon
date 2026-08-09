"""Maker review packages, SES delivery receipts, and chat revisions."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from brasstacks.artifacts import FakeArtifactStore
from brasstacks.handlers.ask import (
    MAX_REVISIONS,
    is_revision_request,
    perform_revision,
)
from brasstacks.handlers.maker import process_task
from brasstacks.handlers.maker_email import notify
from brasstacks.handlers.ses_event import process_event
from brasstacks.providers import FakeReasoner
from brasstacks.repository import EvidenceRef, InMemoryRepository
from brasstacks.workflow_snapshot import build_workspace

NOW = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


class FakeQueue:
    def __init__(self):
        self.calls = []

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": f"queue-{len(self.calls)}"}


class FakeSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-message-structured-1"}


def setup_task():
    repo = InMemoryRepository()
    business_id = repo.create_business(
        name="Asaka", category="restaurant", city="Los Angeles"
    )
    account_id = repo.create_account(
        business_id, username="asaka-owner", password_hash="not-used"
    )
    repo.update_account_profile(
        account_id,
        display_name="Asaka Owner",
        email="virtual.icfd@gmail.com",
    )
    observation_id = repo.insert_observation(
        business_id,
        content="Delivery customers ask for a clearly priced set for two.",
        kind="review",
        embedding=VECTOR,
        observed_at=NOW,
    )
    find_id = repo.insert_find_with_evidence(
        business_id,
        title="Create a fixed-price sushi set for two",
        summary="Delivery customers need a simple set with clear value.",
        rationale="Reviews and orders show demand for a set for two.",
        move="Prepare the menu listing and owner review package.",
        emoji="↗",
        predicted_daily_cents=1800,
        confidence=.78,
        verify_after=date(2026, 8, 20),
        status="accepted",
        evidence=[EvidenceRef(observation_id, .91)],
    )
    task = repo.create_or_get_maker_task(
        business_id,
        find_id=find_id,
        requested_by_account_id=account_id,
    )
    return repo, business_id, account_id, find_id, task


V1 = {
    "title": "Sushi set for two",
    "body": "## Menu listing\n\nAsaka Set for Two — $40\n\n24 pieces, packed to travel.",
    "summary": "A concise fixed-price sushi-set listing is ready for review.",
    "review_state": "ready_for_review",
    "owner_action": "Confirm the $40 price, then copy the listing to the delivery menu.",
    "owner_questions": [],
    "artifact_type": "menu_update",
    "sections": [{
        "title": "Delivery menu listing",
        "purpose": "Ready to paste after price approval",
        "content": "Asaka Set for Two — $40. 24 pieces, packed to travel.",
    }],
}


V2 = {
    "title": "Sushi set for two",
    "body": "## Menu listing\n\nAsaka Set for Two — $40. 24 pieces. Packed to travel.",
    "summary": "A shorter sushi-set listing is ready for review.",
    "review_state": "ready_for_review",
    "owner_action": "Review the shorter wording, then copy it to the delivery menu.",
    "owner_questions": [],
    "artifact_type": "menu_update",
    "sections": [{
        "title": "Short menu listing",
        "purpose": "Fits tighter delivery-platform fields",
        "content": "Asaka Set for Two — $40. 24 pieces. Packed to travel.",
    }],
}


def complete_first_draft(repo, task):
    result = process_task(
        repo=repo,
        reasoner_factory=lambda: FakeReasoner([V1]),
        store_factory=FakeArtifactStore,
        task_id=task.task_id,
        worker_id="maker-v1",
    )
    assert result["status"] == "completed"
    return result


def test_maker_stores_a_structured_owner_review_package():
    repo, _, _, find_id, task = setup_task()

    result = complete_first_draft(repo, task)

    [artifact] = repo.get_artifacts(find_id)
    assert artifact.artifact_id == result["artifact_id"]
    assert artifact.summary == V1["summary"]
    assert artifact.owner_action == V1["owner_action"]
    assert artifact.review_state == "ready_for_review"
    assert artifact.metadata["artifact_type"] == "menu_update"
    assert artifact.metadata["sections"] == V1["sections"]
    assert artifact.revision == 1


def test_email_is_a_concise_notification_and_exact_rendered_copy_is_auditable():
    repo, _, _, _, task = setup_task()
    complete_first_draft(repo, task)
    ses = FakeSes()

    result = notify(
        repo=repo,
        ses_client=ses,
        task_id=task.task_id,
        env={
            "MAKER_EMAIL_ENABLED": "true",
            "MAKER_EMAIL_FROM": "peter.flp.2006@gmail.com",
            "MAKER_REVIEW_EMAIL": "fallback@example.com",
            "BRASSTACKS_SITE_URL": "https://app.example.com",
            "MAKER_EMAIL_CONFIGURATION_SET": "brasstacks-maker-review",
        },
    )

    assert result["status"] == "succeeded"
    [request] = ses.calls
    assert request["ConfigurationSetName"] == "brasstacks-maker-review"
    assert request["Destination"]["ToAddresses"] == ["virtual.icfd@gmail.com"]
    assert {tag["Name"] for tag in request["Tags"]} >= {
        "task_id", "business_id", "tool_execution_id", "revision",
    }
    plain = request["Message"]["Body"]["Text"]["Data"]
    assert V1["summary"] in plain
    assert V1["owner_action"] in plain
    assert "## Menu listing" not in plain
    assert "Nothing has been published or sent to customers" in plain

    [receipt] = repo.tool_executions(task.task_id)
    assert receipt.input_data["subject"] == request["Message"]["Subject"]["Data"]
    assert receipt.input_data["plain_body"] == plain
    assert "<html>" in receipt.input_data["html_body"]
    assert receipt.output_data["delivery_status"] == "accepted"
    assert receipt.output_data["recipient"] == "virtual.icfd@gmail.com"


def test_ses_events_are_idempotent_and_advance_the_delivery_timeline():
    repo, business_id, _, find_id, task = setup_task()
    complete_first_draft(repo, task)
    notify(
        repo=repo,
        ses_client=FakeSes(),
        task_id=task.task_id,
        env={
            "MAKER_EMAIL_ENABLED": "true",
            "MAKER_EMAIL_FROM": "peter.flp.2006@gmail.com",
            "BRASSTACKS_SITE_URL": "https://app.example.com",
            "MAKER_EMAIL_CONFIGURATION_SET": "brasstacks-maker-review",
        },
    )
    [tool] = repo.tool_executions(task.task_id)

    def event(event_id, detail_type, at, detail=None):
        return {
            "id": event_id,
            "source": "aws.ses",
            "detail-type": detail_type,
            "time": at,
            "region": "us-east-1",
            "detail": {
                "mail": {
                    "messageId": "ses-message-structured-1",
                    "destination": ["virtual.icfd@gmail.com"],
                    "tags": {"tool_execution_id": [tool.execution_id]},
                },
                **(detail or {}),
            },
        }

    delivered = process_event(
        repo=repo,
        event=event("evt-delivered", "Email Delivered", "2026-08-03T18:01:00Z"),
    )
    duplicate = process_event(
        repo=repo,
        event=event("evt-delivered", "Email Delivered", "2026-08-03T18:01:00Z"),
    )
    opened = process_event(
        repo=repo,
        event=event("evt-opened", "Email Opened", "2026-08-03T18:03:00Z"),
    )
    clicked = process_event(
        repo=repo,
        event=event(
            "evt-clicked",
            "Email Clicked",
            "2026-08-03T18:04:00Z",
            {"click": {"link": "https://app.example.com/app/?task=" + task.task_id}},
        ),
    )

    assert delivered["status"] == "recorded"
    assert duplicate["status"] == "duplicate"
    assert opened["event_type"] == "opened"
    assert clicked["event_type"] == "clicked"
    assert [row.event_type for row in repo.email_events_for_tool(tool.execution_id)] == [
        "delivered", "opened", "clicked",
    ]

    artifacts = repo.get_artifacts(find_id)
    data = {
        "business": {"id": business_id, "name": "Asaka", "category": "restaurant"},
        "summary": {},
        "corpus": {},
        "finds": [{
            "id": find_id,
            "title": "Create a fixed-price sushi set for two",
            "summary": "Delivery customers need a simple set with clear value.",
            "rationale": "Demand exists.",
            "move": "Prepare a menu listing.",
            "emoji": "↗",
            "predicted_daily_cents": 1800,
            "confidence": .78,
            "verify_after": "2026-08-20",
            "status": "accepted",
            "created_at": "2026-08-03T18:00:00+00:00",
            "evidence": [],
            "artifacts": [{
                "id": artifacts[0].artifact_id,
                "kind": artifacts[0].kind,
                "title": artifacts[0].title,
                "preview": artifacts[0].preview,
                "body": artifacts[0].body,
                "summary": artifacts[0].summary,
                "owner_action": artifacts[0].owner_action,
                "review_state": artifacts[0].review_state,
                "metadata": dict(artifacts[0].metadata),
                "revision": artifacts[0].revision,
                "task_id": task.task_id,
                "created_at": "2026-08-03T18:00:30+00:00",
            }],
        }],
        "tasks": [{
            "id": task.task_id,
            "business_id": business_id,
            "find_id": find_id,
            "agent": "maker",
            "task_type": "maker.generate_draft",
            "status": "completed",
            "priority": 100,
            "approval_state": "approved",
            "attempt_count": 1,
            "dispatch_count": 1,
            "created_at": "2026-08-03T18:00:00+00:00",
            "updated_at": "2026-08-03T18:00:30+00:00",
            "completed_at": "2026-08-03T18:00:30+00:00",
            "output_artifact_id": artifacts[0].artifact_id,
            "input_data": {"title": "Create a fixed-price sushi set for two"},
            "output_data": {"artifact_id": artifacts[0].artifact_id},
            "events": [],
            "tools": [{
                "id": tool.execution_id,
                "tool_name": tool.tool_name,
                "status": tool.status,
                "started_at": tool.started_at.isoformat(),
                "finished_at": tool.finished_at.isoformat(),
                "external_reference": tool.external_reference,
                "input_data": dict(tool.input_data),
                "output_data": dict(tool.output_data),
                "email_events": [{
                    "id": row.event_id,
                    "provider_event_id": row.provider_event_id,
                    "event_type": row.event_type,
                    "event_at": row.event_at.isoformat(),
                    "recipient": row.recipient,
                    "link": row.link,
                    "data": dict(row.data),
                } for row in repo.email_events_for_tool(tool.execution_id)],
            }],
        }],
        "runs": [],
        "kinds": [],
    }
    workspace = build_workspace(data)
    projected = workspace["maker"]["tasks"][0]["tools"][0]
    assert projected["deliveryStatus"] == "clicked"
    assert [row["type"] for row in projected["events"]] == [
        "delivered", "opened", "clicked",
    ]
    assert projected["input"]["plain_body"]


def test_chat_revision_keeps_history_and_creates_one_new_current_version():
    repo, business_id, account_id, find_id, task = setup_task()
    first = complete_first_draft(repo, task)
    queue = FakeQueue()

    response = perform_revision(
        repo=repo,
        business_id=business_id,
        find_id=find_id,
        request_text="Make it shorter",
        timestamp=NOW + timedelta(minutes=5),
        queue_client=queue,
        maker_queue_url="https://sqs.example/maker.fifo",
        requested_by_account_id=account_id,
        model_id="claude-opus-5",
    )

    assert response["statusCode"] == 202
    payload = json.loads(response["body"])
    assert payload["action"] == {
        "type": "revise_draft",
        "status": "queued",
        "revision": 2,
        "task_id": task.task_id,
    }
    queued = repo.get_task(task.task_id)
    assert queued.status == "queued"
    assert queued.attempt_count == 0
    assert queued.input_data["base_artifact_id"] == first["artifact_id"]
    assert queued.input_data["revision_instruction"] == "Make it shorter"
    assert len(queue.calls) == 1

    result = process_task(
        repo=repo,
        reasoner_factory=lambda: FakeReasoner([V2]),
        store_factory=FakeArtifactStore,
        task_id=task.task_id,
        worker_id="maker-v2",
    )
    assert result["status"] == "completed"
    assert result["revision"] == 2

    artifacts = repo.get_artifacts(find_id)
    current = next(item for item in artifacts if item.superseded_at is None)
    previous = next(item for item in artifacts if item.revision == 1)
    assert current.revision == 2
    assert current.parent_artifact_id == previous.artifact_id
    assert current.body == V2["body"]
    assert previous.body == V1["body"]
    assert previous.superseded_at is not None
    assert [event.event_type for event in repo.task_events(task.task_id)].count(
        "task.revision_requested"
    ) == 1


def test_revision_is_capped_to_stop_an_endless_loop():
    # Past the ceiling, another pass is rarely the answer. The agent stops
    # queuing, spends no tokens, and nudges toward accept-or-rethink.
    repo, business_id, account_id, find_id, task = setup_task()
    repo.insert_artifact(
        find_id=find_id, kind="general_draft", title="converged",
        body="the sixth version", review_state="ready_for_review",
        revision=MAX_REVISIONS)
    queue = FakeQueue()

    response = perform_revision(
        repo=repo, business_id=business_id, find_id=find_id,
        request_text="revise again", timestamp=NOW + timedelta(minutes=9),
        queue_client=queue, maker_queue_url="https://sqs.example/maker.fifo",
        requested_by_account_id=account_id, model_id="claude-opus-5")

    payload = json.loads(response["body"])
    assert payload["action"]["status"] == "revision_limit"
    assert payload["action"]["revision"] == MAX_REVISIONS
    assert len(queue.calls) == 0  # nothing queued past the ceiling


def test_common_owner_commands_are_recognised_as_draft_revisions():
    assert is_revision_request("Make it shorter")
    assert is_revision_request("Make this more professional")
    assert is_revision_request("Revise the draft to use a warmer tone")
    assert not is_revision_request("Should I make the lunch special shorter?")


def test_unrelated_ses_events_are_ignored():
    repo = InMemoryRepository()
    event = {
        "id": "evt-unrelated",
        "detail-type": "Email Delivered",
        "detail": {
            "mail": {
                "messageId": "external-message",
                "destination": ["someone@example.com"],
                "tags": {},
            },
            "delivery": {"timestamp": "2026-08-03T12:00:00Z"},
        },
    }

    result = process_event(repo=repo, event=event)

    assert result == {
        "status": "ignored",
        "reason": "maker_tool_tag_missing",
        "message_id": "external-message",
    }
