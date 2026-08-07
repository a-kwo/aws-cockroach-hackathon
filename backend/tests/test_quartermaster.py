"""The ordering sequence: request in, receipt or an approval request out.

This is where the spend limits and the provider boundary meet. It runs entirely
against ``FakeOrderingTool``, so no test here can spend money or needs DoorDash
access.

The rules it enforces come from docs/ORDERS_AGENT.md section 10. The two that
matter most:

* the cart the owner approved is the cart that gets bought, and
* an order the agent inferred rather than being asked for always goes through a
  human, whatever standing permission exists.
"""

import pytest

from brasstacks.agents.quartermaster import (
    OrderRequest,
    Status,
    place_approved_order,
    plan_order,
)
from brasstacks.ordering import FakeOrderingTool, ItemUnavailable
from brasstacks.purchase_authority import Level, PurchaseAuthority

CATALOGUE = {"tomatoes": 4_50, "olive oil": 12_00, "flour": 3_25}


def tool(**kwargs) -> FakeOrderingTool:
    return FakeOrderingTool(catalogue=dict(CATALOGUE), **kwargs)


def standing(scope="tomatoes", *, level=Level.AUTO, per_order=100_00,
             **kwargs) -> PurchaseAuthority:
    return PurchaseAuthority(scope=scope, level=level,
                             per_order_cap_cents=per_order, **kwargs)


def ask_for(*items, trigger="owner_instruction", category=None) -> OrderRequest:
    return OrderRequest(items=tuple(items), trigger=trigger, category=category)


class TestTheHappyPath:
    def test_a_covered_order_is_placed(self):
        t = tool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                          authorities=[standing()])
        assert plan.status is Status.PLACED

    def test_placing_produces_a_receipt(self):
        t = tool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                          authorities=[standing()])
        assert plan.receipt is not None
        assert plan.receipt.total_cents == 9_00

    def test_the_order_actually_reached_the_provider(self):
        t = tool()
        plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                   authorities=[standing()])
        assert len(t.placed) == 1


class TestApprovalRequired:
    def test_an_uncovered_order_waits_for_the_owner(self):
        t = tool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                          authorities=[])
        assert plan.status is Status.AWAITING_APPROVAL

    def test_nothing_is_bought_while_waiting(self):
        t = tool()
        plan_order(request=ask_for(("tomatoes", 2)), tool=t, authorities=[])
        assert t.placed == []

    def test_the_owner_still_gets_a_priced_cart_to_look_at(self):
        # Asking "may I spend $9.00 on this?" is a different question from
        # "may I buy some tomatoes?", and only the first one is answerable.
        t = tool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                          authorities=[])
        assert plan.cart is not None
        assert plan.cart.total_cents == 9_00

    def test_the_reason_explains_why(self):
        t = tool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                          authorities=[])
        assert "authority" in plan.reason.lower()

    def test_over_the_cap_waits_rather_than_placing(self):
        t = tool()
        plan = plan_order(request=ask_for(("tomatoes", 10)), tool=t,
                          authorities=[standing(per_order=20_00)])
        assert plan.status is Status.AWAITING_APPROVAL
        assert t.placed == []


class TestEveryItemMustBeCovered:
    """A cart is placed automatically only when every line in it is covered and
    the total is inside every limit that applies. One unauthorised item stops
    the whole order — it must not be possible to smuggle something through by
    bundling it with things that are covered."""

    def test_one_uncovered_item_stops_the_whole_order(self):
        t = tool()
        plan = plan_order(request=ask_for(("tomatoes", 1), ("olive oil", 1)),
                          tool=t, authorities=[standing("tomatoes")])
        assert plan.status is Status.AWAITING_APPROVAL
        assert t.placed == []

    def test_all_covered_items_are_placed(self):
        t = tool()
        plan = plan_order(
            request=ask_for(("tomatoes", 1), ("olive oil", 1)), tool=t,
            authorities=[standing("tomatoes"), standing("olive oil")])
        assert plan.status is Status.PLACED

    def test_every_applicable_limit_binds_the_whole_total(self):
        # The tomato rule permits $100 and the oil rule $10. A $16.50 cart is
        # inside one of them and outside the other, so it waits.
        t = tool()
        plan = plan_order(
            request=ask_for(("tomatoes", 1), ("olive oil", 1)), tool=t,
            authorities=[standing("tomatoes", per_order=100_00),
                         standing("olive oil", per_order=10_00)])
        assert plan.status is Status.AWAITING_APPROVAL


class TestInferredStockAlwaysAsks:
    """Section 7: an inferred stock level is a guess, and a guess must not be
    able to spend money on its own however much standing permission exists."""

    def test_a_stock_triggered_order_waits_even_with_auto_authority(self):
        t = tool()
        plan = plan_order(
            request=ask_for(("tomatoes", 2), trigger="stock_threshold"),
            tool=t, authorities=[standing(level=Level.AUTO)])
        assert plan.status is Status.AWAITING_APPROVAL
        assert t.placed == []

    def test_the_reason_says_it_was_inferred(self):
        t = tool()
        plan = plan_order(
            request=ask_for(("tomatoes", 2), trigger="stock_threshold"),
            tool=t, authorities=[standing(level=Level.AUTO)])
        assert "stock" in plan.reason.lower() or "inferred" in plan.reason.lower()

    def test_the_same_cart_from_a_standing_order_is_placed(self):
        # Proving the difference is the trigger, not the cart.
        t = tool()
        plan = plan_order(
            request=ask_for(("tomatoes", 2), trigger="standing_order"),
            tool=t, authorities=[standing(level=Level.AUTO)])
        assert plan.status is Status.PLACED


class TestTriggerValidation:
    def test_an_unknown_trigger_is_refused(self):
        with pytest.raises(ValueError):
            plan_order(request=ask_for(("tomatoes", 1), trigger="whatever"),
                       tool=tool(), authorities=[standing()])


class TestProviderFailures:
    def test_an_unavailable_item_fails_without_placing(self):
        t = tool()
        plan = plan_order(request=ask_for(("saffron", 1)), tool=t,
                          authorities=[standing("saffron")])
        assert plan.status is Status.FAILED
        assert t.placed == []

    def test_the_failure_names_the_item(self):
        plan = plan_order(request=ask_for(("saffron", 1)), tool=tool(),
                          authorities=[standing("saffron")])
        assert "saffron" in plan.reason

    def test_a_declined_payment_fails_the_order(self):
        t = tool()
        t.fail_next("card declined")
        plan = plan_order(request=ask_for(("tomatoes", 1)), tool=t,
                          authorities=[standing()])
        assert plan.status is Status.FAILED
        assert "declined" in plan.reason

    def test_a_failed_order_reports_no_receipt(self):
        t = tool()
        t.fail_next("card declined")
        plan = plan_order(request=ask_for(("tomatoes", 1)), tool=t,
                          authorities=[standing()])
        assert plan.receipt is None


class TestApprovedCartIsTheCartBought:
    """Money-safety rule 3. Between the owner seeing a cart and approving it,
    a price can move. Buying the new one silently would mean the number they
    agreed to was not the number they paid."""

    def test_an_unchanged_cart_is_placed_on_approval(self):
        t = tool()
        pending = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                             authorities=[])
        done = place_approved_order(
            request=ask_for(("tomatoes", 2)), tool=t,
            approved_fingerprint=pending.cart.fingerprint,
            idempotency_key="task-1")
        assert done.status is Status.PLACED

    def test_a_price_move_since_approval_stops_the_order(self):
        t = tool()
        pending = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                             authorities=[])
        t.set_price("tomatoes", 9_00)
        done = place_approved_order(
            request=ask_for(("tomatoes", 2)), tool=t,
            approved_fingerprint=pending.cart.fingerprint,
            idempotency_key="task-1")
        assert done.status is Status.AWAITING_APPROVAL
        assert t.placed == []

    def test_the_owner_is_told_the_price_moved(self):
        t = tool()
        pending = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                             authorities=[])
        t.set_price("tomatoes", 9_00)
        done = place_approved_order(
            request=ask_for(("tomatoes", 2)), tool=t,
            approved_fingerprint=pending.cart.fingerprint,
            idempotency_key="task-1")
        assert "changed" in done.reason.lower() or "moved" in done.reason.lower()

    def test_the_re_priced_cart_is_offered_for_a_fresh_approval(self):
        t = tool()
        pending = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                             authorities=[])
        t.set_price("tomatoes", 9_00)
        done = place_approved_order(
            request=ask_for(("tomatoes", 2)), tool=t,
            approved_fingerprint=pending.cart.fingerprint,
            idempotency_key="task-1")
        assert done.cart.total_cents == 18_00

    def test_approval_without_a_fingerprint_is_refused(self):
        # An approval that is not bound to a specific cart is not an approval.
        t = tool()
        with pytest.raises(ValueError):
            place_approved_order(request=ask_for(("tomatoes", 2)), tool=t,
                                 approved_fingerprint="", idempotency_key="t1")


class TestIdempotency:
    def test_placing_the_same_approved_order_twice_charges_once(self):
        t = tool()
        pending = plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                             authorities=[])
        for _ in range(2):
            place_approved_order(
                request=ask_for(("tomatoes", 2)), tool=t,
                approved_fingerprint=pending.cart.fingerprint,
                idempotency_key="task-1")
        assert len(t.placed) == 1

    def test_an_automatic_order_derives_a_key_from_the_cart(self):
        # Two identical automatic orders in one run are almost certainly one
        # order dispatched twice.
        t = tool()
        for _ in range(2):
            plan_order(request=ask_for(("tomatoes", 2)), tool=t,
                       authorities=[standing()], idempotency_key="night-1")
        assert len(t.placed) == 1
