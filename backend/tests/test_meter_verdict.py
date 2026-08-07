"""The Meter's judgement is the highest-risk code in the product.

It decides whether a prediction the agent made on a previous night actually paid
off. Getting this wrong means the ledger lies — and the ledger is the entire
value proposition. So it gets exhaustive tests, including every unhappy path.

Money is integer cents throughout. No floats.
"""

import pytest

from brasstacks.meter import (
    MEASUREMENT_BASES,
    Verdict,
    daily_cents_from,
    judge,
)


class TestNoOutcomeData:
    """Before real outcome data exists, a prediction is 'estimated' — never a
    miss. Calling something a miss when we simply haven't measured it yet would
    be dishonest in the direction that makes us look better (fewer live claims),
    but it still misrepresents the record."""

    def test_without_outcome_data_is_estimated(self):
        assert judge(predicted_daily_cents=2300, actual_daily_cents=0,
                     has_outcome_data=False) is Verdict.ESTIMATED

    def test_outcome_data_absent_beats_a_supplied_actual(self):
        # If the caller says there's no data, we don't quietly trust the number.
        assert judge(predicted_daily_cents=2300, actual_daily_cents=9999,
                     has_outcome_data=False) is Verdict.ESTIMATED


class TestAbsentMeasurement:
    """``None`` is how "we never measured this" travels.

    It exists because the Meter used to hand the prediction back as the actual
    when nothing was connected, which put a forecast in a column named actual.
    The honest value there is nothing at all, so the judge has to accept
    nothing — and has to refuse it when the caller claims a measurement.
    """

    def test_absent_actual_without_outcome_data_is_estimated(self):
        assert judge(predicted_daily_cents=2300, actual_daily_cents=None,
                     has_outcome_data=False) is Verdict.ESTIMATED

    def test_claiming_outcome_data_with_no_number_is_rejected(self):
        # has_outcome_data=True is an assertion that a measurement exists. If
        # the number is missing, the source contradicted itself and we fail
        # loudly rather than score a find on nothing.
        with pytest.raises(ValueError):
            judge(predicted_daily_cents=2300, actual_daily_cents=None,
                  has_outcome_data=True)

    def test_a_corrupt_prediction_still_fails_first(self):
        # Validation order is load-bearing: a negative prediction is a bug worth
        # surfacing even on a run that had no measurement to score.
        with pytest.raises(ValueError):
            judge(predicted_daily_cents=-100, actual_daily_cents=None,
                  has_outcome_data=False)


class TestMiss:
    """We publish misses. That is the differentiator, so the logic that produces
    them must be strict."""

    def test_zero_actual_is_a_miss(self):
        assert judge(predicted_daily_cents=2300, actual_daily_cents=0,
                     has_outcome_data=True) is Verdict.MISS

    def test_negative_actual_is_a_miss(self):
        # A move can actively cost money. That is still a miss, not a negative
        # verified win.
        assert judge(predicted_daily_cents=2300, actual_daily_cents=-500,
                     has_outcome_data=True) is Verdict.MISS

    def test_trivial_fraction_of_prediction_is_a_miss(self):
        # 2% of what we promised is not a win just because it is positive.
        assert judge(predicted_daily_cents=10000, actual_daily_cents=200,
                     has_outcome_data=True) is Verdict.MISS


class TestVerified:
    def test_meeting_the_prediction_is_verified(self):
        assert judge(predicted_daily_cents=2300, actual_daily_cents=2300,
                     has_outcome_data=True) is Verdict.VERIFIED

    def test_beating_the_prediction_is_verified(self):
        assert judge(predicted_daily_cents=2300, actual_daily_cents=9900,
                     has_outcome_data=True) is Verdict.VERIFIED

    def test_underperforming_but_material_is_still_verified(self):
        # Half of what we promised is a real win. The ledger shows predicted vs
        # actual, so the shortfall is visible without being branded a miss.
        assert judge(predicted_daily_cents=10000, actual_daily_cents=5000,
                     has_outcome_data=True) is Verdict.VERIFIED


class TestThresholdBoundary:
    """The miss/verified line sits at 25% of the prediction. Boundary conditions
    get pinned down explicitly so a future refactor cannot drift them silently."""

    def test_exactly_at_threshold_is_verified(self):
        assert judge(predicted_daily_cents=10000, actual_daily_cents=2500,
                     has_outcome_data=True) is Verdict.VERIFIED

    def test_one_cent_below_threshold_is_a_miss(self):
        assert judge(predicted_daily_cents=10000, actual_daily_cents=2499,
                     has_outcome_data=True) is Verdict.MISS

    def test_threshold_is_configurable(self):
        # A stricter 50% bar moves the line: 4000 clears the default 25% bar but
        # not this one.
        assert judge(predicted_daily_cents=10000, actual_daily_cents=4000,
                     has_outcome_data=True) is Verdict.VERIFIED
        assert judge(predicted_daily_cents=10000, actual_daily_cents=4000,
                     has_outcome_data=True, miss_threshold=0.5) is Verdict.MISS
        assert judge(predicted_daily_cents=10000, actual_daily_cents=5000,
                     has_outcome_data=True, miss_threshold=0.5) is Verdict.VERIFIED


class TestDegenerateInput:
    def test_zero_prediction_with_real_gain_is_verified(self):
        # Guards division by zero. A find we valued at nothing that earned real
        # money is a win, not a crash.
        assert judge(predicted_daily_cents=0, actual_daily_cents=1500,
                     has_outcome_data=True) is Verdict.VERIFIED

    def test_zero_prediction_and_zero_actual_is_a_miss(self):
        assert judge(predicted_daily_cents=0, actual_daily_cents=0,
                     has_outcome_data=True) is Verdict.MISS

    def test_negative_prediction_is_rejected(self):
        # The Analyst must never write a negative prediction. If one appears,
        # the data is corrupt and we fail loudly rather than judge it.
        with pytest.raises(ValueError):
            judge(predicted_daily_cents=-100, actual_daily_cents=0,
                  has_outcome_data=True)

    def test_non_integer_cents_are_rejected(self):
        # Floats are how money bugs get in. The boundary refuses them.
        with pytest.raises(TypeError):
            judge(predicted_daily_cents=23.5, actual_daily_cents=0,
                  has_outcome_data=True)

    def test_threshold_outside_unit_interval_is_rejected(self):
        with pytest.raises(ValueError):
            judge(predicted_daily_cents=10000, actual_daily_cents=5000,
                  has_outcome_data=True, miss_threshold=1.5)


class TestReportedAmountToDailyCents:
    """An owner reports what a move earned over a period they actually track.

    Nobody knows their takings per day; they know "about $300 a week". So the
    entry point is an amount and the period it covers, and the conversion to
    the daily figure the ledger judges against happens here — in integer cents,
    once, rather than in the browser or three times in three callers.
    """

    def test_a_daily_figure_passes_through(self):
        assert daily_cents_from(4200, "day") == 4200

    def test_a_weekly_figure_is_divided_by_seven(self):
        assert daily_cents_from(7000, "week") == 1000

    def test_a_monthly_figure_uses_the_same_thirty_days_as_the_board(self):
        # The card renders a daily prediction as `daily * 30` a month. A month
        # that meant 30.4 days here would make a reported figure disagree with
        # the forecast it is being measured against, for no reason the owner
        # could see.
        assert daily_cents_from(3000, "month") == 100

    def test_rounding_is_half_up_and_never_leaves_a_fraction(self):
        # 100/7 = 14.28..., 15/30 = 0.5. Both land on an integer number of
        # cents, because a fraction of a cent has nowhere to be stored.
        assert daily_cents_from(100, "week") == 14
        assert daily_cents_from(15, "month") == 1

    def test_a_reported_zero_stays_zero(self):
        # This is the published miss. It must survive the conversion intact.
        assert daily_cents_from(0, "week") == 0

    def test_a_loss_keeps_its_sign(self):
        # A move can cost money, and `judge` accepts a negative actual.
        assert daily_cents_from(-3000, "month") == -100
        assert daily_cents_from(-100, "week") == -14

    def test_a_float_amount_is_rejected(self):
        # Same rule as the verdict itself: money arrives as integer cents or
        # not at all.
        with pytest.raises(TypeError):
            daily_cents_from(70.5, "week")

    def test_a_bool_is_not_an_amount(self):
        with pytest.raises(TypeError):
            daily_cents_from(True, "day")

    def test_an_unknown_period_is_rejected(self):
        with pytest.raises(ValueError):
            daily_cents_from(7000, "fortnight")

    def test_the_offered_periods_are_the_ones_the_ui_offers(self):
        assert set(MEASUREMENT_BASES) == {"day", "week", "month"}
