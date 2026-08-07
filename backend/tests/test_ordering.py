"""The boundary between Brass Tacks and whoever actually sells the goods.

Everything above this line — the task flow, the spend limits, the triggers — is
built and tested against ``FakeOrderingTool``. That is not a convenience. The
real DoorDash CLI is macOS-only, waitlist-gated, and spends real money from a
real payment method, so a suite that needed it would be a suite nobody could
run. Faking at the boundary rather than inside the agent is what lets the whole
system be exercised offline and then flipped to the real tool without rewriting
anything above.

Money is integer cents throughout. No floats.
"""

import pytest

from brasstacks.ordering import (
    Cart,
    FakeOrderingTool,
    ItemUnavailable,
    LineItem,
    OrderingError,
    OrderingTool,
)


CATALOGUE = {
    "tomatoes": 4_50,
    "olive oil": 12_00,
    "flour": 3_25,
}


def tool(**kwargs) -> FakeOrderingTool:
    return FakeOrderingTool(catalogue=dict(CATALOGUE), store_name="Test Grocer",
                            **kwargs)


class TestItIsATool:
    def test_the_fake_satisfies_the_protocol(self):
        # runtime_checkable, so a swap to the real CLI tool is caught by the
        # type system rather than at 6am in a Lambda.
        assert isinstance(tool(), OrderingTool)

    def test_it_names_itself(self):
        assert tool().name


class TestDrafting:
    """Turning "20lb of tomatoes" into a cart with a price on it."""

    def test_it_prices_a_single_item(self):
        cart = tool().draft(items=[("tomatoes", 1)])
        assert cart.total_cents == 4_50

    def test_quantity_multiplies(self):
        cart = tool().draft(items=[("tomatoes", 3)])
        assert cart.total_cents == 13_50

    def test_several_items_sum(self):
        cart = tool().draft(items=[("tomatoes", 2), ("flour", 4)])
        assert cart.subtotal_cents == 9_00 + 13_00

    def test_fees_are_added_on_top_of_the_subtotal(self):
        cart = tool(fees_cents=2_99).draft(items=[("tomatoes", 1)])
        assert cart.subtotal_cents == 4_50
        assert cart.total_cents == 4_50 + 2_99

    def test_an_unknown_item_is_refused_not_guessed(self):
        # Substituting something plausible would mean the owner approves one
        # thing and receives another.
        with pytest.raises(ItemUnavailable):
            tool().draft(items=[("saffron", 1)])

    def test_the_unavailable_item_is_named(self):
        with pytest.raises(ItemUnavailable, match="saffron"):
            tool().draft(items=[("saffron", 1)])

    def test_an_empty_cart_is_refused(self):
        with pytest.raises(ValueError):
            tool().draft(items=[])

    def test_a_zero_quantity_is_refused(self):
        with pytest.raises(ValueError):
            tool().draft(items=[("tomatoes", 0)])

    def test_a_negative_quantity_is_refused(self):
        with pytest.raises(ValueError):
            tool().draft(items=[("tomatoes", -2)])

    def test_a_float_price_in_the_catalogue_is_refused(self):
        # 4.50 dollars looks harmless and is 4 cents after int(). Money that
        # arrives as a float means somebody parsed a price wrong.
        bad = FakeOrderingTool(catalogue={"tomatoes": 4.50})
        with pytest.raises(TypeError):
            bad.draft(items=[("tomatoes", 1)])


class TestFingerprint:
    """Binds an approval to the exact cart the owner saw.

    Rule 3 of the money-safety list: the draft the owner approved is the cart
    that is bought. The fingerprint is how a later step notices that prices
    moved between drafting and checkout.
    """

    def test_the_same_cart_fingerprints_the_same(self):
        a = tool().draft(items=[("tomatoes", 2)])
        b = tool().draft(items=[("tomatoes", 2)])
        assert a.fingerprint == b.fingerprint

    def test_a_price_change_changes_the_fingerprint(self):
        before = tool().draft(items=[("tomatoes", 2)])
        moved = tool()
        moved.set_price("tomatoes", 5_00)
        after = moved.draft(items=[("tomatoes", 2)])
        assert after.fingerprint != before.fingerprint

    def test_a_quantity_change_changes_the_fingerprint(self):
        a = tool().draft(items=[("tomatoes", 2)])
        b = tool().draft(items=[("tomatoes", 3)])
        assert a.fingerprint != b.fingerprint

    def test_an_extra_item_changes_the_fingerprint(self):
        a = tool().draft(items=[("tomatoes", 2)])
        b = tool().draft(items=[("tomatoes", 2), ("flour", 1)])
        assert a.fingerprint != b.fingerprint

    def test_item_order_does_not_change_the_fingerprint(self):
        # The same cart typed in a different order is the same cart.
        a = tool().draft(items=[("tomatoes", 2), ("flour", 1)])
        b = tool().draft(items=[("flour", 1), ("tomatoes", 2)])
        assert a.fingerprint == b.fingerprint


class TestPlacing:
    def test_placing_returns_a_receipt_for_what_was_bought(self):
        t = tool()
        cart = t.draft(items=[("tomatoes", 2)])
        receipt = t.place(cart=cart, idempotency_key="k1")
        assert receipt.total_cents == cart.total_cents

    def test_the_receipt_carries_an_external_reference(self):
        # This is what proves an order exists on the provider's side, and what
        # a human would quote to DoorDash support.
        t = tool()
        receipt = t.place(cart=t.draft(items=[("tomatoes", 1)]), idempotency_key="k1")
        assert receipt.external_reference

    def test_the_receipt_lists_the_lines(self):
        t = tool()
        receipt = t.place(cart=t.draft(items=[("tomatoes", 2), ("flour", 1)]),
                          idempotency_key="k1")
        assert {line.name for line in receipt.lines} == {"tomatoes", "flour"}

    def test_the_tool_records_what_it_placed(self):
        t = tool()
        t.place(cart=t.draft(items=[("tomatoes", 1)]), idempotency_key="k1")
        assert len(t.placed) == 1


class TestIdempotency:
    """The single most dangerous path in the whole feature.

    A retried queue message, a worker that died after charging but before
    recording, a double-clicked approve — each must result in one order and one
    charge.
    """

    def test_the_same_key_twice_places_one_order(self):
        t = tool()
        cart = t.draft(items=[("tomatoes", 1)])
        t.place(cart=cart, idempotency_key="k1")
        t.place(cart=cart, idempotency_key="k1")
        assert len(t.placed) == 1

    def test_the_same_key_returns_the_original_receipt(self):
        t = tool()
        cart = t.draft(items=[("tomatoes", 1)])
        first = t.place(cart=cart, idempotency_key="k1")
        second = t.place(cart=cart, idempotency_key="k1")
        assert second.external_reference == first.external_reference

    def test_a_replay_with_a_different_cart_is_refused(self):
        # Same key, different contents means the caller has a bug. Returning
        # the old receipt would hide it; charging again would double-spend.
        t = tool()
        t.place(cart=t.draft(items=[("tomatoes", 1)]), idempotency_key="k1")
        with pytest.raises(OrderingError):
            t.place(cart=t.draft(items=[("flour", 1)]), idempotency_key="k1")

    def test_a_different_key_places_a_second_order(self):
        t = tool()
        cart = t.draft(items=[("tomatoes", 1)])
        t.place(cart=cart, idempotency_key="k1")
        t.place(cart=cart, idempotency_key="k2")
        assert len(t.placed) == 2

    def test_an_empty_key_is_refused(self):
        # Without a key there is no protection against a retry, so proceeding
        # would silently give up the guarantee.
        t = tool()
        with pytest.raises(ValueError):
            t.place(cart=t.draft(items=[("tomatoes", 1)]), idempotency_key="")


class TestFailure:
    """Providers fail. The fake has to be able to fail too, or nothing above it
    ever gets its error handling tested."""

    def test_an_injected_failure_raises(self):
        t = tool()
        t.fail_next("card declined")
        with pytest.raises(OrderingError, match="card declined"):
            t.place(cart=t.draft(items=[("tomatoes", 1)]), idempotency_key="k1")

    def test_a_failed_order_is_not_recorded_as_placed(self):
        t = tool()
        t.fail_next("card declined")
        with pytest.raises(OrderingError):
            t.place(cart=t.draft(items=[("tomatoes", 1)]), idempotency_key="k1")
        assert t.placed == []

    def test_a_failure_does_not_burn_the_idempotency_key(self):
        # An order that never happened must be retryable under the same key.
        t = tool()
        cart = t.draft(items=[("tomatoes", 1)])
        t.fail_next("network")
        with pytest.raises(OrderingError):
            t.place(cart=cart, idempotency_key="k1")
        receipt = t.place(cart=cart, idempotency_key="k1")
        assert receipt.total_cents == cart.total_cents


class TestLineItemArithmetic:
    def test_line_total_multiplies_cleanly(self):
        assert LineItem(name="x", quantity=3, unit_price_cents=333).total_cents == 999

    def test_a_cart_total_never_becomes_a_float(self):
        cart = Cart(store_id="s", store_name="n",
                    lines=(LineItem(name="x", quantity=3, unit_price_cents=333),),
                    fees_cents=1)
        assert isinstance(cart.total_cents, int)
