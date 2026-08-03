"""An agent that dies must not leave its run row saying "running" forever.

This is a honesty bug before it is a plumbing bug. The board reads open
`agent_run` rows as "the agents are working", so a row that no process will ever
close makes the product claim work is in progress when nothing is running at
all. Yellow Cow Korean BBQ carried exactly such a row — an Analyst run opened
2026-08-02 that produced three finds and then never closed — and the owner's
board told them the agents were working for a day and a half.

Every agent opens its run before it can know whether it will succeed, so the
close has to be guaranteed by the caller rather than remembered on each path.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from brasstacks.agent_runs import closing_run
from brasstacks.agents.analyst import run_analyst
from brasstacks.agents.meter import run_meter
from brasstacks.agents.radar import run_radar
from brasstacks.outcomes import NoOutcomeSource
from brasstacks.providers import FakeEmbedder, FakeReasoner
from brasstacks.repository import InMemoryRepository
from brasstacks.signals import RawSignal

TODAY = date(2026, 7, 28)
NOW = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def business(repo):
    return repo.create_business(name="Yellow Cow", category="restaurant",
                                city="Gardena")


class ExplodingEmbedder(FakeEmbedder):
    """A provider that fails in a way no agent has a named handler for."""

    def embed(self, texts):
        raise RuntimeError("the embedding endpoint went away")


class ExplodingOutcomes(NoOutcomeSource):
    def measure(self, find, *, business_id):
        raise RuntimeError("the outcome source went away")


def only_run(repo, business):
    [run] = repo.recent_runs(business, limit=1)
    return run


class TestTheGuard:
    def test_it_closes_the_run_when_the_body_raises(self, repo, business):
        run_id = repo.start_run(business, agent="analyst")

        with pytest.raises(RuntimeError):
            with closing_run(repo, run_id):
                raise RuntimeError("boom")

        run = only_run(repo, business)
        assert run.status == "failed"
        assert "boom" in (run.error or "")

    def test_it_names_the_exception_type(self, repo, business):
        """"failed" alone sends an operator to CloudWatch. The type is what
        makes the row worth reading."""
        run_id = repo.start_run(business, agent="analyst")

        with pytest.raises(ValueError):
            with closing_run(repo, run_id):
                raise ValueError("bad input")

        assert only_run(repo, business).error.startswith("ValueError:")

    def test_it_re_raises_rather_than_swallowing(self, repo, business):
        """Closing the row must not turn a crash into a silent success."""
        run_id = repo.start_run(business, agent="analyst")

        with pytest.raises(RuntimeError, match="boom"):
            with closing_run(repo, run_id):
                raise RuntimeError("boom")

    def test_it_leaves_a_run_the_body_already_closed_alone(self, repo, business):
        """Agents close their own runs on the paths they anticipate. The guard
        must not overwrite a considered note with a generic one."""
        run_id = repo.start_run(business, agent="analyst")

        with closing_run(repo, run_id):
            repo.finish_run(run_id, status="ok", note="42 retrieved")

        run = only_run(repo, business)
        assert run.status == "ok"
        assert run.note == "42 retrieved"

    def test_a_body_that_succeeds_is_untouched(self, repo, business):
        run_id = repo.start_run(business, agent="meter")

        with closing_run(repo, run_id):
            pass

        # Still open: the guard exists for crashes, not for closing runs the
        # agent is still legitimately using.
        assert only_run(repo, business).status == "running"


class TestTheAgentsCloseTheirRuns:
    """The failure each agent has no named handler for."""

    def test_the_analyst_leaves_no_open_run(self, repo, business):
        with pytest.raises(RuntimeError):
            run_analyst(repo=repo, embedder=ExplodingEmbedder(),
                        reasoner=FakeReasoner([]), business_id=business,
                        today=TODAY)

        assert only_run(repo, business).status == "failed"

    def test_the_radar_leaves_no_open_run(self, repo, business):
        source = _StubSource("reviews", [
            RawSignal(content="Great brisket.", kind="review",
                      source_name="reviews", observed_at=NOW),
        ])

        with pytest.raises(RuntimeError):
            run_radar(repo=repo, embedder=ExplodingEmbedder(),
                      business_id=business, sources=[source], now=NOW)

        assert only_run(repo, business).status == "failed"

    def test_the_meter_leaves_no_open_run(self):
        # The Meter guards `measure` and the duplicate-verdict case by name. A
        # ledger write that fails some other way is the gap.
        repo = _BrokenLedgerRepository()
        business = repo.create_business(name="Yellow Cow", category="restaurant")
        _due_find(repo, business)

        with pytest.raises(RuntimeError):
            run_meter(repo=repo, outcomes=NoOutcomeSource(),
                      business_id=business, today=TODAY)

        assert only_run(repo, business).status == "failed"


class _StubSource:
    def __init__(self, name, signals):
        self.name = name
        self._signals = signals

    def fetch(self, *, business_name, city, limit):
        return list(self._signals)


class _BrokenLedgerRepository(InMemoryRepository):
    def insert_ledger_entry(self, *args, **kwargs):
        raise RuntimeError("the ledger write went away")


def _due_find(repo, business):
    from brasstacks.repository import EvidenceRef

    observation = repo.insert_observation(
        business, content="Saturday waits are long", kind="review",
        embedding=FakeEmbedder().embed(["x"])[0], observed_at=NOW)
    return repo.insert_find_with_evidence(
        business, title="Fix the wait", rationale="Because.", move="Do it.",
        emoji="⏱️", predicted_daily_cents=2300, confidence=0.7,
        verify_after=TODAY, status="accepted",
        evidence=[EvidenceRef(observation, 0.6)])
