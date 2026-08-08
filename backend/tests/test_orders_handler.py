"""The /orders API: the DoorDash screen stops being a preview.

Every route resolves its tenant from the caller's session, exactly like
/decision. The ordering tool is the simulated store (DoorDash access is
waitlist-gated); the payment tool is Stripe when keys exist and the fake
otherwise — and the payload says which, so the screen can label it honestly.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.auth import token_fingerprint
from brasstacks.handlers.orders import run_orders, simple_parse
from brasstacks.ordering import FakeOrderingTool
from brasstacks.orders_store import InMemoryOrdersStore
from brasstacks.payments import FakePaymentTool
from brasstacks.repository import InMemoryRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 8, 8)  # a Saturday

CATALOGUE = {"tomatoes": 4_50, "olive oil": 12_00, "flour": 3_25,
             "saffron": 42_00}


def owner():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    account_id = repo.create_account(business_id, username="rosa",
                                     password_hash="not-used")
    token = "rosa-session-token"
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


class Board:
    """One owner, one store, one simulated shop — shared test harness."""

    def __init__(self):
        self.repo, self.business_id, self.token = owner()
        self.store = InMemoryOrdersStore()
        self.tool = FakeOrderingTool(catalogue=dict(CATALOGUE),
                                     store_name="Simulated store",
                                     fees_cents=2_99)
        self.payments = FakePaymentTool()

    def call(self, *, method="GET", action=None, body=None, token="default",
             reasoner=None):
        return run_orders(
            event(token=self.token if token == "default" else token,
                  method=method, action=action, body=body),
            repo=self.repo, store=self.store, tool=self.tool,
            payment_tool=self.payments, payment_provider="simulated",
            reasoner=reasoner, now=NOW)

    def body(self, response):
        return json.loads(response["body"])


@pytest.fixture
def board() -> Board:
    return Board()


class TestAuth:
    def test_no_session_no_board(self, board):
        assert board.call(token=None)["statusCode"] == 401

    def test_a_bad_token_is_a_stranger(self, board):
        assert board.call(token="wrong")["statusCode"] == 401


class TestState:
    def test_an_empty_board_has_its_shape(self, board):
        response = board.call()
        assert response["statusCode"] == 200
        state = board.body(response)
        assert state["standing"] == []
        assert state["limits"] == []
        assert state["pending"] == []
        assert state["payment"]["provider"] == "simulated"

    def test_the_store_admits_it_is_simulated(self, board):
        state = board.body(board.call())
        assert state["store"]["simulated"] is True
        assert any(entry["name"] == "tomatoes"
                   for entry in state["store"]["catalogue"])

    def test_rows_come_back(self, board):
        board.store.add_standing(board.business_id, name="Produce",
                                 items=[["tomatoes", 8]], weekday=1)
        board.store.add_authority(board.business_id, scope="produce",
                                  level="auto", per_order_cap_cents=120_00)
        board.store.add_stock(board.business_id, name="tomatoes", reorder_at=7,
                              usage_per_week=7,
                              last_purchased_on=TODAY - timedelta(days=9),
                              last_purchased_quantity=14)
        state = board.body(board.call())
        assert state["standing"][0]["name"] == "Produce"
        assert state["limits"][0]["per_order_cap_cents"] == 120_00
        # The estimate carries its reasoning, per the stock module's contract.
        assert "Last bought 9 days ago" in state["stock"][0]["basis"]


class TestAsk:
    def test_a_covered_ask_places_pays_and_records(self, board):
        board.store.add_authority(board.business_id, scope="tomatoes",
                                  level="auto", per_order_cap_cents=100_00)
        response = board.call(method="POST", action="ask",
                              body={"text": "order 2 tomatoes"})
        answer = board.body(response)
        assert answer["kind"] == "placed"
        placed = board.store.list_orders(board.business_id, status="placed")
        assert len(placed) == 1
        assert placed[0]["payment_reference"]
        assert placed[0]["payment_provider"] == "simulated"
        assert [c.amount_cents for c in board.payments.charged] == [
            2 * 4_50 + 2_99]

    def test_an_uncovered_ask_waits_and_charges_nothing(self, board):
        response = board.call(method="POST", action="ask",
                              body={"text": "order 1 saffron"})
        answer = board.body(response)
        assert answer["kind"] == "needs_approval"
        pending = board.store.list_orders(board.business_id,
                                          status="awaiting_approval")
        assert len(pending) == 1
        assert pending[0]["fingerprint"]
        assert board.payments.charged == []

    def test_a_placed_order_feeds_the_stock_history(self, board):
        board.store.add_stock(board.business_id, name="tomatoes", reorder_at=7,
                              usage_per_week=7)
        board.store.add_authority(board.business_id, scope="tomatoes",
                                  level="auto", per_order_cap_cents=100_00)
        board.call(method="POST", action="ask",
                   body={"text": "order 2 tomatoes"})
        row = board.store.list_stock(board.business_id)[0]
        assert row["last_purchased_on"] == TODAY
        assert row["last_purchased_quantity"] == 2

    def test_words_that_name_nothing_are_not_an_order(self, board):
        response = board.call(method="POST", action="ask",
                              body={"text": "how did we do last month?"})
        assert board.body(response)["kind"] == "not_an_order"

    def test_a_reasoner_reads_the_message_when_present(self, board):
        class Reasoner:
            def complete_json(self, **kwargs):
                return {"is_order_request": True,
                        "items": [{"name": "flour", "quantity": 3}],
                        "category": None}
        response = board.call(method="POST", action="ask",
                              body={"text": "we need flour, three bags"},
                              reasoner=Reasoner())
        answer = board.body(response)
        # No flour authority exists, so the parsed cart stops for approval.
        assert answer["kind"] == "needs_approval"
        assert answer["order"]["cart"]["total_cents"] == 3 * 3_25 + 2_99

    def test_an_empty_ask_is_a_400(self, board):
        assert board.call(method="POST", action="ask",
                          body={"text": ""})["statusCode"] == 400


class TestApprove:
    def waiting(self, board):
        board.call(method="POST", action="ask", body={"text": "1 saffron"})
        return board.store.list_orders(board.business_id,
                                       status="awaiting_approval")[0]

    def test_an_approval_places_and_pays(self, board):
        row = self.waiting(board)
        response = board.call(method="POST", action="approve",
                              body={"order_id": row["id"]})
        answer = board.body(response)
        assert answer["kind"] == "placed"
        updated = board.store.get_order(board.business_id, row["id"])
        assert updated["status"] == "placed"
        assert updated["decided_at"] is not None
        assert [c.amount_cents for c in board.payments.charged] == [
            42_00 + 2_99]

    def test_a_moved_price_stops_and_charges_nothing(self, board):
        row = self.waiting(board)
        board.tool.set_price("saffron", 55_00)
        response = board.call(method="POST", action="approve",
                              body={"order_id": row["id"]})
        answer = board.body(response)
        assert answer["kind"] == "price_moved"
        updated = board.store.get_order(board.business_id, row["id"])
        assert updated["status"] == "awaiting_approval"
        # The owner sees the new cart, and approves that one or nothing.
        assert updated["cart"]["total_cents"] == 55_00 + 2_99
        assert board.payments.charged == []

    def test_an_unknown_order_is_a_404(self, board):
        response = board.call(method="POST", action="approve",
                              body={"order_id": "not-a-row"})
        assert response["statusCode"] == 404

    def test_an_already_placed_order_is_a_409(self, board):
        row = self.waiting(board)
        board.call(method="POST", action="approve",
                   body={"order_id": row["id"]})
        response = board.call(method="POST", action="approve",
                              body={"order_id": row["id"]})
        assert response["statusCode"] == 409

    def test_a_rejection_is_recorded_and_spends_nothing(self, board):
        row = self.waiting(board)
        response = board.call(method="POST", action="reject",
                              body={"order_id": row["id"]})
        assert response["statusCode"] == 200
        updated = board.store.get_order(board.business_id, row["id"])
        assert updated["status"] == "rejected"
        assert board.payments.charged == []


class TestStanding:
    def test_created_and_listed(self, board):
        response = board.call(method="POST", action="standing", body={
            "name": "The usual produce",
            "items": [["tomatoes", 8]], "weekday": 1, "category": "produce"})
        assert response["statusCode"] == 200
        assert board.body(board.call())["standing"][0]["name"] == \
            "The usual produce"

    def test_a_schedule_is_required(self, board):
        response = board.call(method="POST", action="standing", body={
            "name": "x", "items": [["tomatoes", 1]]})
        assert response["statusCode"] == 400

    def test_run_now_prices_against_the_limits(self, board):
        board.store.add_authority(board.business_id, scope="produce",
                                  level="auto", per_order_cap_cents=200_00)
        sid = board.store.add_standing(board.business_id, name="Produce",
                                       items=[["tomatoes", 8]], weekday=1,
                                       category="produce")
        response = board.call(method="POST", action="standing/run",
                              body={"standing_id": sid})
        assert board.body(response)["kind"] == "placed"
        assert board.store.list_standing(board.business_id)[0]["last_run_on"] \
            == TODAY

    def test_pause_toggles(self, board):
        sid = board.store.add_standing(board.business_id, name="x",
                                       items=[["flour", 1]], interval_days=14)
        board.call(method="POST", action="standing/pause",
                   body={"standing_id": sid})
        assert board.store.list_standing(board.business_id)[0]["enabled"] \
            is False


class TestLimits:
    def test_created_in_integer_cents(self, board):
        response = board.call(method="POST", action="limits", body={
            "scope": "produce", "level": "auto",
            "per_order_cap_cents": 120_00, "period_cap_cents": 200_00})
        assert response["statusCode"] == 200
        row = board.store.list_authorities(board.business_id)[0]
        assert row["per_order_cap_cents"] == 120_00

    def test_a_bad_level_is_a_400(self, board):
        response = board.call(method="POST", action="limits", body={
            "scope": "produce", "level": "sure", "per_order_cap_cents": 100})
        assert response["statusCode"] == 400

    def test_ask_if_over_without_threshold_is_a_400(self, board):
        response = board.call(method="POST", action="limits", body={
            "scope": "flour", "level": "ask_if_over",
            "per_order_cap_cents": 80_00})
        assert response["statusCode"] == 400


class TestStock:
    def test_created_and_listed(self, board):
        response = board.call(method="POST", action="stock", body={
            "name": "tomatoes", "reorder_at": 7, "usage_per_week": 7,
            "reorder_quantity": 12})
        assert response["statusCode"] == 200
        assert board.body(board.call())["stock"][0]["name"] == "tomatoes"

    def test_a_draft_always_stops_for_approval(self, board):
        # However generous the limits: a depletion figure is an estimate.
        board.store.add_authority(board.business_id, scope="tomatoes",
                                  level="auto", per_order_cap_cents=999_00)
        sid = board.store.add_stock(board.business_id, name="tomatoes",
                                    reorder_at=7, usage_per_week=7,
                                    reorder_quantity=12)
        response = board.call(method="POST", action="stock/draft",
                              body={"stock_id": sid})
        answer = board.body(response)
        assert answer["kind"] == "needs_approval"
        assert board.payments.charged == []
        pending = board.store.list_orders(board.business_id,
                                          status="awaiting_approval")
        assert pending[0]["trigger"] == "stock_threshold"


class TestSimpleParse:
    """The no-model fallback: keyword-matched against the catalogue, refusing
    what it does not recognise — the same contract as order_intent."""

    def test_reads_names_and_quantities(self):
        items = simple_parse("order 2 tomatoes and a case of olive oil",
                             CATALOGUE)
        assert ("tomatoes", 2) in items
        assert ("olive oil", 1) in items

    def test_unknown_words_read_as_nothing(self):
        assert simple_parse("how did we do last month?", CATALOGUE) == []
