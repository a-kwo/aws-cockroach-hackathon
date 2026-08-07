"""The path from "we sold 40 of them" to a verdict that is not an estimate.

This is the loop the product's central claim depends on and the one thing the
deployed system could not do: `NoOutcomeSource` is honest, but it means every
verdict the Meter can ever reach is ESTIMATED and the published hit rate is
permanently undefined. These tests cover the other half — an owner reporting
what a move actually earned, and the Meter judging against it.

Two orderings matter and both are tested:

  * the figure arrives **before** the window closes, and the first verdict the
    find ever gets is a measured one;
  * the figure arrives **after** an estimate was already published, and the
    estimate is replaced — while a verdict that was itself measured never is.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.agents.meter import run_meter
from brasstacks.meter import Verdict, judge
from brasstacks.outcomes import (
    NoOutcomeSource,
    build_outcome_source,
    RecordedOutcomeSource,
    outcomes_from_reports,
)
from brasstacks.repository import EvidenceRef, InMemoryRepository, RepositoryError

TODAY = date(2026, 8, 7)
NOW = datetime(2026, 8, 7, 6, 0, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def business(repo):
    return repo.create_business(name="Yellow Cow", category="restaurant")


def accepted_find(repo, business, *, predicted=2300, days_ago=20, window=14):
    observation_id = repo.insert_observation(
        business, content=f"obs {predicted}", kind="review", embedding=VECTOR,
        observed_at=NOW - timedelta(days=days_ago))
    created = NOW - timedelta(days=days_ago)
    return repo.insert_find_with_evidence(
        business, title=f"find {predicted}", rationale="r", move="m", emoji="x",
        predicted_daily_cents=predicted, confidence=0.8,
        verify_after=(created + timedelta(days=window)).date(),
        status="accepted", created_at=created,
        evidence=[EvidenceRef(observation_id, 0.9)])


def source_for(repo, business):
    """What the nightly run builds: the Meter judging against stored reports."""
    return RecordedOutcomeSource(
        outcomes_from_reports(repo.find_outcome_reports(business)))


# ---------------------------------------------------------------------------
# Stored reports become outcomes the Meter can judge
# ---------------------------------------------------------------------------

class TestReportsBecomeOutcomes:
    def test_a_stored_report_is_a_real_measurement(self, repo, business):
        find_id = accepted_find(repo, business)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=7000,
                                 basis="week", note="Counted from the till.")

        outcomes = outcomes_from_reports(repo.find_outcome_reports(business))
        measured = outcomes[find_id]

        assert measured.has_outcome_data is True
        assert measured.actual_daily_cents == 1000
        # The method names where the number came from, because the ledger row
        # is read by the person whose money it is.
        assert "owner" in measured.method
        assert "week" in measured.method
        assert measured.note == "Counted from the till."

    def test_a_find_with_no_report_is_still_an_estimate(self, repo, business):
        find_id = accepted_find(repo, business)
        outcomes = source_for(repo, business)

        measured = outcomes.measure(
            repo.due_finds(business, today=TODAY)[0], business_id=business)

        assert measured.has_outcome_data is False
        assert measured.actual_daily_cents is None
        assert judge(predicted_daily_cents=2300,
                     actual_daily_cents=measured.actual_daily_cents,
                     has_outcome_data=False) is Verdict.ESTIMATED
        assert find_id  # the find exists; it simply has no measurement

    def test_the_newest_correction_is_the_one_judged(self, repo, business):
        find_id = accepted_find(repo, business)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=700,
                                 basis="week")
        repo.record_find_outcome(business, find_id=find_id, amount_cents=21000,
                                 basis="week")

        outcomes = outcomes_from_reports(repo.find_outcome_reports(business))
        assert outcomes[find_id].actual_daily_cents == 3000


# ---------------------------------------------------------------------------
# The figure arrives before the window closes
# ---------------------------------------------------------------------------

class TestFirstVerdictIsMeasured:
    def test_a_reported_win_is_verified_not_estimated(self, repo, business):
        find_id = accepted_find(repo, business, predicted=2300)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=21000,
                                 basis="week")   # $30.00/day against $23.00

        result = run_meter(repo=repo, outcomes=source_for(repo, business),
                           business_id=business, today=TODAY)

        assert result.verified == 1
        assert result.estimated == 0
        summary = repo.ledger_summary(business)
        assert summary.verified_count == 1
        assert summary.verified_daily_cents == 3000
        assert summary.hit_rate == 1.0

    def test_a_reported_nothing_is_a_published_miss(self, repo, business):
        # The product's whole claim. A move that earned nothing is recorded as
        # having earned nothing, and it moves the hit rate.
        find_id = accepted_find(repo, business, predicted=2300)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=0,
                                 basis="week")

        result = run_meter(repo=repo, outcomes=source_for(repo, business),
                           business_id=business, today=TODAY)

        assert result.misses == 1
        assert repo.ledger_summary(business).miss_count == 1
        assert repo.ledger_summary(business).hit_rate == 0.0

    def test_a_figure_far_under_the_prediction_is_a_miss(self, repo, business):
        # 2% of what was promised is not a win because it is above zero.
        find_id = accepted_find(repo, business, predicted=10000)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=1400,
                                 basis="week")   # $2.00/day against $100.00

        result = run_meter(repo=repo, outcomes=source_for(repo, business),
                           business_id=business, today=TODAY)
        assert result.misses == 1

    def test_one_tenants_report_never_scores_another_tenants_find(
            self, repo, business):
        rival = repo.create_business(name="Rival", category="restaurant")
        theirs = accepted_find(repo, rival, predicted=2300)
        with pytest.raises(RepositoryError):
            repo.record_find_outcome(business, find_id=theirs,
                                     amount_cents=21000, basis="week")

        run_meter(repo=repo, outcomes=source_for(repo, rival),
                  business_id=rival, today=TODAY)
        assert repo.ledger_summary(rival).verified_count == 0
        assert repo.ledger_summary(rival).estimated_count == 1


# ---------------------------------------------------------------------------
# The figure arrives after an estimate was already published
# ---------------------------------------------------------------------------

class TestReplacingAnEstimate:
    def test_an_estimate_is_replaced_by_the_measurement(self, repo, business):
        find_id = accepted_find(repo, business, predicted=2300)

        first = run_meter(repo=repo, outcomes=NoOutcomeSource(),
                          business_id=business, today=TODAY)
        assert first.estimated == 1

        repo.record_find_outcome(business, find_id=find_id, amount_cents=21000,
                                 basis="week")
        second = run_meter(repo=repo, outcomes=source_for(repo, business),
                           business_id=business, today=TODAY)

        assert second.remeasured == 1
        assert second.verified == 1
        summary = repo.ledger_summary(business)
        # One find, one verdict. The estimate was replaced, not joined.
        assert summary.verified_count == 1
        assert summary.estimated_count == 0
        assert summary.verified_daily_cents == 3000

    def test_the_replacement_is_reported_in_the_run_note(self, repo, business):
        # A night that re-scored an estimate on the owner's numbers looks
        # identical to a quiet night unless the receipt says so.
        find_id = accepted_find(repo, business, predicted=2300)
        run_meter(repo=repo, outcomes=NoOutcomeSource(), business_id=business,
                  today=TODAY)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=21000,
                                 basis="week")

        result = run_meter(repo=repo, outcomes=source_for(repo, business),
                           business_id=business, today=TODAY)
        assert "re-scored" in result.note

    def test_a_published_miss_is_never_quietly_upgraded(self, repo, business):
        # The append-only rule that matters. Reporting a better number after a
        # measured miss does not turn the miss into a win.
        find_id = accepted_find(repo, business, predicted=2300)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=0,
                                 basis="week")
        run_meter(repo=repo, outcomes=source_for(repo, business),
                  business_id=business, today=TODAY)
        assert repo.ledger_summary(business).miss_count == 1

        repo.record_find_outcome(business, find_id=find_id, amount_cents=70000,
                                 basis="week")
        again = run_meter(repo=repo, outcomes=source_for(repo, business),
                          business_id=business, today=TODAY)

        assert again.judged == 0
        assert again.remeasured == 0
        summary = repo.ledger_summary(business)
        assert summary.miss_count == 1
        assert summary.verified_count == 0

    def test_a_quiet_night_does_not_rewrite_yesterdays_estimate(
            self, repo, business):
        # Without the "newer than the verdict" gate the Meter re-judges every
        # estimate that ever had a report, every night, forever.
        find_id = accepted_find(repo, business, predicted=2300)
        repo.record_find_outcome(business, find_id=find_id, amount_cents=21000,
                                 basis="week")
        run_meter(repo=repo, outcomes=source_for(repo, business),
                  business_id=business, today=TODAY)

        third = run_meter(repo=repo, outcomes=source_for(repo, business),
                          business_id=business, today=TODAY)
        assert third.judged == 0
        assert third.remeasured == 0

    def test_an_estimate_with_no_new_figure_is_left_exactly_as_it_was(
            self, repo, business):
        accepted_find(repo, business, predicted=2300)
        run_meter(repo=repo, outcomes=NoOutcomeSource(), business_id=business,
                  today=TODAY)
        second = run_meter(repo=repo, outcomes=NoOutcomeSource(),
                           business_id=business, today=TODAY)

        assert second.judged == 0
        assert repo.ledger_summary(business).estimated_count == 1


# ---------------------------------------------------------------------------
# What the nightly run actually wires up
# ---------------------------------------------------------------------------

class TestTheNightlyWiring:
    def test_the_source_is_built_per_tenant_from_stored_reports(self, repo):
        first = repo.create_business(name="Yellow Cow", category="restaurant")
        second = repo.create_business(name="Asaka", category="restaurant")
        mine = accepted_find(repo, first, predicted=2300)
        theirs = accepted_find(repo, second, predicted=2300)
        repo.record_find_outcome(first, find_id=mine, amount_cents=21000,
                                 basis="week")

        source = build_outcome_source(repo, first)
        other = build_outcome_source(repo, second)

        assert source.measure(repo.due_finds(first, today=TODAY)[0],
                              business_id=first).has_outcome_data is True
        # One tenant's figures are not visible to another's Meter run.
        assert other.measure(repo.due_finds(second, today=TODAY)[0],
                             business_id=second).has_outcome_data is False
        assert theirs

    def test_a_tenant_with_no_reports_still_gets_honest_estimates(self, repo,
                                                                 business):
        accepted_find(repo, business, predicted=2300)
        result = run_meter(repo=repo, outcomes=build_outcome_source(repo, business),
                           business_id=business, today=TODAY)
        assert result.estimated == 1
        assert result.verified == 0
