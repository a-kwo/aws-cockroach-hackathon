"""The Quartermaster's memory: per-tenant rows behind the DoorDash screen.

The store keeps four kinds of rows — standing orders, purchase authorities,
stock items, and order records — and every read is scoped by business_id,
because the browser never gets to choose a tenant. These tests run against the
in-memory implementation; ``PostgresOrdersStore`` mirrors it statement for
statement and is exercised by the integration suite against a real cluster.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.orders_store import InMemoryOrdersStore

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
BIZ = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def store() -> InMemoryOrdersStore:
    return InMemoryOrdersStore()


class TestStandingOrders:
    def test_an_added_order_comes_back(self, store):
        store.add_standing(BIZ, name="The usual produce",
                           items=[["tomatoes", 8]], weekday=1)
        rows = store.list_standing(BIZ)
        assert [r["name"] for r in rows] == ["The usual produce"]
        assert rows[0]["items"] == [["tomatoes", 8]]

    def test_rows_are_tenant_scoped(self, store):
        store.add_standing(BIZ, name="Mine", items=[["flour", 1]],
                           interval_days=14)
        assert store.list_standing(OTHER) == []

    def test_enabled_can_be_toggled(self, store):
        sid = store.add_standing(BIZ, name="x", items=[["flour", 1]],
                                 interval_days=14)
        assert store.set_standing_enabled(BIZ, sid, False) is True
        assert store.list_standing(BIZ)[0]["enabled"] is False

    def test_toggling_another_tenants_row_does_nothing(self, store):
        sid = store.add_standing(BIZ, name="x", items=[["flour", 1]],
                                 interval_days=14)
        assert store.set_standing_enabled(OTHER, sid, False) is False
        assert store.list_standing(BIZ)[0]["enabled"] is True

    def test_a_run_is_remembered(self, store):
        sid = store.add_standing(BIZ, name="x", items=[["flour", 1]],
                                 interval_days=14)
        store.mark_standing_ran(BIZ, sid, on=date(2026, 8, 8))
        assert store.list_standing(BIZ)[0]["last_run_on"] == date(2026, 8, 8)

    def test_an_order_needs_a_schedule(self, store):
        with pytest.raises(ValueError):
            store.add_standing(BIZ, name="x", items=[["flour", 1]])

    def test_an_order_needs_items(self, store):
        with pytest.raises(ValueError):
            store.add_standing(BIZ, name="x", items=[], weekday=1)


class TestAuthorities:
    def test_an_added_authority_comes_back(self, store):
        store.add_authority(BIZ, scope="produce", level="auto",
                            per_order_cap_cents=120_00)
        rows = store.list_authorities(BIZ)
        assert rows[0]["scope"] == "produce"
        assert rows[0]["per_order_cap_cents"] == 120_00

    def test_caps_must_be_integer_cents(self, store):
        with pytest.raises(TypeError):
            store.add_authority(BIZ, scope="produce", level="auto",
                                per_order_cap_cents=120.50)

    def test_the_level_must_be_a_known_one(self, store):
        with pytest.raises(ValueError):
            store.add_authority(BIZ, scope="produce", level="yolo",
                                per_order_cap_cents=100)

    def test_ask_if_over_requires_its_threshold(self, store):
        with pytest.raises(ValueError):
            store.add_authority(BIZ, scope="flour", level="ask_if_over",
                                per_order_cap_cents=80_00)

    def test_tenant_scoped(self, store):
        store.add_authority(BIZ, scope="produce", level="auto",
                            per_order_cap_cents=100)
        assert store.list_authorities(OTHER) == []


class TestStock:
    def test_an_added_item_comes_back(self, store):
        store.add_stock(BIZ, name="tomatoes", reorder_at=7, usage_per_week=7,
                        reorder_quantity=12)
        assert store.list_stock(BIZ)[0]["name"] == "tomatoes"

    def test_a_placed_purchase_updates_the_history(self, store):
        # This is the memory story: what was bought through Brass Tacks
        # sharpens the next depletion estimate.
        store.add_stock(BIZ, name="tomatoes", reorder_at=7, usage_per_week=7)
        store.record_purchase(BIZ, [("tomatoes", 12)], on=date(2026, 8, 8))
        row = store.list_stock(BIZ)[0]
        assert row["last_purchased_on"] == date(2026, 8, 8)
        assert row["last_purchased_quantity"] == 12

    def test_a_purchase_of_an_untracked_item_is_ignored(self, store):
        store.record_purchase(BIZ, [("saffron", 1)], on=date(2026, 8, 8))
        assert store.list_stock(BIZ) == []


class TestOrders:
    def order(self, store, *, status="awaiting_approval", total=9_00,
              business=BIZ, now=NOW):
        return store.create_order(
            business, title="tomatoes x2", trigger="owner_instruction",
            status=status, items=[["tomatoes", 2]], category=None,
            cart={"lines": [], "fees_cents": 0, "total_cents": total},
            total_cents=total, reason="because", fingerprint="fp-1", now=now)

    def test_a_created_order_can_be_fetched(self, store):
        oid = self.order(store)
        row = store.get_order(BIZ, oid)
        assert row["status"] == "awaiting_approval"
        assert row["total_cents"] == 9_00

    def test_fetching_across_tenants_returns_nothing(self, store):
        oid = self.order(store)
        assert store.get_order(OTHER, oid) is None

    def test_orders_list_newest_first(self, store):
        self.order(store, now=NOW)
        self.order(store, total=1_00, now=NOW + timedelta(minutes=1))
        rows = store.list_orders(BIZ)
        assert rows[0]["total_cents"] == 1_00

    def test_listing_can_filter_by_status(self, store):
        self.order(store, status="placed")
        self.order(store, status="awaiting_approval")
        assert all(r["status"] == "placed"
                   for r in store.list_orders(BIZ, status="placed"))

    def test_an_update_lands(self, store):
        oid = self.order(store)
        assert store.update_order(BIZ, oid, status="placed",
                                  external_reference="sim-1") is True
        assert store.get_order(BIZ, oid)["external_reference"] == "sim-1"

    def test_updates_are_tenant_scoped(self, store):
        oid = self.order(store)
        assert store.update_order(OTHER, oid, status="placed") is False
        assert store.get_order(BIZ, oid)["status"] == "awaiting_approval"

    def test_unknown_update_fields_are_refused(self, store):
        # A typo'd field silently dropped would report an update that never
        # happened.
        oid = self.order(store)
        with pytest.raises(ValueError):
            store.update_order(BIZ, oid, verdict="placed")


class TestSpentInPeriod:
    def test_placed_orders_in_the_window_are_summed(self, store):
        store.create_order(BIZ, title="a", trigger="owner_instruction",
                           status="placed", items=[], category=None, cart=None,
                           total_cents=10_00, reason="", fingerprint=None,
                           now=NOW - timedelta(days=2))
        store.create_order(BIZ, title="b", trigger="owner_instruction",
                           status="placed", items=[], category=None, cart=None,
                           total_cents=5_00, reason="", fingerprint=None,
                           now=NOW - timedelta(days=1))
        assert store.spent_since(BIZ, since=NOW - timedelta(days=7)) == 15_00

    def test_pending_and_rejected_money_does_not_count_as_spent(self, store):
        store.create_order(BIZ, title="a", trigger="owner_instruction",
                           status="awaiting_approval", items=[], category=None,
                           cart=None, total_cents=99_00, reason="",
                           fingerprint=None, now=NOW)
        assert store.spent_since(BIZ, since=NOW - timedelta(days=7)) == 0

    def test_old_spending_falls_out_of_the_window(self, store):
        store.create_order(BIZ, title="a", trigger="owner_instruction",
                           status="placed", items=[], category=None, cart=None,
                           total_cents=10_00, reason="", fingerprint=None,
                           now=NOW - timedelta(days=30))
        assert store.spent_since(BIZ, since=NOW - timedelta(days=7)) == 0
