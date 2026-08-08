"""Where the spend limits meet the card: the Quartermaster paying for orders.

The payment tool is optional — every existing caller passes none and behaves
exactly as before — but when one is present the money rules are absolute:

* nothing is charged unless the order was authorised to be placed;
* a failed charge means no order, retryable, with the reason on the plan;
* a failed order after a successful charge refunds the charge, and the plan
  says both things happened rather than pretending neither did.

Everything runs against the two fakes. No network, no Stripe account, no money.
"""

from brasstacks.agents.quartermaster import (
    OrderRequest,
    Status,
    place_approved_order,
    plan_order,
)
from brasstacks.ordering import FakeOrderingTool
from brasstacks.payments import FakePaymentTool
from brasstacks.purchase_authority import Level, PurchaseAuthority

CATALOGUE = {"tomatoes": 4_50, "olive oil": 12_00, "saffron": 42_00}


def tool(**kwargs) -> FakeOrderingTool:
    return FakeOrderingTool(catalogue=dict(CATALOGUE), **kwargs)


def standing(scope="tomatoes", *, level=Level.AUTO,
             per_order=100_00) -> PurchaseAuthority:
    return PurchaseAuthority(scope=scope, level=level,
                             per_order_cap_cents=per_order)


def ask_for(*items, trigger="owner_instruction") -> OrderRequest:
    return OrderRequest(items=tuple(items), trigger=trigger)


class TestPayingForAnAutoPlacedOrder:
    def test_an_authorised_order_charges_the_cart_total_exactly(self):
        payments = FakePaymentTool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=tool(),
                          authorities=[standing()], payment_tool=payments)
        assert plan.status is Status.PLACED
        assert [c.amount_cents for c in payments.charged] == [9_00]

    def test_the_plan_carries_the_charge(self):
        # The receipt proves the order exists; the charge proves the money
        # moved. The owner-facing ledger needs both.
        payments = FakePaymentTool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=tool(),
                          authorities=[standing()], payment_tool=payments)
        assert plan.charge is not None
        assert plan.charge.external_reference

    def test_without_a_payment_tool_nothing_changes(self):
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=tool(),
                          authorities=[standing()])
        assert plan.status is Status.PLACED
        assert plan.charge is None


class TestNothingIsChargedWithoutAuthority:
    def test_an_uncovered_order_awaits_approval_and_charges_nothing(self):
        payments = FakePaymentTool()
        plan = plan_order(request=ask_for(("saffron", 1)), tool=tool(),
                          authorities=[], payment_tool=payments)
        assert plan.status is Status.AWAITING_APPROVAL
        assert payments.charged == []

    def test_a_stock_triggered_order_charges_nothing(self):
        # The always-ask rule for inferred orders outranks the card exactly as
        # it outranks every standing permission.
        payments = FakePaymentTool()
        plan = plan_order(request=ask_for(("tomatoes", 12),
                                          trigger="stock_threshold"),
                          tool=tool(), authorities=[standing()],
                          payment_tool=payments)
        assert plan.status is Status.AWAITING_APPROVAL
        assert payments.charged == []

    def test_an_unpriceable_order_charges_nothing(self):
        payments = FakePaymentTool()
        plan = plan_order(request=ask_for(("caviar", 1)), tool=tool(),
                          authorities=[standing("caviar")],
                          payment_tool=payments)
        assert plan.status is Status.FAILED
        assert payments.charged == []


class TestAFailedCharge:
    def test_a_declined_card_places_no_order(self):
        payments = FakePaymentTool()
        payments.fail_next("Your card was declined.", declined=True)
        ordering = tool()
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=ordering,
                          authorities=[standing()], payment_tool=payments)
        assert plan.status is Status.FAILED
        assert ordering.placed == []

    def test_the_reason_names_the_decline(self):
        payments = FakePaymentTool()
        payments.fail_next("Your card was declined.", declined=True)
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=tool(),
                          authorities=[standing()], payment_tool=payments)
        assert "declined" in plan.reason

    def test_the_cart_survives_so_the_owner_sees_what_failed(self):
        payments = FakePaymentTool()
        payments.fail_next("network")
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=tool(),
                          authorities=[standing()], payment_tool=payments)
        assert plan.cart is not None
        assert plan.cart.total_cents == 9_00


class TestAFailedOrderAfterASuccessfulCharge:
    """The worst path: the money moved and then the shop said no."""

    def test_the_charge_is_refunded(self):
        payments = FakePaymentTool()
        ordering = tool()
        ordering.fail_next("store is closed")
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=ordering,
                          authorities=[standing()], payment_tool=payments)
        assert plan.status is Status.FAILED
        assert [r.amount_cents for r in payments.refunded] == [9_00]

    def test_the_reason_admits_both_the_charge_and_the_refund(self):
        # "Failed" alone would leave the owner staring at a card statement the
        # plan never mentioned.
        payments = FakePaymentTool()
        ordering = tool()
        ordering.fail_next("store is closed")
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=ordering,
                          authorities=[standing()], payment_tool=payments)
        assert "store is closed" in plan.reason
        assert "refunded" in plan.reason

    def test_a_failed_refund_is_admitted_not_hidden(self):
        # Charged, order failed, and then the refund failed too. The plan must
        # say the charge stands — anything softer buries a real debt.
        payments = FakePaymentTool()
        payments.fail_next_refund("stripe is down")
        ordering = tool()
        ordering.fail_next("store is closed")
        plan = plan_order(request=ask_for(("tomatoes", 2)), tool=ordering,
                          authorities=[standing()], payment_tool=payments)
        assert plan.status is Status.FAILED
        assert "could not be refunded" in plan.reason
        assert plan.charge is not None


class TestPayingForAnApprovedOrder:
    def approved(self, ordering):
        return ordering.draft(items=[("saffron", 1)]).fingerprint

    def test_an_approved_order_is_charged_and_placed(self):
        payments = FakePaymentTool()
        ordering = tool()
        plan = place_approved_order(
            request=ask_for(("saffron", 1)), tool=ordering,
            approved_fingerprint=self.approved(ordering),
            idempotency_key="approval-1", payment_tool=payments)
        assert plan.status is Status.PLACED
        assert [c.amount_cents for c in payments.charged] == [42_00]

    def test_a_moved_price_charges_nothing(self):
        # The approval died with the old price; so must the charge.
        payments = FakePaymentTool()
        ordering = tool()
        fingerprint = self.approved(ordering)
        ordering.set_price("saffron", 55_00)
        plan = place_approved_order(
            request=ask_for(("saffron", 1)), tool=ordering,
            approved_fingerprint=fingerprint,
            idempotency_key="approval-1", payment_tool=payments)
        assert plan.status is Status.AWAITING_APPROVAL
        assert payments.charged == []

    def test_a_replayed_approval_charges_once(self):
        # The double-clicked approve, end to end through the Quartermaster.
        payments = FakePaymentTool()
        ordering = tool()
        fingerprint = self.approved(ordering)
        for _ in range(2):
            place_approved_order(
                request=ask_for(("saffron", 1)), tool=ordering,
                approved_fingerprint=fingerprint,
                idempotency_key="approval-1", payment_tool=payments)
        assert len(payments.charged) == 1
        assert len(ordering.placed) == 1
