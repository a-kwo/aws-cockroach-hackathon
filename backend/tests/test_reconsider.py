"""Reconsideration opens a new decision cycle without rewriting history."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from brasstacks.auth import token_fingerprint
from brasstacks.decisions import DECISION_ACCEPTED, DECISION_REOPENED
from brasstacks.handlers.ask import perform_reconsider
from brasstacks.handlers.decision import record_decision
from brasstacks.repository import EvidenceRef, InMemoryRepository, RepositoryError
from brasstacks.tasks import maker_artifact_idempotency_key, maker_task_idempotency_key

NOW = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


def owner_workspace(*, initial_status: str = "proposed"):
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Test business", category="restaurant")
    account_id = repo.create_account(
        business_id, username="owner", password_hash="unused",
    )
    token = "owner-session"
    repo.create_session(
        token_fingerprint(token), business_id=business_id,
        account_id=account_id, expires_at=NOW + timedelta(days=1),
    )
    observation_id = repo.insert_observation(
        business_id, content="Customers want an easier ordering path",
        kind="review", embedding=VECTOR, observed_at=NOW - timedelta(days=1),
    )
    find_id = repo.insert_find_with_evidence(
        business_id,
        title="Fix online ordering",
        summary="Customers are abandoning orders because the menu is difficult to use.",
        rationale="Several customers could not finish an order.",
        move="Prepare a corrected delivery menu and publishing checklist.",
        emoji="↗", predicted_daily_cents=1200, confidence=.72,
        verify_after=date(2026, 8, 20), status=initial_status,
        decided_at=NOW if initial_status == "accepted" else None,
        evidence=[EvidenceRef(observation_id, .91)],
    )
    return repo, business_id, account_id, token, find_id


def approved_task_with_draft(repo, business_id, account_id, find_id):
    task = repo.create_or_get_maker_task(
        business_id, find_id=find_id, requested_by_account_id=account_id,
        approved_at=NOW,
    )
    claim = repo.claim_task(task.task_id, worker_id="maker-test")
    artifact_id = repo.insert_artifact(
        find_id=find_id, kind="review_reply", title="Ordering update draft",
        preview="Update your delivery menu.", body="Complete draft",
        task_id=task.task_id,
        idempotency_key=maker_artifact_idempotency_key(task.task_id, kind="review_reply"),
    )
    repo.complete_task(
        task.task_id, claim_token=claim.claim_token,
        output_artifact_id=artifact_id,
    )
    return repo.get_task(task.task_id), artifact_id


def test_reconsider_preserves_do_it_and_opens_a_fresh_cycle():
    repo, business_id, account_id, _, find_id = owner_workspace()
    accepted = repo.set_find_status(
        find_id, status="accepted", business_id=business_id,
        actor_account_id=account_id, decided_at=NOW,
    )
    old_task, artifact_id = approved_task_with_draft(
        repo, business_id, account_id, find_id,
    )
    email = repo.start_tool_execution(
        task_id=old_task.task_id, business_id=business_id,
        tool_name="ses.send_review_email", idempotency_key=f"email:{old_task.task_id}",
    )
    repo.finish_tool_execution(
        email.execution_id, status="succeeded",
        external_reference="ses-message-1",
    )

    reopened = repo.reconsider_find(
        find_id, business_id=business_id, actor_account_id=account_id,
        reason_code="timing_changed", reason_note="Wait until next week",
        reopened_at=NOW + timedelta(minutes=10),
    )

    assert accepted.decision_cycle == 1
    assert reopened.previous_cycle == 1
    assert reopened.decision_cycle == 2
    assert reopened.status == "proposed"
    context = repo.get_find_context(business_id, find_id)
    assert context.status == "proposed"
    assert context.decision_cycle == 2
    assert context.reopen_reason_code == "timing_changed"

    preserved_task = repo.get_task(old_task.task_id)
    assert preserved_task.status == "completed"
    assert preserved_task.superseded_at is not None
    preserved_artifact = next(item for item in repo.get_artifacts(find_id)
                              if item.artifact_id == artifact_id)
    assert preserved_artifact.superseded_at is not None
    assert repo.tool_executions(old_task.task_id)[0].external_reference == "ses-message-1"

    events = list(reversed(repo.decision_events(find_id, business_id=business_id)))
    assert [event.event_type for event in events] == [DECISION_ACCEPTED, DECISION_REOPENED]
    assert events[0].decision_cycle == 1
    assert events[1].decision_cycle == 2
    assert events[1].reason_note == "Wait until next week"

    second_accept = repo.set_find_status(
        find_id, status="accepted", business_id=business_id,
        actor_account_id=account_id, decided_at=NOW + timedelta(minutes=20),
    )
    new_task = repo.create_or_get_maker_task(
        business_id, find_id=find_id, requested_by_account_id=account_id,
        approved_at=second_accept.decided_at,
    )
    assert second_accept.decision_cycle == 2
    assert new_task.task_id != old_task.task_id
    assert new_task.decision_cycle == 2
    assert new_task.idempotency_key == maker_task_idempotency_key(
        find_id, decision_cycle=2,
    )
    assert len(repo.list_tasks(business_id)) == 2


def test_reconsider_cancels_a_running_worker_before_it_can_store_output():
    repo, business_id, account_id, _, find_id = owner_workspace()
    repo.set_find_status(
        find_id, status="accepted", business_id=business_id,
        actor_account_id=account_id, decided_at=NOW,
    )
    task = repo.create_or_get_maker_task(
        business_id, find_id=find_id, requested_by_account_id=account_id,
    )
    claim = repo.claim_task(task.task_id, worker_id="maker-running")
    assert repo.task_can_continue(task.task_id, claim_token=claim.claim_token)

    reopened = repo.reconsider_find(
        find_id, business_id=business_id, actor_account_id=account_id,
        reason_code="approved_by_mistake", reopened_at=NOW + timedelta(minutes=1),
    )

    assert reopened.cancelled_task_ids == (task.task_id,)
    cancelled = repo.get_task(task.task_id)
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested_at is not None
    assert cancelled.superseded_at is not None
    assert not repo.task_can_continue(task.task_id, claim_token=claim.claim_token)
    with pytest.raises(RepositoryError, match="cancelled or superseded"):
        repo.insert_artifact(
            find_id=find_id, kind="review_reply", title="Too late",
            task_id=task.task_id,
        )


def test_customer_facing_external_action_requires_a_corrective_task():
    repo, business_id, account_id, _, find_id = owner_workspace()
    repo.set_find_status(
        find_id, status="accepted", business_id=business_id,
        actor_account_id=account_id, decided_at=NOW,
    )
    task, _ = approved_task_with_draft(repo, business_id, account_id, find_id)
    execution = repo.start_tool_execution(
        task_id=task.task_id, business_id=business_id,
        tool_name="google_business.publish_post",
        idempotency_key=f"publish:{task.task_id}",
    )
    repo.finish_tool_execution(execution.execution_id, status="succeeded")

    with pytest.raises(RepositoryError, match="corrective task"):
        repo.reconsider_find(
            find_id, business_id=business_id,
            actor_account_id=account_id, reason_code="needs_revision",
        )

    assert repo.get_find_context(business_id, find_id).status == "accepted"
    assert repo.get_task(task.task_id).superseded_at is None


def test_metered_result_cannot_be_rewritten_as_an_undecided_post():
    repo, business_id, account_id, _, find_id = owner_workspace()
    repo.set_find_status(
        find_id, status="accepted", business_id=business_id,
        actor_account_id=account_id, decided_at=NOW,
    )
    repo.insert_ledger_entry(
        business_id, find_id=find_id, verdict="verified",
        predicted_daily_cents=1200, actual_daily_cents=1000,
        period_start=date(2026, 8, 1), period_end=date(2026, 8, 7),
        method="sales", note="Measured",
    )

    with pytest.raises(RepositoryError, match="Meter result"):
        repo.reconsider_find(
            find_id, business_id=business_id,
            actor_account_id=account_id, reason_code="other",
        )
    assert repo.get_find_context(business_id, find_id).status == "accepted"


def test_legacy_accepted_projection_is_backfilled_before_reopening():
    repo, business_id, account_id, _, find_id = owner_workspace(initial_status="accepted")
    assert repo.decision_events(find_id, business_id=business_id) == []

    repo.reconsider_find(
        find_id, business_id=business_id,
        actor_account_id=account_id, reason_code="other",
    )

    events = list(reversed(repo.decision_events(find_id, business_id=business_id)))
    assert [event.event_type for event in events] == [DECISION_ACCEPTED, DECISION_REOPENED]
    assert events[0].source == "legacy_projection_backfill"
    assert events[0].data["inferred_from_find_projection"] is True


def test_decision_endpoint_reopens_without_dispatching_a_new_maker_task():
    repo, business_id, account_id, token, find_id = owner_workspace()
    repo.set_find_status(
        find_id, status="accepted", business_id=business_id,
        actor_account_id=account_id, decided_at=NOW,
    )
    event = {
        "headers": {"Authorization": f"Bearer {token}"},
        "pathParameters": {"find_id": find_id},
        "body": json.dumps({
            "decision": "reconsider",
            "reason_code": "needs_revision",
            "reason_note": "The offer needs a different price",
        }),
    }

    response = record_decision(event, repo=repo, now=NOW + timedelta(minutes=5))
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["status"] == "proposed"
    assert body["previous_cycle"] == 1
    assert body["decision_cycle"] == 2
    assert body["maker"] == "cancelled_or_superseded"
    assert repo.list_dispatchable_tasks() == []


def test_chat_reconsider_is_zero_token_and_stored_as_owner_memory():
    repo, business_id, account_id, _, find_id = owner_workspace()
    repo.set_find_status(
        find_id, status="accepted", business_id=business_id,
        actor_account_id=account_id, decided_at=NOW,
    )

    response = perform_reconsider(
        repo=repo, business_id=business_id, find_id=find_id,
        request_text="Put this back in For You.", timestamp=NOW + timedelta(minutes=2),
        requested_by_account_id=account_id,
    )
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["action"]["type"] == "reconsider"
    assert body["tokens"] == {"input": 0, "output": 0, "total": 0}
    assert body["status"] == "proposed"
    assert repo.count_chat_messages(business_id) == 2
    run = repo.recent_runs(business_id, limit=1)[0]
    assert run.input_tokens == 0
    assert run.output_tokens == 0
    assert '"type":"reconsider"' in run.note


def test_owner_ui_exposes_reconsideration_without_deleting_history():
    source = (Path(__file__).resolve().parents[2] / "site" / "app.html").read_text(
        encoding="utf-8"
    )
    assert "Return this move to For You" in source
    assert "Decision history is append-only" in source
    assert 'decision: "reconsider"' in source
    assert "Reopened · previously approved" in source
    assert "Prior work remains in history" in source
