"""The Meter's judgement is the highest-risk code in the product.

It decides whether a prediction the agent made on a previous night actually paid
off. Getting this wrong means the ledger lies — and the ledger is the entire
value proposition. So it gets exhaustive tests, including every unhappy path.

Money is integer cents throughout. No floats.
"""

import pytest

from brasstacks.meter import Verdict, judge


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
