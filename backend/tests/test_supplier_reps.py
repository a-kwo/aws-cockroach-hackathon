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
from brasstacks.rep_messages import compose_rep_message, compose_rep_text
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

    def test_a_phone_number_is_kept_and_normalised(self, store):
        row = store.add_contact("biz-1", name="Dana", email="dana@x.com",
                                phone=" +1 (415) 555-0134 ", category=None)
        assert row["phone"] == "+14155550134"

    def test_phone_alone_is_a_valid_channel(self, store):
        row = store.add_contact("biz-1", name="Dana", email=None,
                                phone="+14155550134", category="produce")
        assert row["email"] is None
        assert row["phone"] == "+14155550134"

    def test_a_contact_needs_at_least_one_channel(self, store):
        with pytest.raises(ValueError):
            store.add_contact("biz-1", name="Dana", email=None, phone=None,
                              category=None)

    def test_a_garbage_phone_is_refused(self, store):
        with pytest.raises(ValueError):
            store.add_contact("biz-1", name="Dana", email=None,
                              phone="call the shop", category=None)


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

    def test_the_whatsapp_text_is_short_but_keeps_both_promises(self):
        text = compose_rep_text(
            gist="Any heirloom tomatoes this week?",
            business_name="Harborview Japanese")
        assert "Any heirloom tomatoes this week?" in text
        assert "Harborview Japanese" in text
        assert "Brass Tacks" in text
        assert "approved" in text

    def test_the_whatsapp_text_refuses_emptiness_too(self):
        with pytest.raises(ValueError):
            compose_rep_text(gist=" ", business_name="X")


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
                                     password_hash="not-used",
                                     email="maya@harborviewjapanese.com")
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
    """The orders board with recording mail and WhatsApp robots attached."""

    def __init__(self, *, email: bool = True, whatsapp: bool = True):
        self.repo, self.business_id, self.token = owner()
        self.store = InMemoryOrdersStore()
        self.tool = FakeOrderingTool(catalogue=dict(CATALOGUE))
        self.payments = FakePaymentTool()
        self.sent: list[dict] = []
        self.texted: list[dict] = []

        def sender(*, source, recipient, subject, body, reply_to=None):
            self.sent.append({"source": source, "recipient": recipient,
                              "subject": subject, "body": body,
                              "reply_to": reply_to})
            return f"msg-{len(self.sent)}"

        def texter(*, recipient, body):
            self.texted.append({"recipient": recipient, "body": body})
            return f"wamid-{len(self.texted)}"

        self.email_sender = sender if email else None
        self.email_source = "night@brasstacks.example" if email else None
        self.whatsapp_sender = texter if whatsapp else None

    def call(self, *, method="GET", action=None, body=None, token="default"):
        return run_orders(
            event(token=self.token if token == "default" else token,
                  method=method, action=action, body=body),
            repo=self.repo, store=self.store, tool=self.tool,
            payment_tool=self.payments, payment_provider="simulated",
            email_sender=self.email_sender, email_source=self.email_source,
            whatsapp_sender=self.whatsapp_sender,
            now=NOW)

    def body(self, response):
        return json.loads(response["body"])

    def add_rep(self, **overrides):
        payload = {"name": "Dana Cruz", "email": "dana@harborproduce.com",
                   "phone": "+14155550134", "category": "produce"}
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


class TestWhatsApp:
    """The second channel: same doctrine, different pipe.

    "WhatsApp my produce rep …" drafts a WhatsApp message; Send routes it
    through the WhatsApp seam instead of SES. iMessage gets an honest no —
    Apple offers no API a server could call, and pretending otherwise is
    exactly the kind of simulation wearing the wrong name this codebase
    refuses.
    """

    def draft_whatsapp(self, board,
                       text="whatsapp my produce rep: any uni today?"):
        board.add_rep()
        response = board.call(method="POST", action="ask",
                              body={"text": text})
        assert response["statusCode"] == 200, response["body"]
        return board.body(response)

    def test_the_verb_picks_the_channel(self, board):
        answer = self.draft_whatsapp(board)
        assert answer["kind"] == "message_drafted"
        draft = answer["state"]["messages"][0]
        assert draft["channel"] == "whatsapp"
        assert "any uni today?" in draft["body"]

    def test_texting_works_as_a_verb_too(self, board):
        answer = self.draft_whatsapp(
            board, text="text my produce rep: any uni today?")
        assert answer["state"]["messages"][0]["channel"] == "whatsapp"

    def test_email_verb_still_means_email(self, board):
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "email my produce rep: any uni today?"})
        assert board.body(response)["state"]["messages"][0]["channel"] \
            == "email"

    def test_sending_routes_through_the_whatsapp_seam(self, board):
        answer = self.draft_whatsapp(board)
        message_id = answer["state"]["messages"][0]["id"]
        response = board.call(method="POST", action="message/send",
                              body={"id": message_id})
        assert response["statusCode"] == 200, response["body"]
        assert len(board.texted) == 1
        assert board.texted[0]["recipient"] == "+14155550134"
        assert "any uni today?" in board.texted[0]["body"]
        assert board.sent == []
        sent = [m for m in board.body(board.call())["messages"]
                if m["status"] == "sent"][0]
        assert sent["external_reference"] == "whatsapp:wamid-1"

    def test_a_rep_without_a_phone_gets_an_honest_answer(self, board):
        board.add_rep(phone=None)
        response = board.call(
            method="POST", action="ask",
            body={"text": "whatsapp my produce rep: any uni today?"})
        answer = board.body(response)
        assert answer["kind"] == "failed"
        assert "phone" in answer["reason"].lower()

    def test_without_whatsapp_config_the_refusal_is_honest(self):
        muted = RepBoard(whatsapp=False)
        answer = TestWhatsApp().draft_whatsapp(muted)
        response = muted.call(method="POST", action="message/send",
                              body={"id": answer["state"]["messages"][0]["id"]})
        assert response["statusCode"] == 400
        assert "whatsapp" in json.loads(response["body"])["error"].lower()

    def test_imessage_is_refused_with_the_reason(self, board):
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "imessage my produce rep: any uni today?"})
        answer = board.body(response)
        assert answer["kind"] == "failed"
        assert "imessage" in answer["reason"].lower()
        assert answer["state"]["messages"] == []

    def test_a_phone_only_rep_defaults_to_whatsapp(self, board):
        board.add_rep(email=None)
        response = board.call(
            method="POST", action="ask",
            body={"text": "ask my produce rep: any uni today?"})
        draft = board.body(response)["state"]["messages"][0]
        assert draft["channel"] == "whatsapp"


class TestSenderIdentity:
    """The rep sees the business; the reply reaches the owner.

    The From can never truthfully be the owner's Gmail — SES sends from the
    platform's verified identity, and gmail.com's own DNS would disown the
    message anywhere else. What CAN be true: the display name carries the
    business, and Reply-To carries the signup email, which makes the
    template's "Replies go to the owner" a statement of fact.
    """

    def test_the_send_carries_display_name_and_reply_to(self, board):
        state = TestMessageRoute().draft(board)
        board.call(method="POST", action="message/send",
                   body={"id": state["messages"][0]["id"]})
        sent = board.sent[0]
        assert sent["source"] == ('"Harborview Japanese (via Brass Tacks)" '
                                  "<night@brasstacks.example>")
        assert sent["reply_to"] == "maya@harborviewjapanese.com"


class TestCartsThroughReps:
    """Order-by-email folds into rep messaging.

    A structured cart to a human supplier is just a message that happens to
    carry items — so it lives in the same drafts, behind the same single
    Send. The consequence is deliberate: a human supplier is never emailed
    automatically, whatever standing authority exists. Machines can be
    ordered from on autopilot; people get asked.
    """

    def test_an_order_verb_in_the_gist_drafts_a_cart(self, board):
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "email my produce rep: order 8 tomatoes"})
        answer = board.body(response)
        assert answer["kind"] == "message_drafted", answer
        draft = answer["state"]["messages"][0]
        assert draft["kind"] == "order"
        assert draft["total_cents"] == 8 * 4_50
        assert draft["cart"]["lines"][0]["name"] == "tomatoes"
        assert "8" in draft["body"] and "$36.00" in draft["body"]
        assert "invoice" in draft["body"].lower()
        assert board.sent == []

    def test_a_question_naming_an_item_stays_a_note(self, board):
        """"any tomatoes this week?" contains a catalogue word, and turning
        that into a cart would be the misfiling this router exists to
        prevent. Only a leading order verb makes a cart."""
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "ask my produce rep: any tomatoes this week?"})
        draft = board.body(response)["state"]["messages"][0]
        assert draft["kind"] == "note"
        assert draft.get("cart") is None

    def test_an_order_nobody_can_price_is_an_honest_miss(self, board):
        board.add_rep()
        response = board.call(
            method="POST", action="ask",
            body={"text": "email my produce rep: order 3 unicorn steaks"})
        answer = board.body(response)
        assert answer["kind"] == "failed"
        assert "catalogue" in answer["reason"].lower()

    def test_sending_a_cart_message_writes_the_books(self, board):
        board.add_rep()
        drafted = board.body(board.call(
            method="POST", action="ask",
            body={"text": "email my produce rep: order 8 tomatoes"}))
        message_id = drafted["state"]["messages"][0]["id"]
        response = board.call(method="POST", action="message/send",
                              body={"id": message_id})
        assert response["statusCode"] == 200, response["body"]
        assert len(board.sent) == 1
        assert "8" in board.sent[0]["body"]
        state = board.body(board.call())
        placed = [row for row in state["history"]
                  if row["status"] == "placed"]
        assert len(placed) == 1
        assert placed[0]["total_cents"] == 8 * 4_50
        assert placed[0]["external_reference"].startswith("email:")
        assert "invoice" in placed[0]["reason"].lower()

    def test_the_email_supplier_mode_is_retired(self, board):
        state = board.body(board.call())
        assert [option["id"] for option in state["supplier"]["options"]]             == ["simulated", "doordash"]
        response = board.call(method="POST", action="supplier",
                              body={"supplier": "email",
                                    "email": "old@supplier.com"})
        assert response["statusCode"] == 400
        assert "rep" in json.loads(response["body"])["error"].lower()
