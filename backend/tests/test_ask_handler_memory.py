"""Authenticated Ask turns use compact CockroachDB conversation memory."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from brasstacks.auth import token_fingerprint
from brasstacks.handlers.ask import (
    _embed_with_retry,
    answer_question,
    build_context_question,
    read_chat_history,
    undo_pass_action,
)
from brasstacks.providers import Answer, FakeAsker, ModelUsage, ToolCall
from brasstacks.repository import EvidenceRef, InMemoryRepository

NOW = datetime(2026, 8, 2, 19, 30, tzinfo=timezone.utc)
VECTOR = [1.0, 0.0] + [0.0] * 1022


class Embedder:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("Titan temporarily unavailable")
        return [list(VECTOR) for _ in texts]


class Settings:
    anthropic_model_id = "claude-opus-5"
    cockroach_cluster_id = "cluster-1"
    cockroach_database = "defaultdb"


class FakeQueue:
    def __init__(self):
        self.calls = []

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": f"message-{len(self.calls)}"}


class FailAssistantWriteRepository(InMemoryRepository):
    def insert_chat_message(self, business_id, *, role, **kwargs):
        if role == "assistant":
            raise RuntimeError("conversation write unavailable")
        return super().insert_chat_message(business_id, role=role, **kwargs)


def owner_repo():
    repo = InMemoryRepository()
    business_id = repo.create_business(
        name="Rosa's Trattoria", category="restaurant", city="Columbus")
    account_id = repo.create_account(
        business_id, username="rosa", password_hash="not-used-here")
    token = "owner-session-token"
    repo.create_session(
        token_fingerprint(token), business_id=business_id,
        account_id=account_id, expires_at=NOW + timedelta(days=1))
    return repo, business_id, token


def owner_repo_with(repo):
    business_id = repo.create_business(
        name="Rosa's Trattoria", category="restaurant", city="Columbus")
    account_id = repo.create_account(
        business_id, username="rosa", password_hash="not-used-here")
    token = "owner-session-token"
    repo.create_session(
        token_fingerprint(token), business_id=business_id,
        account_id=account_id, expires_at=NOW + timedelta(days=1))
    return repo, business_id, token


def event(token, question="Can we do this without more staff?", find_id=None):
    body = {"question": question}
    if find_id:
        body["find_id"] = find_id
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "body": json.dumps(body),
    }


def undo_event(token, find_id):
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "body": json.dumps({
            "action": "undo_pass",
            "question": "I changed my mind. Do it.",
            "find_id": find_id,
        }),
    }


def history_event(token, find_id=None):
    return {
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {token}"},
        "queryStringParameters": {"find_id": find_id} if find_id else {},
    }


def response_body(response):
    return json.loads(response["body"])


def test_context_prompt_is_bounded_and_labels_owner_memory_as_context():
    from brasstacks.repository import ChatMessage, OwnerRule

    prompt = build_context_question(
        question="What should I do?",
        find_context=None,
        facts=["The business serves families"],
        rules=[OwnerRule("r1", "Never add weekend headcount", None)],
        recent_messages=[ChatMessage(
            "m1", "user", "Keep Sunday simple", NOW)],
        relevant_messages=[ChatMessage(
            "m2", "user", "Margin matters more than volume", NOW, similarity=.91)],
        artifact_context={
            "title": "Weekday lunch post",
            "revision": 2,
            "review_state": "needs_owner_input",
            "use_context": {
                "surface": "Google Business Profile",
                "placement": "A public post on the selected business location",
                "audience": "People viewing that business profile",
                "draft_state": "Not published",
                "owner_gate": "Review and confirm before publishing.",
            },
        },
    )

    assert "DURABLE BUSINESS PROFILE" in prompt
    assert "OWNER GUARDRAILS" in prompt
    assert "SEMANTICALLY RELEVANT PRIOR OWNER MEMORY" in prompt
    assert "RECENT CONVERSATION" in prompt
    assert "intent, not independent proof" in prompt
    assert "CURRENT MAKER DRAFT" in prompt
    assert "intended surface: Google Business Profile" in prompt
    assert "current delivery state: Not published" in prompt
    assert "owner gate: Review and confirm before publishing." in prompt


def test_one_turn_retrieves_memory_records_tokens_and_persists_both_messages():
    repo, business_id, token = owner_repo()
    repo.insert_business_fact(
        business_id, fact="Dinner is the highest-margin service",
        source="onboarding", embedding=VECTOR)
    repo.insert_owner_rule(
        business_id, rule="Never add weekend headcount")
    repo.insert_chat_message(
        business_id, role="user",
        content="I prefer prep changes over adding a server",
        created_at=NOW - timedelta(days=3), embedding=VECTOR)

    asker = FakeAsker([Answer(
        text="Use a prep change first; it respects your staffing rule.",
        tool_calls=(ToolCall(
            name="select_query",
            input={"sql": "SELECT rule FROM owner_rule WHERE business_id = $1"}),),
    )])
    asker.last_usage = ModelUsage(input_tokens=410, output_tokens=62)
    embedder = Embedder()

    response = answer_question(
        event(token), repo=repo, asker=asker, embedder=embedder,
        settings=Settings(), now=NOW)

    assert response["statusCode"] == 200
    body = response_body(response)
    assert body["answer"].startswith("Use a prep change")
    assert body["queried_the_cluster"] is True
    assert body["tokens"] == {"input": 410, "output": 62, "total": 472}
    assert body["memory"]["stored"] is True
    assert body["memory"]["relevantMessages"] >= 1
    assert body["memory"]["embeddingCalls"] == 1
    assert repo.count_chat_messages(business_id) == 3

    sent = asker.calls[0]["question"]
    assert "Dinner is the highest-margin service" in sent
    assert "Never add weekend headcount" in sent
    assert "I prefer prep changes" in sent
    assert "OWNER'S CURRENT QUESTION" in sent

    history = response_body(read_chat_history(
        history_event(token), repo=repo, now=NOW))
    assert [message["role"] for message in history["messages"]][-2:] == [
        "user", "assistant"]
    assert history["memory"]["totalStoredMessages"] == 3


def test_embedding_failure_falls_back_to_recent_memory_instead_of_silencing_chat():
    repo, business_id, token = owner_repo()
    repo.insert_chat_message(
        business_id, role="user", content="Keep changes under $100",
        created_at=NOW - timedelta(days=1), embedding=VECTOR)
    asker = FakeAsker([Answer(text="I can work within that cap.", tool_calls=())])

    response = answer_question(
        event(token, "What can we try next?"), repo=repo, asker=asker,
        embedder=Embedder(fail=True), settings=Settings(), now=NOW)

    assert response["statusCode"] == 200
    body = response_body(response)
    assert body["memory"]["embeddingFallback"] is True
    assert body["memory"]["embeddingCalls"] == 0
    assert "Keep changes under $100" in asker.calls[0]["question"]
    assert repo.count_chat_messages(business_id) == 3


def test_recent_continuity_is_owner_wide_even_when_drawer_is_find_scoped():
    repo, business_id, token = owner_repo()
    repo.insert_chat_message(
        business_id, role="user", content="Never schedule me before 10 AM",
        created_at=NOW - timedelta(days=2), embedding=VECTOR,
        find_id="an-older-recommendation",
    )
    asker = FakeAsker([Answer(text="I will keep the 10 AM constraint.", tool_calls=())])

    response = answer_question(
        event(token, "Can I do this tomorrow?", find_id=None),
        repo=repo, asker=asker, embedder=Embedder(fail=True),
        settings=Settings(), now=NOW,
    )

    assert response["statusCode"] == 200
    assert "Never schedule me before 10 AM" in asker.calls[0]["question"]
    assert response_body(response)["memory"]["analystBridge"] is True


def test_completed_answer_is_returned_if_assistant_memory_write_temporarily_fails():
    repo, business_id, token = owner_repo_with(FailAssistantWriteRepository())
    asker = FakeAsker([Answer(text="Here is the answer.", tool_calls=())])

    response = answer_question(
        event(token, "Help me decide"), repo=repo, asker=asker,
        embedder=Embedder(), settings=Settings(), now=NOW,
    )

    assert response["statusCode"] == 200
    body = response_body(response)
    assert body["answer"] == "Here is the answer."
    assert body["memory"]["stored"] is False
    assert body["memory"]["storedThisTurn"] == 1
    assert "conversation write unavailable" in body["memory"]["storageError"]
    assert repo.count_chat_messages(business_id) == 1


class FlakyEmbedder:
    """Fails its first N calls, then works — a transient blip."""

    def __init__(self, *, fail_first=1):
        self.fail_first = fail_first
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("Titan blip")
        return [list(VECTOR) for _ in texts]


def test_embed_retry_clears_a_single_transient_failure():
    embedder = FlakyEmbedder(fail_first=1)
    vector = _embed_with_retry(embedder, "hello")
    assert vector is not None
    assert embedder.calls == 2  # first failed, retry succeeded


def test_embed_retry_gives_up_after_its_attempts():
    embedder = FlakyEmbedder(fail_first=5)
    assert _embed_with_retry(embedder, "hello", attempts=2) is None


def test_a_truncated_answer_is_flagged_in_the_response():
    repo, _, token = owner_repo()
    asker = FakeAsker([Answer(text="The first part of a long answer …",
                              tool_calls=(), truncated=True)])
    response = answer_question(event(token, "Explain everything"), repo=repo,
                              asker=asker, embedder=Embedder(),
                              settings=Settings(), now=NOW)
    assert response_body(response)["truncated"] is True


def test_recommendation_context_is_tenant_scoped_and_enters_the_compact_prompt():
    repo, business_id, token = owner_repo()
    observation_id = repo.insert_observation(
        business_id, content="Groups ask for a fixed menu", kind="review",
        embedding=VECTOR, observed_at=NOW)
    find_id = repo.insert_find_with_evidence(
        business_id, title="Create a group package",
        summary="Package the group meal.",
        rationale="Repeated group enquiries are being lost.",
        move="Set a package price. Add reservations.", emoji="↗",
        predicted_daily_cents=2200, confidence=.72,
        verify_after=date(2026, 8, 20),
        evidence=[EvidenceRef(observation_id, .9)])
    asker = FakeAsker([Answer(text="Start with the package price.", tool_calls=())])

    response = answer_question(
        event(token, find_id=find_id), repo=repo, asker=asker,
        embedder=Embedder(), settings=Settings(), now=NOW)

    assert response["statusCode"] == 200
    sent = asker.calls[0]["question"]
    assert "CURRENT FOR YOU RECOMMENDATION" in sent
    assert find_id in sent
    assert "modelled impact" in sent

    other = repo.create_business(name="Other", category="restaurant")
    other_account = repo.create_account(other, username="other", password_hash="x")
    other_token = "other-token"
    repo.create_session(
        token_fingerprint(other_token), business_id=other,
        account_id=other_account, expires_at=NOW + timedelta(days=1))
    denied = answer_question(
        event(other_token, find_id=find_id), repo=repo,
        asker=FakeAsker([]), embedder=Embedder(), settings=Settings(), now=NOW)
    assert denied["statusCode"] == 404


def test_current_maker_draft_context_names_where_the_draft_will_be_used():
    repo, business_id, token = owner_repo()
    observation_id = repo.insert_observation(
        business_id, content="Guests ask about the weekday lunch set", kind="review",
        embedding=VECTOR, observed_at=NOW)
    find_id = repo.insert_find_with_evidence(
        business_id, title="Publish the weekday lunch set",
        summary="Tell local customers about the offer.",
        rationale="The offer is hard to discover.",
        move="Prepare a Google Business Profile post.", emoji="↗",
        predicted_daily_cents=1800, confidence=.74,
        verify_after=date(2026, 8, 20), status="accepted",
        evidence=[EvidenceRef(observation_id, .9)])
    repo.insert_artifact(
        find_id=find_id,
        kind="google_business_post",
        title="Weekday lunch set post",
        body="Try our weekday lunch set.",
        review_state="needs_owner_input",
        metadata={
            "artifact_type": "google_business_post",
            "owner_questions": ["What exact price should the post show?"],
        },
        revision=2,
    )
    asker = FakeAsker([Answer(
        text="Answer:\nThis is for your Google Business Profile and has not been published.",
        tool_calls=(),
    )])

    response = answer_question(
        event(token, "Where will this draft be used?", find_id=find_id),
        repo=repo, asker=asker, embedder=Embedder(),
        settings=Settings(), now=NOW,
    )

    assert response["statusCode"] == 200
    sent = asker.calls[0]["question"]
    assert "CURRENT MAKER DRAFT" in sent
    assert "intended surface: Google Business Profile" in sent
    assert "placement: A public update on the selected business location" in sent
    assert "current delivery state: Not published" in sent
    assert "Nothing becomes public until you review and confirm publishing" in sent


def test_compact_context_keeps_memory_rules_even_when_profile_is_large():
    prompt = build_context_question(
        question="How should I handle this?",
        find_context=None,
        facts=["fact " + ("x" * 600)] * 12,
        rules=[],
        recent_messages=[],
        relevant_messages=[],
    )

    assert len(prompt) <= 3600
    assert "OWNER'S CURRENT QUESTION" in prompt
    assert "MEMORY USE RULES" in prompt
    assert "intent, not independent proof" in prompt


def rejected_find(repo, business_id):
    observation_id = repo.insert_observation(
        business_id,
        content="Customers ask for a fixed group menu",
        kind="review",
        embedding=VECTOR,
        observed_at=NOW - timedelta(days=5),
    )
    return repo.insert_find_with_evidence(
        business_id,
        title="Create a group package",
        summary="Package the group meal.",
        rationale="Repeated group enquiries are being lost.",
        move="Set a package price and add reservations.",
        emoji="↗",
        predicted_daily_cents=2200,
        confidence=.72,
        verify_after=date(2026, 8, 20),
        status="rejected",
        decided_at=NOW - timedelta(days=1),
        evidence=[EvidenceRef(observation_id, .9)],
    )


def test_undo_pass_button_action_is_zero_token_durable_and_starts_maker():
    repo, business_id, token = owner_repo()
    find_id = rejected_find(repo, business_id)
    queue = FakeQueue()

    response = undo_pass_action(
        undo_event(token, find_id),
        repo=repo,
        queue_client=queue,
        maker_queue_url="https://sqs.example/maker.fifo",
        model_id=Settings.anthropic_model_id,
        now=NOW,
    )

    assert response["statusCode"] == 200
    body = response_body(response)
    assert body["action"] == {
        "type": "undo_pass", "status": "completed", "changed": True}
    assert body["status"] == "accepted"
    assert body["previous_status"] == "rejected"
    assert body["tokens"] == {"input": 0, "output": 0, "total": 0}
    assert body["maker"] == "queued"
    assert repo.get_find_context(business_id, find_id).status == "accepted"
    assert repo.count_chat_messages(business_id) == 2
    assert len(queue.calls) == 1
    payload = json.loads(queue.calls[0]["MessageBody"])
    assert payload["business_id"] == business_id
    assert payload["find_id"] == find_id
    assert payload["task_id"] == body["maker_task"]["task_id"]
    run = repo.recent_runs(business_id, limit=1)[0]
    assert '"previous_status":"rejected"' in run.note
    assert '"status":"accepted"' in run.note
    assert '"model_tokens":0' in run.note


def test_clear_changed_mind_message_undoes_pass_without_calling_the_model():
    repo, business_id, token = owner_repo()
    find_id = rejected_find(repo, business_id)
    asker = FakeAsker([])
    embedder = Embedder()

    response = answer_question(
        event(token, "I changed my mind. Do it.", find_id=find_id),
        repo=repo,
        asker=asker,
        embedder=embedder,
        settings=Settings(),
        queue_client=FakeQueue(),
        maker_queue_url="https://sqs.example/maker.fifo",
        now=NOW,
    )

    assert response["statusCode"] == 200
    body = response_body(response)
    assert body["action"]["type"] == "undo_pass"
    assert body["decision"] == "approved"
    assert asker.calls == []
    assert embedder.calls == []
    assert repo.get_find_context(business_id, find_id).status == "accepted"



def test_redo_pass_wording_is_treated_as_an_explicit_changed_mind_action():
    repo, business_id, token = owner_repo()
    find_id = rejected_find(repo, business_id)
    asker = FakeAsker([])
    embedder = Embedder()

    response = answer_question(
        event(token, "Redo my pass", find_id=find_id),
        repo=repo,
        asker=asker,
        embedder=embedder,
        settings=Settings(),
        queue_client=FakeQueue(),
        maker_queue_url="https://sqs.example/maker.fifo",
        now=NOW,
    )

    assert response["statusCode"] == 200
    assert response_body(response)["action"]["type"] == "undo_pass"
    assert asker.calls == []
    assert embedder.calls == []
    assert repo.get_find_context(business_id, find_id).status == "accepted"

def test_vague_undo_question_is_answered_normally_without_changing_status():
    repo, business_id, token = owner_repo()
    find_id = rejected_find(repo, business_id)
    asker = FakeAsker([Answer(
        text="Yes. Use the Undo Pass button in this recommendation's chat.",
        tool_calls=(),
    )])

    response = answer_question(
        event(token, "Can I undo this?", find_id=find_id),
        repo=repo,
        asker=asker,
        embedder=Embedder(),
        settings=Settings(),
        now=NOW,
    )

    assert response["statusCode"] == 200
    assert asker.calls
    assert repo.get_find_context(business_id, find_id).status == "rejected"
