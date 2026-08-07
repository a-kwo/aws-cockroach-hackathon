"""Deciding which recurring orders are due today.

A standing order is a purchase the owner set up once — "the usual produce, every
Tuesday" — and then stopped thinking about. This module answers one question per
day per order: should this fire now?

The failure that matters most is firing too often. A nightly run that was down
for a week must not wake up and place seven orders, and a run that happens twice
in one day must not order twice. Both are tested below.

There is no scheduler here and deliberately so. The night already runs daily per
tenant, so it evaluates these itself — no per-tenant EventBridge rules to
provision, and nothing that can drift out of step with the row describing it.
"""

from datetime import date

import pytest

from brasstacks.standing_orders import StandingOrder, due_orders, is_due

TUESDAY = date(2026, 8, 4)
WEDNESDAY = date(2026, 8, 5)
NEXT_TUESDAY = date(2026, 8, 11)


def weekly(**kwargs) -> StandingOrder:
    base = dict(name="usual produce", items=(("tomatoes", 4),), weekday=1)
    base.update(kwargs)
    return StandingOrder(**base)


def every(days: int, **kwargs) -> StandingOrder:
    base = dict(name="dry goods", items=(("flour", 2),), interval_days=days)
    base.update(kwargs)
    return StandingOrder(**base)


class TestWeekly:
    def test_it_fires_on_its_weekday(self):
        assert is_due(weekly(), today=TUESDAY) is True

    def test_it_does_not_fire_on_other_days(self):
        assert is_due(weekly(), today=WEDNESDAY) is False

    def test_it_fires_again_the_following_week(self):
        assert is_due(weekly(last_run_on=TUESDAY), today=NEXT_TUESDAY) is True


class TestItNeverFiresTwice:
    """The single most important property here. Everything else is convenience;
    this one is the owner's money."""

    def test_it_does_not_fire_twice_in_one_day(self):
        # Two nightly runs, a manual re-run, a retried invocation — all of these
        # happen, and none of them may place a second order.
        assert is_due(weekly(last_run_on=TUESDAY), today=TUESDAY) is False

    def test_a_missed_week_fires_once_not_twice(self):
        # Down for three weeks. Today is Tuesday. That is one order, not three.
        stale = weekly(last_run_on=date(2026, 7, 14))
        assert is_due(stale, today=TUESDAY) is True

    def test_an_interval_order_overdue_by_a_fortnight_fires_once(self):
        stale = every(7, last_run_on=date(2026, 7, 21))
        assert is_due(stale, today=TUESDAY) is True
        # ...and having fired, is not due again the same day.
        assert is_due(every(7, last_run_on=TUESDAY), today=TUESDAY) is False


class TestInterval:
    def test_a_never_run_interval_order_is_due(self):
        assert is_due(every(14), today=TUESDAY) is True

    def test_it_waits_out_the_interval(self):
        order = every(14, last_run_on=date(2026, 8, 1))
        assert is_due(order, today=WEDNESDAY) is False

    def test_it_fires_once_the_interval_has_passed(self):
        order = every(7, last_run_on=date(2026, 7, 28))
        assert is_due(order, today=TUESDAY) is True

    def test_exactly_on_the_interval_fires(self):
        order = every(7, last_run_on=date(2026, 7, 28))
        assert is_due(order, today=date(2026, 8, 4)) is True


class TestSwitchedOff:
    def test_a_disabled_order_never_fires(self):
        assert is_due(weekly(enabled=False), today=TUESDAY) is False

    def test_a_paused_order_does_not_fire(self):
        assert is_due(weekly(paused_until=date(2026, 8, 20)), today=TUESDAY) is False

    def test_a_pause_covers_its_final_day(self):
        # "Paused until the 4th" means the 4th is still paused. The alternative
        # reading costs an owner an order they thought they had stopped.
        assert is_due(weekly(paused_until=TUESDAY), today=TUESDAY) is False

    def test_it_resumes_the_day_after_the_pause_ends(self):
        order = weekly(paused_until=date(2026, 8, 3))
        assert is_due(order, today=TUESDAY) is True


class TestMalformed:
    def test_an_order_with_no_schedule_is_rejected(self):
        # Silently never firing would look identical to "nothing was due", and
        # the owner would find out by running out of something.
        with pytest.raises(ValueError):
            is_due(StandingOrder(name="x", items=(("flour", 1),)), today=TUESDAY)

    def test_an_order_with_no_items_is_rejected(self):
        with pytest.raises(ValueError):
            is_due(StandingOrder(name="x", items=(), weekday=1), today=TUESDAY)

    def test_a_zero_interval_is_rejected(self):
        with pytest.raises(ValueError):
            is_due(every(0), today=TUESDAY)

    def test_a_negative_interval_is_rejected(self):
        with pytest.raises(ValueError):
            is_due(every(-7), today=TUESDAY)

    def test_an_out_of_range_weekday_is_rejected(self):
        with pytest.raises(ValueError):
            is_due(weekly(weekday=9), today=TUESDAY)


class TestSelectingTheDueOnes:
    def test_it_returns_only_what_is_due(self):
        orders = [weekly(), every(14, last_run_on=date(2026, 8, 3))]
        assert [o.name for o in due_orders(orders, today=TUESDAY)] == ["usual produce"]

    def test_an_empty_list_is_fine(self):
        assert due_orders([], today=TUESDAY) == []

    def test_a_malformed_order_does_not_hide_the_valid_ones(self):
        # One bad row must not stop a tenant's other standing orders from
        # running. It is reported, not silently dropped — see the note in
        # due_orders about why this does not raise.
        broken = StandingOrder(name="broken", items=(("x", 1),))
        due = due_orders([broken, weekly()], today=TUESDAY)
        assert [o.name for o in due] == ["usual produce"]

    def test_malformed_orders_are_reported(self):
        broken = StandingOrder(name="broken", items=(("x", 1),))
        problems: list[tuple[StandingOrder, str]] = []
        due_orders([broken, weekly()], today=TUESDAY, on_error=problems.append)
        assert len(problems) == 1
        assert problems[0][0].name == "broken"
