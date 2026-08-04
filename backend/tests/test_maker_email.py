"""The first external Maker tool sends one owner-review email with a receipt."""

from datetime import date, datetime, timezone

from brasstacks.handlers.maker_email import notify
from brasstacks.repository import EvidenceRef, InMemoryRepository

NOW = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


class FakeSes:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("SES unavailable")
        return {"MessageId": "ses-message-123"}


def completed_task(repo, *, owner_email=None):
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    obs = repo.insert_observation(
        business_id, content="Groups ask for a package", kind="review",
        embedding=VECTOR, observed_at=NOW,
    )
    find_id = repo.insert_find_with_evidence(
        business_id, title="Create a group package", summary="Group package",
        rationale="Demand exists", move="Create menu and booking message", emoji="↗",
        predicted_daily_cents=2000, confidence=.8,
        verify_after=date(2026, 8, 20), status="accepted",
        evidence=[EvidenceRef(obs, .9)],
    )
    account_id = None
    if owner_email:
        account_id = repo.create_account(
            business_id, username="owner", password_hash="not-used"
        )
        repo.update_account_profile(
            account_id, display_name="Owner", email=owner_email,
        )
    task = repo.create_or_get_maker_task(
        business_id, find_id=find_id, requested_by_account_id=account_id,
    )
    claim = repo.claim_task(task.task_id, worker_id="maker")
    artifact_id = repo.insert_artifact(
        find_id=find_id, kind="review_reply", title="Group package kit",
        preview="A concise preview", body="# Group package\n\nPost this message.",
        task_id=task.task_id, idempotency_key=f"artifact:{task.task_id}",
    )
    repo.complete_task(
        task.task_id, claim_token=claim.claim_token,
        output_artifact_id=artifact_id,
    )
    return repo, task.task_id


def configured_env(**overrides):
    env = {
        "MAKER_EMAIL_ENABLED": "true",
        "MAKER_EMAIL_FROM": "verified@example.com",
        "MAKER_REVIEW_EMAIL": "virtual.icfd@gmail.com",
        "BRASSTACKS_SITE_URL": "https://app.example.com",
    }
    env.update(overrides)
    return env


def test_completed_draft_is_sent_to_the_fixed_owner_review_inbox():
    repo, task_id = completed_task(InMemoryRepository())
    ses = FakeSes()

    result = notify(
        repo=repo, ses_client=ses, task_id=task_id, env=configured_env()
    )

    assert result["status"] == "succeeded"
    assert result["external_reference"] == "ses-message-123"
    assert len(ses.calls) == 1
    sent = ses.calls[0]
    assert sent["Source"] == "verified@example.com"
    assert sent["Destination"]["ToAddresses"] == ["virtual.icfd@gmail.com"]
    text = sent["Message"]["Body"]["Text"]["Data"]
    assert "Your Maker draft is ready" in text
    assert "YOUR NEXT STEP" in text
    assert "# Group package" not in text
    assert "Nothing has been published or sent to customers" in text
    assert f"https://app.example.com/app/?task={task_id}" in text
    [receipt] = repo.tool_executions(task_id)
    assert receipt.status == "succeeded"
    assert receipt.external_reference == "ses-message-123"


def test_duplicate_workflow_notification_does_not_send_a_second_email():
    repo, task_id = completed_task(InMemoryRepository())
    ses = FakeSes()

    first = notify(repo=repo, ses_client=ses, task_id=task_id, env=configured_env())
    second = notify(repo=repo, ses_client=ses, task_id=task_id, env=configured_env())

    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert len(ses.calls) == 1
    assert len(repo.tool_executions(task_id)) == 1


def test_email_can_be_disabled_without_failing_the_completed_maker_task():
    repo, task_id = completed_task(InMemoryRepository())
    ses = FakeSes()

    result = notify(
        repo=repo, ses_client=ses, task_id=task_id,
        env=configured_env(MAKER_EMAIL_ENABLED="false"),
    )

    assert result["status"] == "skipped"
    assert result["data"]["reason"] == "email_disabled"
    assert ses.calls == []
    assert repo.get_task(task_id).status == "completed"


def test_provider_failure_is_recorded_without_resending_automatically():
    repo, task_id = completed_task(InMemoryRepository())
    ses = FakeSes(fail=True)

    first = notify(repo=repo, ses_client=ses, task_id=task_id, env=configured_env())
    assert first["status"] == "failed"
    assert len(ses.calls) == 1
    [receipt] = repo.tool_executions(task_id)
    assert receipt.status == "failed"
    assert "SES unavailable" in receipt.error


def test_profile_email_overrides_the_global_test_recipient():
    repo, task_id = completed_task(
        InMemoryRepository(), owner_email="peter.flp.2006@gmail.com"
    )
    ses = FakeSes()

    result = notify(
        repo=repo, ses_client=ses, task_id=task_id,
        env=configured_env(MAKER_REVIEW_EMAIL="fallback@example.com"),
    )

    assert result["status"] == "succeeded"
    assert ses.calls[0]["Destination"]["ToAddresses"] == [
        "peter.flp.2006@gmail.com"
    ]
    assert result["data"]["recipient"] == "peter.flp.2006@gmail.com"
