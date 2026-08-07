"""Estimating what is left in the store cupboard.

There is no inventory system here and no camera in the walk-in. This works from
what the owner declared they keep on hand, what they last bought, and how long
ago — which means every number it produces is an **estimate**, not a
measurement.

That distinction is load-bearing. Brass Tacks already refuses to print a
modelled figure in a column headed Actual, and a depletion guess is the same
trap in a different costume. So this module says how it reached its answer, and
the ordering path treats anything it produces as needing a human.

Quantities are whole units. No floats.
"""

from datetime import date

import pytest

from brasstacks.stock import StockItem, estimate_remaining, low_items

BOUGHT = date(2026, 8, 1)


def item(**kwargs) -> StockItem:
    base = dict(name="tomatoes", reorder_at=3, usage_per_week=7,
                last_purchased_on=BOUGHT, last_purchased_quantity=14)
    base.update(kwargs)
    return StockItem(**base)


class TestFreshPurchase:
    def test_on_the_day_it_was_bought_nothing_is_used(self):
        assert estimate_remaining(item(), today=BOUGHT).remaining == 14

    def test_a_fresh_purchase_is_not_low(self):
        assert estimate_remaining(item(), today=BOUGHT).is_low is False


class TestDepletion:
    def test_a_week_at_seven_a_week_uses_seven(self):
        est = estimate_remaining(item(), today=date(2026, 8, 8))
        assert est.remaining == 7

    def test_it_rounds_consumption_up(self):
        # One day at 7/week is exactly 1. Two days at 3/week is 6/7 of a unit,
        # and the safe rounding is to assume the unit is gone: a false prompt
        # costs the owner a tap, running out costs them service.
        est = estimate_remaining(item(usage_per_week=3), today=date(2026, 8, 3))
        assert est.remaining == 13

    def test_it_never_reports_negative_stock(self):
        est = estimate_remaining(item(), today=date(2027, 1, 1))
        assert est.remaining == 0

    def test_zero_usage_never_depletes(self):
        est = estimate_remaining(item(usage_per_week=0), today=date(2027, 1, 1))
        assert est.remaining == 14
        assert est.is_low is False


class TestLowness:
    def test_it_is_low_at_the_reorder_point(self):
        # Reorder *at* 3 means 3 triggers it, not 2.
        est = estimate_remaining(item(reorder_at=7), today=date(2026, 8, 8))
        assert est.is_low is True

    def test_it_is_not_low_just_above_the_reorder_point(self):
        est = estimate_remaining(item(reorder_at=6), today=date(2026, 8, 8))
        assert est.is_low is False

    def test_an_immediately_insufficient_purchase_is_low(self):
        est = estimate_remaining(item(reorder_at=20), today=BOUGHT)
        assert est.is_low is True


class TestUnknown:
    """Never bought through Brass Tacks means no basis for a guess. Guessing
    anyway would be inventing a number and then ordering against it."""

    def test_an_item_never_purchased_is_unknown(self):
        est = estimate_remaining(
            item(last_purchased_on=None, last_purchased_quantity=None),
            today=BOUGHT)
        assert est.known is False

    def test_an_unknown_item_reports_no_remaining_figure(self):
        est = estimate_remaining(
            item(last_purchased_on=None, last_purchased_quantity=None),
            today=BOUGHT)
        assert est.remaining is None

    def test_an_unknown_item_is_not_treated_as_low(self):
        # "We don't know" must not become "order some", or the first night
        # would buy one of everything the owner ever listed.
        est = estimate_remaining(
            item(last_purchased_on=None, last_purchased_quantity=None),
            today=BOUGHT)
        assert est.is_low is False


class TestItShowsItsWorking:
    """The owner has to be able to correct a wrong model, which means seeing
    what it assumed — not just its conclusion."""

    def test_the_basis_mentions_how_long_ago(self):
        est = estimate_remaining(item(), today=date(2026, 8, 10))
        assert "9 days" in est.basis

    def test_the_basis_mentions_the_usage_rate(self):
        est = estimate_remaining(item(), today=date(2026, 8, 10))
        assert "7" in est.basis

    def test_an_unknown_item_says_why_it_is_unknown(self):
        est = estimate_remaining(
            item(last_purchased_on=None, last_purchased_quantity=None),
            today=BOUGHT)
        assert "never" in est.basis.lower() or "no record" in est.basis.lower()

    def test_the_estimate_is_labelled_an_estimate(self):
        assert estimate_remaining(item(), today=BOUGHT).is_estimate is True


class TestMalformed:
    def test_a_negative_usage_rate_is_rejected(self):
        with pytest.raises(ValueError):
            estimate_remaining(item(usage_per_week=-1), today=BOUGHT)

    def test_a_negative_reorder_point_is_rejected(self):
        with pytest.raises(ValueError):
            estimate_remaining(item(reorder_at=-1), today=BOUGHT)

    def test_a_negative_purchase_quantity_is_rejected(self):
        with pytest.raises(ValueError):
            estimate_remaining(item(last_purchased_quantity=-5), today=BOUGHT)

    def test_a_purchase_dated_in_the_future_is_rejected(self):
        # Clock skew or a bad import. Treating it as "bought -3 days ago" would
        # silently inflate stock.
        with pytest.raises(ValueError):
            estimate_remaining(item(), today=date(2026, 7, 1))

    def test_a_float_quantity_is_rejected(self):
        with pytest.raises(TypeError):
            estimate_remaining(item(last_purchased_quantity=14.0), today=BOUGHT)


class TestSelectingWhatIsLow:
    def test_it_returns_only_the_low_ones(self):
        items = [item(name="tomatoes", reorder_at=7),
                 item(name="flour", reorder_at=1)]
        low = low_items(items, today=date(2026, 8, 8))
        assert [e.name for e in low] == ["tomatoes"]

    def test_unknown_items_are_left_out(self):
        items = [item(name="saffron", last_purchased_on=None,
                      last_purchased_quantity=None)]
        assert low_items(items, today=date(2026, 8, 8)) == []

    def test_an_empty_list_is_fine(self):
        assert low_items([], today=BOUGHT) == []
