"""The Maker — the agent that does the work, and where the work goes.

This is the only agent that produces something the owner can hand to a customer,
and it is the reason S3 appears in the AWS disclosure at all. Its scope is
deliberately one artifact type; the value is in the loop being complete, not in
the variety.

Two constraints shape everything here:

* **It drafts; it never sends.** `PRODUCT.md` commits to the owner holding the
  leash — the agent proposes and drafts, and nothing reaches a customer without
  her. An artifact is a draft by construction, not by convention.
* **A failed upload must not lose the draft.** The text is the deliverable; S3
  is where a copy lives. Dropping a good draft because a bucket was briefly
  unavailable would be the wrong trade, so the row is written either way and
  records honestly that it has no location.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.agents.maker import (
    MAKER_SYSTEM_PROMPT,
    build_prompt,
    next_undrafted_find,
    run_maker,
)
from brasstacks.artifacts import (
    ArtifactStoreError,
    FakeArtifactStore,
    S3ArtifactStore,
    StoredLocation,
)
from brasstacks.providers import FakeReasoner, ModelRefusedError
from brasstacks.repository import EvidenceRef, InMemoryRepository, StoredArtifact

TODAY = date(2026, 7, 28)


def vec(*leading):
    v = list(leading) + [0.0] * (1024 - len(leading))
    return v[:1024]


def a_business(repo):
    return repo.create_business(name="Rosa's Trattoria", category="restaurant")


def a_find(repo, business_id, *, status="accepted", title="Reply to low reviews"):
    observation_id = repo.insert_observation(
        business_id, content="Nobody ever replies to our reviews.",
        kind="review", embedding=vec(1.0),
        observed_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    return repo.insert_find_with_evidence(
        business_id, title=title,
        rationale="Unanswered reviews read as indifference.",
        move="I will draft a reply to each review from the last 30 days.",
        emoji="✍️", predicted_daily_cents=1500, confidence=0.6,
        verify_after=TODAY + timedelta(days=14), status=status,
        evidence=[EvidenceRef(observation_id, 0.51)])


DRAFT = {
    "title": "Draft replies for 4 recent reviews",
    "body": "Thank you for telling us about the wait on Saturday. "
            "You were right, and we have changed how we quote times.",
}


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

class StubS3:
    def __init__(self, fail=False):
        self.fail = fail
        self.puts = []

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError("bucket does not exist")
        self.puts.append(kwargs)
        return {}


class TestS3ArtifactStore:
    def test_writes_the_body_to_the_configured_bucket(self):
        stub = StubS3()
        store = S3ArtifactStore(client=stub, bucket="brasstacks-artifacts")

        where = store.put(key="finds/abc/replies.md", body="Dear guest,")

        assert where == StoredLocation(bucket="brasstacks-artifacts",
                                       key="finds/abc/replies.md")
        assert stub.puts[0]["Bucket"] == "brasstacks-artifacts"
        assert stub.puts[0]["Key"] == "finds/abc/replies.md"
        assert stub.puts[0]["Body"] == b"Dear guest,"

    def test_failure_is_wrapped_with_the_key_that_failed(self):
        store = S3ArtifactStore(client=StubS3(fail=True), bucket="b")
        with pytest.raises(ArtifactStoreError, match="finds/abc"):
            store.put(key="finds/abc/replies.md", body="x")


# --------------------------------------------------------------------------
# Choosing what to draft
# --------------------------------------------------------------------------

class TestNextUndraftedFind:
    def test_picks_an_accepted_find(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        find_id = a_find(repo, business_id, status="accepted")

        assert next_undrafted_find(repo, business_id).find_id == find_id

    def test_ignores_finds_the_owner_has_not_accepted(self):
        # The agent drafts what she said yes to. Drafting for a proposal she
        # has not decided on spends money on work she may not want.
        repo = InMemoryRepository()
        business_id = a_business(repo)
        a_find(repo, business_id, status="proposed")

        assert next_undrafted_find(repo, business_id) is None

    def test_skips_a_find_that_already_has_an_artifact(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        find_id = a_find(repo, business_id, status="accepted")
        repo.insert_artifact(find_id=find_id, kind="review_reply",
                             title="Already drafted")

        assert next_undrafted_find(repo, business_id) is None


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------

class TestRunMaker:
    def test_stores_the_draft_and_records_where_it_went(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        find_id = a_find(repo, business_id)
        store = FakeArtifactStore(bucket="brasstacks-artifacts")

        result = run_maker(repo=repo, reasoner=FakeReasoner([DRAFT]),
                           store=store, business_id=business_id,
                           find=next_undrafted_find(repo, business_id))

        assert result.artifact_id
        stored = repo.get_artifacts(find_id)[0]
        assert stored.title == "Draft replies for 4 recent reviews"
        assert stored.s3_bucket == "brasstacks-artifacts"
        assert find_id in stored.s3_key
        assert store.puts[0]["body"].startswith("Thank you for telling us")

    def test_preview_carries_the_opening_of_the_draft(self):
        # The UI shows the preview inline; it must be the real text, not a
        # summary of it, or the owner is approving something she cannot see.
        repo = InMemoryRepository()
        business_id = a_business(repo)
        find_id = a_find(repo, business_id)

        run_maker(repo=repo, reasoner=FakeReasoner([DRAFT]),
                  store=FakeArtifactStore(), business_id=business_id,
                  find=next_undrafted_find(repo, business_id))

        preview = repo.get_artifacts(find_id)[0].preview
        assert DRAFT["body"].startswith(preview.rstrip("…"))

    def test_a_failed_upload_still_keeps_the_draft(self):
        # The text is the deliverable; S3 is where a copy lives. Losing a good
        # draft because a bucket was unavailable is the wrong trade.
        repo = InMemoryRepository()
        business_id = a_business(repo)
        find_id = a_find(repo, business_id)

        result = run_maker(repo=repo, reasoner=FakeReasoner([DRAFT]),
                           store=FakeArtifactStore(fail=True),
                           business_id=business_id,
                           find=next_undrafted_find(repo, business_id))

        stored = repo.get_artifacts(find_id)[0]
        assert result.artifact_id
        assert stored.s3_bucket is None
        assert stored.s3_key is None
        assert stored.preview  # the draft itself survived
        assert "could not be uploaded" in repo.recent_runs(
            business_id, limit=1)[0].note

    def test_writes_a_finished_maker_run(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        a_find(repo, business_id)

        run_maker(repo=repo, reasoner=FakeReasoner([DRAFT]),
                  store=FakeArtifactStore(), business_id=business_id,
                  find=next_undrafted_find(repo, business_id))

        run = repo.recent_runs(business_id, limit=1)[0]
        assert run.agent == "maker"
        assert run.status == "ok"
        assert run.finished_at is not None

    def test_a_model_refusal_finishes_the_run_and_writes_nothing(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        find_id = a_find(repo, business_id)

        result = run_maker(
            repo=repo, reasoner=FakeReasoner([ModelRefusedError("declined")]),
            store=FakeArtifactStore(), business_id=business_id,
            find=next_undrafted_find(repo, business_id))

        assert result.artifact_id is None
        assert repo.get_artifacts(find_id) == []
        run = repo.recent_runs(business_id, limit=1)[0]
        assert run.status == "failed"
        assert run.finished_at is not None

    def test_nothing_to_draft_is_a_normal_night(self):
        # No accepted find is the common case, not an error. It must not mark
        # the night as failed.
        repo = InMemoryRepository()
        business_id = a_business(repo)

        result = run_maker(repo=repo, reasoner=FakeReasoner([]),
                           store=FakeArtifactStore(),
                           business_id=business_id, find=None)

        assert result.artifact_id is None
        assert result.error is None
        assert repo.recent_runs(business_id, limit=1)[0].status == "ok"

    def test_the_find_is_named_in_the_prompt(self):
        # The draft has to be about the move the owner accepted, not a generic
        # piece of restaurant marketing.
        repo = InMemoryRepository()
        business_id = a_business(repo)
        a_find(repo, business_id, title="Reply to every recent low review")
        reasoner = FakeReasoner([DRAFT])

        run_maker(repo=repo, reasoner=reasoner, store=FakeArtifactStore(),
                  business_id=business_id,
                  find=next_undrafted_find(repo, business_id))

        prompt = reasoner.calls[0]["user"]
        assert "Reply to every recent low review" in prompt
        assert "draft a reply to each review" in prompt

    def test_the_prompt_forbids_sending(self):
        # PRODUCT.md: the agent drafts, the owner decides. That has to be in
        # the prompt, not just the docs.
        assert "draft" in MAKER_SYSTEM_PROMPT.lower()
        assert "never send" in MAKER_SYSTEM_PROMPT.lower()

    def test_missing_inputs_are_one_line_questions_with_an_answer_format(self):
        prompt = MAKER_SYSTEM_PROMPT

        assert "at most three required questions" in prompt
        assert "expected answer format" in prompt
        assert "answerable in one line" in prompt
        assert "answer the numbered questions in one reply" in prompt.lower()
        assert "Do not generate a usable-looking draft" in prompt


def test_google_business_draft_gets_a_server_bounded_action_manifest():
    repo = InMemoryRepository()
    business_id = a_business(repo)
    find_id = a_find(repo, business_id, title="Publish the lunch set")
    payload = {
        "title": "Lunch set post",
        "body": "Try our weekday lunch set.",
        "summary": "A Google Business post ready for review.",
        "review_state": "ready_for_review",
        "owner_action": "Review the exact post before publishing.",
        "owner_questions": [],
        "artifact_type": "google_business_post",
        "sections": [{"title": "Post", "content": "Try our weekday lunch set."}],
    }

    run_maker(repo=repo, reasoner=FakeReasoner([payload]),
              store=FakeArtifactStore(), business_id=business_id,
              find=next_undrafted_find(repo, business_id))

    artifact = repo.get_artifacts(find_id)[0]
    assert artifact.metadata["action_manifest"] == {
        "version": 1,
        "action_type": "google_business.publish_post",
        "state": "ready_for_approval",
        "requires_owner_confirmation": True,
        "content_source": "artifact.body",
    }
    use_context = artifact.metadata["use_context"]
    assert use_context["artifact_type"] == "google_business_post"
    assert use_context["surface"] == "Google Business Profile"
    assert use_context["placement"] == "A public update on the selected business location"
    assert use_context["audience"] == "People viewing that business profile on Google"
    assert use_context["draft_state"] == "Not published"
    assert use_context["owner_gate"] == (
        "Nothing becomes public until you review and confirm publishing."
    )
    assert artifact.metadata["use_context"]["surface"] == "Google Business Profile"
    assert artifact.metadata["use_context"]["draft_state"] == "Not published"
    assert "confirm publishing" in artifact.metadata["use_context"]["owner_gate"]


def test_drafts_needing_owner_input_are_never_executable():
    repo = InMemoryRepository()
    business_id = a_business(repo)
    find_id = a_find(repo, business_id, title="Publish the lunch set")
    payload = {
        "title": "Lunch set post",
        "body": "Draft waiting for the price.",
        "summary": "Price required.",
        "review_state": "needs_owner_input",
        "owner_action": "Confirm the lunch set price.",
        "owner_questions": ["What is the exact lunch set price?"],
        "artifact_type": "google_business_post",
        "sections": [{"title": "Draft", "content": "Waiting for price."}],
    }

    run_maker(repo=repo, reasoner=FakeReasoner([payload]),
              store=FakeArtifactStore(), business_id=business_id,
              find=next_undrafted_find(repo, business_id))

    artifact = repo.get_artifacts(find_id)[0]
    assert artifact.metadata["action_manifest"] is None
    assert artifact.owner_action == (
        "Answer the 1 required item below in one reply. "
        "Maker will use your reply to create the next revision."
    )
    assert artifact.metadata["sections"] == []


def test_revision_prompt_pairs_compact_owner_answers_with_the_current_questions():
    find = type("Find", (), {
        "title": "Publish the lunch set",
        "move": "Prepare a Google Business update for the weekday lunch set.",
    })()
    previous = StoredArtifact(
        artifact_id="artifact-1",
        find_id="find-1",
        kind="google_business_post",
        title="Lunch set draft",
        created_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        body="Weekday lunch set draft awaiting details.",
        metadata={
            "artifact_type": "google_business_post",
            "owner_questions": [
                "What exact price should the lunch set show? Example: $18.",
                "Which days and hours is it available? Example: Mon–Fri, 11:30 AM–2 PM.",
            ],
        },
    )

    prompt = build_prompt(
        find,
        revision_instruction=(
            "Answers for Maker — Google Business Profile:\n"
            "1. What exact price should the lunch set show? → $18\n"
            "2. Which days and hours is it available? → Mon–Fri, 11:30 AM–2 PM"
        ),
        previous_artifact=previous,
    )

    assert "REQUIRED QUESTIONS FROM THE CURRENT REVISION" in prompt
    assert "1. What exact price should the lunch set show?" in prompt
    assert "2. Which days and hours is it available?" in prompt
    assert "Answers for Maker — Google Business Profile" in prompt
    assert "CURRENT DRAFT TO REVISE" in prompt


def test_revision_prompt_pairs_guided_answers_with_the_original_required_questions():
    repo = InMemoryRepository()
    business_id = a_business(repo)
    a_find(repo, business_id, title="Publish the weekday lunch post")
    find = next_undrafted_find(repo, business_id)
    blocked = {
        "title": "Weekday lunch post",
        "body": "Waiting for the confirmed price.",
        "summary": "The price and hours are still required.",
        "review_state": "needs_owner_input",
        "owner_questions": [
            "What exact price should the post show? (example: $18)",
            "Which days and hours apply? (example: Mon–Fri, 11:30 AM–2 PM)",
        ],
        "artifact_type": "google_business_post",
        "sections": [],
    }
    run_maker(
        repo=repo, reasoner=FakeReasoner([blocked]), store=FakeArtifactStore(),
        business_id=business_id, find=find,
    )
    previous = repo.get_artifacts(find.find_id)[0]
    reasoner = FakeReasoner([{
        "title": "Weekday lunch post",
        "body": "Weekday lunch is $18, Monday through Friday from 11:30 AM to 2 PM.",
        "summary": "A complete Google Business Profile post.",
        "review_state": "ready_for_review",
        "owner_questions": [],
        "artifact_type": "google_business_post",
        "sections": [{"title": "Post", "content": "Weekday lunch is $18."}],
    }])

    run_maker(
        repo=repo, reasoner=reasoner, store=FakeArtifactStore(),
        business_id=business_id, find=find, revision=2,
        revision_instruction=(
            "Answers for Maker — Google Business Profile:\n"
            "1. What exact price should the post show? → $18\n"
            "2. Which days and hours apply? → Mon–Fri, 11:30 AM–2 PM"
        ),
        previous_artifact=previous,
    )

    prompt = reasoner.calls[0]["user"]
    assert "REQUIRED QUESTIONS FROM THE CURRENT REVISION" in prompt
    assert "What exact price should the post show?" in prompt
    assert "Which days and hours apply?" in prompt
    assert "Answers for Maker — Google Business Profile" in prompt
