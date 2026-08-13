"""Sales reps, and the messages the agent sends them.

Before any ordering API exists, the one integration every small business
already has is a human: the produce rep who takes orders and questions by
email. Two new kinds of row make that real — supplier contacts, and messages
to them — and one doctrine carries over unchanged from ordering: the agent
composes, but nothing leaves the building until the owner approves. A draft
costs nothing; ``send`` is the dangerous verb, and there is exactly one path
to it.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from brasstacks.auth import token_fingerprint
from brasstacks.handlers.orders import run_orders
from brasstacks.ordering import FakeOrderingTool
from brasstacks.orders_store import InMemoryOrdersStore
from brasstacks.payments import FakePaymentTool
from brasstacks.rep_messages import compose_rep_message
from brasstacks.repository import InMemoryRepository

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

CATALOGUE = {"tomatoes": 4_50}


# ---------------------------------------------------------------- contacts


@pytest.fixture
def store() -> InMemoryOrdersStore:
    return InMemoryOrdersStore()


class TestContacts:
    def test_an_added_contact_comes_back(self, store):
        row = store.add_contact("biz-1", name="Dana Cruz",
                                email="dana@harborproduce.com",
                                category="produce")
        listed = store.list_contacts("biz-1")
        assert [entry["id"] for entry in listed] == [row["id"]]
        assert listed[0]["name"] == "Dana Cruz"
        assert listed[0]["email"] == "dana@harborproduce.com"
        assert listed[0]["category"] == "produce"

    def test_rows_are_tenant_scoped(self, store):
        store.add_contact("biz-1", name="Dana", email="dana@x.com",
                          category="produce")
        assert store.list_contacts("biz-2") == []

    def test_a_contact_can_be_removed(self, store):
        row = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                category="produce")
        assert store.remove_contact("biz-1", row["id"]) is True
        assert store.list_contacts("biz-1") == []

    def test_removing_another_tenants_contact_does_nothing(self, store):
        row = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                category="produce")
        assert store.remove_contact("biz-2", row["id"]) is False
        assert len(store.list_contacts("biz-1")) == 1

    def test_a_contact_needs_a_name(self, store):
        with pytest.raises(ValueError):
            store.add_contact("biz-1", name="  ", email="dana@x.com",
                              category=None)

    def test_a_contact_needs_a_real_address(self, store):
        with pytest.raises(ValueError):
            store.add_contact("biz-1", name="Dana", email="not-an-address",
                              category=None)

    def test_category_is_normalised_and_optional(self, store):
        row = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                category="  Produce ")
        assert row["category"] == "produce"
        bare = store.add_contact("biz-1", name="Sam", email="sam@x.com",
                                 category=None)
        assert bare["category"] is None


# ------------------------------------------------------------- composition


class TestCompose:
    """The message template is deterministic and honest.

    No model writes to a human on the owner's behalf here: the owner's words
    travel verbatim, and the signature says an agent carried them and that
    the owner approved. That disclosure line is the same promise the order
    email makes, and it is load-bearing — a rep who replies needs to know
    who they are talking to.
    """

    def test_the_owners_words_travel_verbatim(self):
        message = compose_rep_message(
            contact_name="Dana Cruz",
            gist="Can you add heirloom tomatoes to Thursday's delivery?",
            business_name="Harborview Japanese")
        assert "Can you add heirloom tomatoes to Thursday's delivery?" \
            in message["body"]
        assert message["body"].startswith("Hello Dana Cruz,")

    def test_the_subject_names_the_business(self):
        message = compose_rep_message(contact_name="Dana", gist="Hi",
                                      business_name="Harborview Japanese")
        assert message["subject"] == "Message from Harborview Japanese"

    def test_the_agent_discloses_itself_and_the_approval(self):
        message = compose_rep_message(contact_name="Dana", gist="Hi",
                                      business_name="Harborview Japanese")
        assert "Brass Tacks" in message["body"]
        assert "approved" in message["body"]

    def test_an_empty_gist_is_refused(self):
        with pytest.raises(ValueError):
            compose_rep_message(contact_name="Dana", gist="   ",
                                business_name="Harborview Japanese")


# ----------------------------------------------------------- message rows


class TestMessageRows:
    def test_a_draft_is_created_pending(self, store):
        contact = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                    category="produce")
        row = store.create_message(
            "biz-1", contact_id=contact["id"], subject="s", body="b")
        assert row["status"] == "draft"
        listed = store.list_messages("biz-1")
        assert [entry["id"] for entry in listed] == [row["id"]]

    def test_rows_are_tenant_scoped(self, store):
        contact = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                    category=None)
        store.create_message("biz-1", contact_id=contact["id"],
                             subject="s", body="b")
        assert store.list_messages("biz-2") == []

    def test_sending_marks_the_row_once(self, store):
        contact = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                    category=None)
        row = store.create_message("biz-1", contact_id=contact["id"],
                                   subject="s", body="b")
        assert store.mark_message_sent(
            "biz-1", row["id"], external_reference="ses-1") is True
        sent = store.list_messages("biz-1")[0]
        assert sent["status"] == "sent"
        assert sent["external_reference"] == "ses-1"
        # A second send of the same row must be refused, not repeated: the
        # rep getting the same email twice reads as a system nobody drives.
        assert store.mark_message_sent(
            "biz-1", row["id"], external_reference="ses-2") is False

    def test_a_draft_can_be_discarded(self, store):
        contact = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                    category=None)
        row = store.create_message("biz-1", contact_id=contact["id"],
                                   subject="s", body="b")
        assert store.discard_message("biz-1", row["id"]) is True
        assert store.list_messages("biz-1")[0]["status"] == "discarded"
        # Discarding what is already sent must fail: the mail has left.
        other = store.create_message("biz-1", contact_id=contact["id"],
                                     subject="s", body="b")
        store.mark_message_sent("biz-1", other["id"],
                                external_reference="ses-3")
        assert store.discard_message("biz-1", other["id"]) is False


# ---------------------------------------------------------------- handler


def owner():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Harborview Japanese",
                                       category="restaurant")
    account_id = repo.create_account(business_id, username="maya",
                                     password_hash="not-used")
    token = "maya-session-token"
    repo.create_session(token_fingerprint(token), business_id=business_id,
                        account_id=account_id,
                        expires_at=NOW + timedelta(days=1))
    return repo, business_id, token


def event(*, token=None, method="GET", action=None, body=None):
    payload = {
        "requestContext": {"http": {"method": method}},
        "headers": {},
        "pathParameters": {"proxy": action} if action else {},
    }
    if token:
        payload["headers"]["Authorization"] = f"Bearer {token}"
    if body is not None:
        payload["body"] = json.dumps(body)
    return payload


class RepBoard:
    """The orders board with a recording mail robot attached."""

    def __init__(self, *, email: bool = True):
        self.repo, self.business_id, self.token = owner()
        self.store = InMemoryOrdersStore()
        self.tool = FakeOrderingTool(catalogue=dict(CATALOGUE))
        self.payments = FakePaymentTool()
        self.sent: list[dict] = []

        def sender(*, source, recipient, subject, body):
            self.sent.append({"source": source, "recipient": recipient,
                              "subject": subject, "body": body})
            return f"msg-{len(self.sent)}"

        self.email_sender = sender if email else None
        self.email_source = "night@brasstacks.example" if email else None

    def call(self, *, method="GET", action=None, body=None, token="default"):
        return run_orders(
            event(token=self.token if token == "default" else token,
                  method=method, action=action, body=body),
            repo=self.repo, store=self.store, tool=self.tool,
            payment_tool=self.payments, payment_provider="simulated",
            email_sender=self.email_sender, email_source=self.email_source,
            now=NOW)

    def body(self, response):
        return json.loads(response["body"])

    def add_rep(self, **overrides):
        payload = {"name": "Dana Cruz", "email": "dana@harborproduce.com",
                   "category": "produce"}
        payload.update(overrides)
        response = self.call(method="POST", action="contacts", body=payload)
        assert response["statusCode"] == 200, response["body"]
        return self.body(response)["state"]


@pytest.fixture
def board() -> RepBoard:
    return RepBoard()


class TestContactsRoute:
    def test_a_rep_lands_in_the_state(self, board):
        board.add_rep()
        state = board.body(board.call())
        assert len(state["contacts"]) == 1
        assert state["contacts"][0]["name"] == "Dana Cruz"

    def test_garbage_is_refused(self, board):
        response = board.call(method="POST", action="contacts",
                              body={"name": "", "email": "x"})
        assert response["statusCode"] == 400

    def test_a_rep_can_be_removed(self, board):
        state = board.add_rep()
        contact_id = state["contacts"][0]["id"]
        response = board.call(method="POST", action="contacts/remove",
                              body={"id": contact_id})
        assert response["statusCode"] == 200
        assert board.body(board.call())["contacts"] == []


class TestMessageRoute:
    def draft(self, board, text="Can you add heirloom tomatoes on Thursday?"):
        contact_id = board.add_rep()["contacts"][0]["id"]
        response = board.call(method="POST", action="message",
                              body={"contact_id": contact_id, "text": text})
        assert response["statusCode"] == 200, response["body"]
        return board.body(response)["state"]

    def test_a_draft_is_composed_but_not_sent(self, board):
        state = self.draft(board)
        drafts = [m for m in state["messages"] if m["status"] == "draft"]
        assert len(drafts) == 1
        assert "heirloom tomatoes" in drafts[0]["body"]
        assert board.sent == []

    def test_a_missing_contact_is_a_404(self, board):
        response = board.call(method="POST", action="message",
                              body={"contact_id": "nope", "text": "hello"})
        assert response["statusCode"] == 404

    def test_empty_text_is_refused(self, board):
        contact_id = board.add_rep()["contacts"][0]["id"]
        response = board.call(method="POST", action="message",
                              body={"contact_id": contact_id, "text": "  "})
        assert response["statusCode"] == 400

    def test_approving_sends_to_the_rep_and_only_then(self, board):
        state = self.draft(board)
        message_id = state["messages"][0]["id"]
        response = board.call(method="POST", action="message/send",
                              body={"id": message_id})
        assert response["statusCode"] == 200, response["body"]
        assert len(board.sent) == 1
        assert board.sent[0]["recipient"] == "dana@harborproduce.com"
        assert "heirloom tomatoes" in board.sent[0]["body"]
        sent_rows = [m for m in board.body(board.call())["messages"]
                     if m["status"] == "sent"]
        assert len(sent_rows) == 1
        assert sent_rows[0]["external_reference"] == "email:msg-1"

    def test_a_message_cannot_be_sent_twice(self, board):
        state = self.draft(board)
        message_id = state["messages"][0]["id"]
        board.call(method="POST", action="message/send",
                   body={"id": message_id})
        response = board.call(method="POST", action="message/send",
                              body={"id": message_id})
        assert response["statusCode"] == 409
        assert len(board.sent) == 1

    def test_a_draft_can_be_discarded_instead(self, board):
        state = self.draft(board)
        message_id = state["messages"][0]["id"]
        response = board.call(method="POST", action="message/discard",
                              body={"id": message_id})
        assert response["statusCode"] == 200
        assert board.sent == []
        assert board.body(board.call())["messages"][0]["status"] == "discarded"

    def test_without_email_config_the_refusal_is_honest(self):
        muted = RepBoard(email=False)
        state = TestMessageRoute().draft(muted)
        response = muted.call(method="POST", action="message/send",
                              body={"id": state["messages"][0]["id"]})
        assert response["statusCode"] == 400
        assert "email" in json.loads(response["body"])["error"].lower()


class TestChatIntent:
    """"Ask my produce rep …" in the chat drafts a message, never sends one.

    Deterministic routing, before the order parser: naming a rep is
    unambiguous enough that no model needs to guess, and the wrong guess
    here — treating a message as an order, or vice versa — costs the owner
    an email a human actually reads.
    """

    def test_the_chat_drafts_a_message_to_the_named_category(self, board):
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "ask my produce rep: any heirloom tomatoes this "
                          "week?"})
        assert response["statusCode"] == 200, response["body"]
        answer = board.body(response)
        assert answer["kind"] == "message_drafted"
        assert "Dana Cruz" in answer["reason"]
        drafts = [m for m in answer["state"]["messages"]
                  if m["status"] == "draft"]
        assert len(drafts) == 1
        assert "any heirloom tomatoes this week?" in drafts[0]["body"]
        assert board.sent == []

    def test_the_rep_can_be_named_by_name(self, board):
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "Message Dana: the delivery gate code changed "
                          "to 4415"})
        answer = board.body(response)
        assert answer["kind"] == "message_drafted"

    def test_an_unknown_rep_gets_an_honest_answer(self, board):
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "tell my fish rep the order is late"})
        answer = board.body(response)
        assert answer["kind"] == "failed"
        assert "fish" in answer["reason"]

    def test_ordinary_orders_still_order(self, board):
        board.add_rep()
        response = board.call(method="POST", action="ask",
                              body={"text": "order 2 tomatoes"})
        answer = board.body(response)
        assert answer["kind"] in {"placed", "needs_approval"}
