"""The Ask agent emails the owner their own draft.

The agent is read-only about the world — no logins, no publishing, no sending
to third parties. But when the owner asks "email me the draft," the helpful
answer is to hand them the draft by email, to their own confirmed address, and
say plainly that nothing went anywhere else. These tests pin that.
"""

from datetime import date, datetime, timedelta, timezone

import json

from brasstacks.auth import token_fingerprint
from brasstacks.handlers.ask import EMAIL_DRAFT_ACTION, email_draft_action
from brasstacks.repository import EvidenceRef, InMemoryRepository

NOW = datetime(2026, 8, 8, 19, 30, tzinfo=timezone.utc)
VECTOR = [1.0, 0.0] + [0.0] * 1022


class FakeSes:
    def __init__(self):
        self.sent = []

    def __call__(self, *, source, recipient, subject, body, html=None):
        self.sent.append({"source": source, "recipient": recipient,
                          "subject": subject, "body": body, "html": html})
        return f"ses-{len(self.sent)}"


def owner(*, email="owner@asaka.example"):
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Asaka", category="restaurant",
                                       city="Rancho Palos Verdes")
    account_id = repo.create_account(business_id, username="asaka",
                                     password_hash="x", email=email)
    token = "owner-session-token"
    repo.create_session(token_fingerprint(token), business_id=business_id,
                        account_id=account_id, expires_at=NOW + timedelta(days=1))
    return repo, business_id, account_id, token


def a_find(repo, business_id, *, move="Email your host to restore checkout."):
    observation_id = repo.insert_observation(
        business_id, content="Online ordering is down", kind="review",
        embedding=VECTOR, observed_at=NOW - timedelta(days=1))
    return repo.insert_find_with_evidence(
        business_id, title="Restore ordering on asakacatogo.com",
        summary="Checkout is broken.", rationale="The page blocks orders.",
        move=move, emoji="↗", predicted_daily_cents=1600, confidence=.7,
        verify_after=date(2026, 8, 21),
        evidence=[EvidenceRef(observation_id, .9)])


def event(token, *, find_id, email=None, confirmed=False):
    body = {"action": EMAIL_DRAFT_ACTION, "find_id": find_id}
    if email is not None:
        body["email"] = email
    if confirmed:
        body["confirmed"] = True
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"Authorization": f"Bearer {token}"},
        "body": json.dumps(body),
    }


def call(repo, token, *, find_id, email=None, confirmed=False, ses=None,
         source="agent@bt.example"):
    return email_draft_action(
        event(token, find_id=find_id, email=email, confirmed=confirmed),
        repo=repo, email_sender=ses, email_source=source if ses else None,
        now=NOW)


def body_of(response):
    return json.loads(response["body"])


class TestAuth:
    def test_no_session_is_refused(self):
        repo, business_id, _, _ = owner()
        find_id = a_find(repo, business_id)
        response = email_draft_action(
            {"requestContext": {"http": {"method": "POST"}}, "headers": {},
             "body": json.dumps({"action": EMAIL_DRAFT_ACTION, "find_id": find_id})},
            repo=repo, email_sender=FakeSes(), email_source="a@b.example",
            now=NOW)
        assert response["statusCode"] == 401


class TestConfirmingTheProfileEmailFirst:
    def test_my_email_is_resolved_from_the_profile_and_confirmed(self):
        # "send to my email" with a profile email must confirm that address,
        # not ask for one — the bug the owner reported.
        repo, business_id, _, token = owner(email="chef@asaka.example")
        find_id = a_find(repo, business_id)
        ses = FakeSes()
        answer = body_of(call(repo, token, find_id=find_id, ses=ses))
        assert answer["action"]["status"] == "confirm"
        assert answer["action"]["recipient"] == "chef@asaka.example"
        assert "chef@asaka.example" in answer["answer"]
        assert ses.sent == []  # nothing sent until confirmed

    def test_confirming_sends_to_the_profile_address(self):
        repo, business_id, _, token = owner(email="chef@asaka.example")
        find_id = a_find(repo, business_id)
        ses = FakeSes()
        answer = body_of(call(repo, token, find_id=find_id, confirmed=True,
                              ses=ses))
        assert answer["action"]["status"] == "sent"
        assert answer["action"]["recipient"] == "chef@asaka.example"
        assert len(ses.sent) == 1
        assert ses.sent[0]["recipient"] == "chef@asaka.example"

    def test_the_email_carries_the_draft_wording(self):
        repo, business_id, _, token = owner()
        find_id = a_find(repo, business_id,
                         move="Ask your host to switch checkout back on.")
        ses = FakeSes()
        call(repo, token, find_id=find_id, confirmed=True, ses=ses)
        assert "switch checkout back on" in ses.sent[0]["body"]
        assert "Restore ordering" in ses.sent[0]["subject"]

    def test_the_email_says_nothing_was_sent_elsewhere(self):
        # The whole point: the owner must not think the agent contacted their
        # provider. The body says this is theirs to send.
        repo, business_id, _, token = owner()
        find_id = a_find(repo, business_id)
        ses = FakeSes()
        call(repo, token, find_id=find_id, confirmed=True, ses=ses)
        assert "anyone else" in ses.sent[0]["body"]

    def test_the_email_is_styled_html_not_just_text(self):
        # The reported gripe: plain text looked unprofessional. The send now
        # carries a review-ready HTML part with the draft framed inside it.
        repo, business_id, _, token = owner()
        find_id = a_find(repo, business_id,
                         move="Ask your host to switch checkout back on.")
        ses = FakeSes()
        call(repo, token, find_id=find_id, confirmed=True, ses=ses)
        html = ses.sent[0]["html"]
        assert html and "<html" in html.lower()
        assert "BRASS TACKS" in html
        assert "switch checkout back on" in html
        assert ses.sent[0]["subject"].startswith("Review-ready draft:")

    def test_the_maker_artifact_body_wins_over_the_raw_move(self):
        repo, business_id, _, token = owner()
        find_id = a_find(repo, business_id, move="short move line")
        repo.insert_artifact(
            find_id=find_id, kind="general_draft",
            title="Asaka online ordering: restore checkout",
            body="Dear host, please re-enable checkout on asakacatogo.com…",
            review_state="ready_for_review")
        ses = FakeSes()
        call(repo, token, find_id=find_id, confirmed=True, ses=ses)
        assert "please re-enable checkout" in ses.sent[0]["body"]
        assert "restore checkout" in ses.sent[0]["subject"]


class TestTypedAddressWins:
    def test_an_explicit_address_sends_directly_without_a_second_confirm(self):
        repo, business_id, _, token = owner(email="old@asaka.example")
        find_id = a_find(repo, business_id)
        ses = FakeSes()
        answer = body_of(call(repo, token, find_id=find_id,
                              email="new@asaka.example", ses=ses))
        assert answer["action"]["status"] == "sent"
        assert answer["action"]["recipient"] == "new@asaka.example"
        assert ses.sent[0]["recipient"] == "new@asaka.example"


class TestAskingForAnAddress:
    def test_no_profile_email_says_so_and_asks_for_one(self):
        repo, business_id, _, token = owner(email=None)
        find_id = a_find(repo, business_id)
        ses = FakeSes()
        answer = body_of(call(repo, token, find_id=find_id, ses=ses))
        assert answer["action"]["status"] == "needs_email"
        assert ses.sent == []
        # The owner's exact ask: say the profile has none, then ask.
        assert "no email found in your profile" in answer["answer"].lower()


class TestHonestFailureModes:
    def test_without_a_configured_sender_it_says_so(self):
        repo, business_id, _, token = owner()
        find_id = a_find(repo, business_id)
        # No ses / no source configured on this deployment.
        answer = body_of(email_draft_action(
            event(token, find_id=find_id), repo=repo,
            email_sender=None, email_source=None, now=NOW))
        assert answer["action"]["status"] == "unavailable"

    def test_a_find_from_another_tenant_is_not_emailed(self):
        repo, business_id, _, token = owner()
        other_business = repo.create_business(name="Rival", category="restaurant")
        other_find = a_find(repo, other_business)
        ses = FakeSes()
        answer = body_of(call(repo, token, find_id=other_find, ses=ses))
        assert answer["action"]["status"] == "not_found"
        assert ses.sent == []

    def test_a_send_failure_is_reported_not_swallowed(self):
        repo, business_id, _, token = owner()
        find_id = a_find(repo, business_id)

        def boom(**kwargs):
            raise RuntimeError("SES throttled")

        answer = body_of(email_draft_action(
            event(token, find_id=find_id, confirmed=True), repo=repo,
            email_sender=boom, email_source="a@b.example", now=NOW))
        assert answer["action"]["status"] == "failed"
        assert "SES throttled" in answer["answer"]

    def test_a_find_with_no_wording_asks_the_owner_to_accept_first(self):
        repo, business_id, _, token = owner()
        # A find whose move/rationale/summary are all blank has nothing to send.
        observation_id = repo.insert_observation(
            business_id, content="x", kind="review", embedding=VECTOR,
            observed_at=NOW - timedelta(days=1))
        find_id = repo.insert_find_with_evidence(
            business_id, title="Bare find", summary="", rationale="", move="",
            emoji="↗", predicted_daily_cents=100, confidence=.5,
            verify_after=date(2026, 8, 21),
            evidence=[EvidenceRef(observation_id, .9)])
        ses = FakeSes()
        answer = body_of(call(repo, token, find_id=find_id, ses=ses))
        assert answer["action"]["status"] == "no_draft"
        assert ses.sent == []
