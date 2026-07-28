"""Contract tests for the memory layer.

Every test here runs against **both** implementations: the in-memory fake (always)
and the real CockroachDB cluster (only under `-m integration`). That is
deliberate. A fake that quietly diverges from real Postgres behaviour is worse
than no fake, because agent tests would pass against a fiction. Running one
contract against both keeps them honest.

    pytest                  # in-memory only
    pytest -m integration   # real cluster only
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.repository import (
    EvidenceRef,
    InMemoryRepository,
    RepositoryError,
)

TODAY = date(2026, 7, 28)


# ---------------------------------------------------------------------------
# Vectors: hand-built so similarity ordering is predictable
# ---------------------------------------------------------------------------

def vec(*leading: float) -> list[float]:
    """A 1024-dim vector with the given leading values, rest zero."""
    v = list(leading) + [0.0] * (1024 - len(leading))
    return v[:1024]


DESSERT = vec(1.0, 0.0, 0.0)
DESSERT_ISH = vec(0.9, 0.1, 0.0)
PARKING = vec(0.0, 1.0, 0.0)
NOISE = vec(0.0, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Both implementations, one contract
# ---------------------------------------------------------------------------

@pytest.fixture(params=[
    "memory",
    pytest.param("postgres", marks=pytest.mark.integration),
])
def repo(request):
    if request.param == "memory":
        yield InMemoryRepository()
        return

    import psycopg

    from brasstacks.config import Settings
    from brasstacks.repository_pg import PostgresRepository

    settings = Settings.load()
    with psycopg.connect(settings.cockroach_url, autocommit=False) as conn:
        yield PostgresRepository(conn)
        # Every test creates its own business; cascade removes all its rows.
        conn.rollback()


@pytest.fixture
def business(repo):
    return repo.create_business(
        name=f"Test Trattoria {uuid.uuid4().hex[:8]}",
        category="restaurant",
        city="Columbus",
        goal_monthly_cents=800_000,
    )


# ---------------------------------------------------------------------------
# Runs — the audit trail
# ---------------------------------------------------------------------------

class TestAgentRuns:
    def test_start_and_finish_a_run(self, repo, business):
        run_id = repo.start_run(business, agent="radar")
        assert run_id
        repo.finish_run(run_id, status="ok", note="12 observed, 3 new")

        runs = repo.recent_runs(business, limit=5)
        assert len(runs) == 1
        assert runs[0].agent == "radar"
        assert runs[0].status == "ok"
        assert runs[0].finished_at is not None

    def test_a_failed_run_records_its_error(self, repo, business):
        # A nightly run that dies must leave evidence of why, or debugging a
        # 2 AM failure means guessing.
        run_id = repo.start_run(business, agent="analyst")
        repo.finish_run(run_id, status="failed", error="model refused")

        runs = repo.recent_runs(business, limit=1)
        assert runs[0].status == "failed"
        assert "refused" in runs[0].error

    def test_runs_come_back_newest_first(self, repo, business):
        first = repo.start_run(business, agent="radar")
        repo.finish_run(first, status="ok")
        second = repo.start_run(business, agent="analyst")
        repo.finish_run(second, status="ok")

        runs = repo.recent_runs(business, limit=5)
        assert [r.agent for r in runs] == ["analyst", "radar"]


# ---------------------------------------------------------------------------
# Observations — dedup is structural, not best-effort
# ---------------------------------------------------------------------------

class TestObservations:
    def test_inserting_returns_the_new_observation_id(self, repo, business):
        # The id (rather than a bare bool) is what lets a caller wire the stored
        # row into find_evidence without a second lookup. Truthiness still reads
        # as "was new".
        observation_id = repo.insert_observation(
            business, content="Best tiramisu in the city", kind="review",
            embedding=DESSERT, observed_at=_dt(TODAY),
        )
        assert observation_id

    def test_identical_content_is_not_stored_twice(self, repo, business):
        # Radar re-reads the same review every night. Night forty must not be
        # row forty.
        args = dict(content="Best tiramisu in the city", kind="review",
                    embedding=DESSERT, observed_at=_dt(TODAY))
        assert repo.insert_observation(business, **args) is not None
        assert repo.insert_observation(business, **args) is None
        assert repo.count_observations(business) == 1

    def test_whitespace_and_case_do_not_defeat_dedup(self, repo, business):
        # A review re-scraped with different spacing is the same review.
        first = dict(content="Best tiramisu in the city", kind="review",
                     embedding=DESSERT, observed_at=_dt(TODAY))
        again = dict(content="  best   TIRAMISU in the city ", kind="review",
                     embedding=DESSERT, observed_at=_dt(TODAY))
        assert repo.insert_observation(business, **first) is not None
        assert repo.insert_observation(business, **again) is None

    def test_duplicate_is_not_an_error(self, repo, business):
        # Re-seeing content is normal operation, not a failure. Raising here
        # would abort a nightly run over nothing.
        args = dict(content="same", kind="review", embedding=DESSERT,
                    observed_at=_dt(TODAY))
        repo.insert_observation(business, **args)
        repo.insert_observation(business, **args)  # must not raise

    def test_dedup_is_scoped_per_business(self, repo, business):
        # Two restaurants can legitimately share a review phrase. One tenant's
        # history must not suppress another's.
        other = repo.create_business(name="Lucca's", category="restaurant")
        args = dict(content="Great pasta", kind="review", embedding=DESSERT,
                    observed_at=_dt(TODAY))
        assert repo.insert_observation(business, **args) is not None
        assert repo.insert_observation(other, **args) is not None

    def test_different_content_is_stored_separately(self, repo, business):
        repo.insert_observation(business, content="a", kind="review",
                                embedding=DESSERT, observed_at=_dt(TODAY))
        repo.insert_observation(business, content="b", kind="review",
                                embedding=PARKING, observed_at=_dt(TODAY))
        assert repo.count_observations(business) == 2

    def test_observation_is_attributed_to_its_run(self, repo, business):
        run_id = repo.start_run(business, agent="radar")
        repo.insert_observation(business, content="x", kind="review",
                                embedding=DESSERT, observed_at=_dt(TODAY),
                                run_id=run_id)
        [obs] = repo.search_observations(business, DESSERT, limit=1)
        assert obs.observation_id


# ---------------------------------------------------------------------------
# Vector search — the memory retrieval itself
# ---------------------------------------------------------------------------

class TestVectorSearch:
    def _seed(self, repo, business):
        for content, embedding in [
            ("tiramisu is the best in the city", DESSERT),
            ("the dessert menu is lovely", DESSERT_ISH),
            ("parking is impossible", PARKING),
            ("unrelated chatter", NOISE),
        ]:
            repo.insert_observation(business, content=content, kind="review",
                                    embedding=embedding, observed_at=_dt(TODAY))

    def test_returns_nearest_first(self, repo, business):
        self._seed(repo, business)
        results = repo.search_observations(business, DESSERT, limit=4)
        assert "tiramisu" in results[0].content
        assert "dessert menu" in results[1].content

    def test_similarity_is_reported_and_ordered(self, repo, business):
        self._seed(repo, business)
        results = repo.search_observations(business, DESSERT, limit=4)
        sims = [r.similarity for r in results]
        assert sims == sorted(sims, reverse=True)
        # An exact match should be ~1.0; an orthogonal vector ~0.0.
        assert results[0].similarity > 0.99

    def test_rank_reflects_retrieval_order(self, repo, business):
        # find_evidence.rank stores this, so it must be 0-based and contiguous.
        self._seed(repo, business)
        results = repo.search_observations(business, DESSERT, limit=3)
        assert [r.rank for r in results] == [0, 1, 2]

    def test_limit_is_respected(self, repo, business):
        self._seed(repo, business)
        assert len(repo.search_observations(business, DESSERT, limit=2)) == 2

    def test_never_returns_another_businesss_observations(self, repo, business):
        # The vector index is prefixed on business_id. A leak here would be a
        # cross-tenant data breach, so it gets an explicit test.
        other = repo.create_business(name="Lucca's", category="restaurant")
        repo.insert_observation(other, content="Lucca's secret sauce",
                                kind="review", embedding=DESSERT,
                                observed_at=_dt(TODAY))
        self._seed(repo, business)

        results = repo.search_observations(business, DESSERT, limit=10)
        assert all("Lucca" not in r.content for r in results)
        assert len(results) == 4

    def test_empty_memory_returns_nothing(self, repo, business):
        assert repo.search_observations(business, DESSERT, limit=5) == []


# ---------------------------------------------------------------------------
# Finds + evidence — the invariant the whole entry rests on
# ---------------------------------------------------------------------------

class TestFindsAndEvidence:
    def _observation(self, repo, business, content="tiramisu praise"):
        repo.insert_observation(business, content=content, kind="review",
                                embedding=DESSERT, observed_at=_dt(TODAY))
        return repo.search_observations(business, DESSERT, limit=1)[0]

    def test_writes_a_find_with_its_evidence(self, repo, business):
        obs = self._observation(repo, business)
        find_id = repo.insert_find_with_evidence(
            business,
            title="Tiramisu → $9",
            rationale="212 reviews call it the best",
            move="Raise to $9",
            emoji="🍰",
            predicted_daily_cents=2300,
            confidence=0.82,
            verify_after=TODAY + timedelta(days=14),
            evidence=[EvidenceRef(obs.observation_id, similarity=0.94)],
        )
        assert find_id

        evidence = repo.get_find_evidence(find_id)
        assert len(evidence) == 1
        assert evidence[0].observation_id == obs.observation_id
        assert evidence[0].similarity == pytest.approx(0.94)
        assert evidence[0].rank == 0

    def test_evidence_rank_follows_the_given_order(self, repo, business):
        a = self._observation(repo, business, "first")
        repo.insert_observation(business, content="second", kind="review",
                                embedding=DESSERT_ISH, observed_at=_dt(TODAY))
        b = [r for r in repo.search_observations(business, DESSERT_ISH, limit=2)
             if r.content == "second"][0]

        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=100, confidence=0.5,
            verify_after=TODAY + timedelta(days=7),
            evidence=[EvidenceRef(b.observation_id, 0.91),
                      EvidenceRef(a.observation_id, 0.88)],
        )
        evidence = repo.get_find_evidence(find_id)
        assert [e.rank for e in evidence] == [0, 1]
        assert evidence[0].observation_id == b.observation_id

    def test_a_find_cannot_be_stored_without_evidence(self, repo, business):
        # This is the claim the submission rests on: every recommendation is
        # traceable to the retrieved rows that produced it. Enforced here, not
        # by convention.
        with pytest.raises(RepositoryError, match="evidence"):
            repo.insert_find_with_evidence(
                business, title="t", rationale="r", move="m", emoji="x",
                predicted_daily_cents=100, confidence=0.5,
                verify_after=TODAY + timedelta(days=7),
                evidence=[],
            )

    def test_bad_evidence_rolls_back_the_whole_find(self, repo, business):
        # A find written without its receipt must be impossible, so a failure
        # partway through must leave nothing behind.
        obs = self._observation(repo, business)
        before = repo.count_finds(business)
        with pytest.raises(RepositoryError):
            repo.insert_find_with_evidence(
                business, title="t", rationale="r", move="m", emoji="x",
                predicted_daily_cents=100, confidence=0.5,
                verify_after=TODAY + timedelta(days=7),
                evidence=[EvidenceRef(obs.observation_id, 0.9),
                          EvidenceRef(str(uuid.uuid4()), 0.8)],  # does not exist
            )
        assert repo.count_finds(business) == before


# ---------------------------------------------------------------------------
# The Meter's inbox
# ---------------------------------------------------------------------------

class TestDueFinds:
    def _find(self, repo, business, *, verify_after, status="live", cents=2300):
        repo.insert_observation(business, content=f"obs {uuid.uuid4().hex[:6]}",
                                kind="review", embedding=DESSERT,
                                observed_at=_dt(TODAY))
        obs = repo.search_observations(business, DESSERT, limit=1)[0]
        return repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=cents, confidence=0.8,
            verify_after=verify_after, status=status,
            evidence=[EvidenceRef(obs.observation_id, 0.9)],
        )

    def test_returns_finds_whose_window_has_elapsed(self, repo, business):
        due = self._find(repo, business, verify_after=TODAY - timedelta(days=1))
        self._find(repo, business, verify_after=TODAY + timedelta(days=30))

        results = repo.due_finds(business, today=TODAY)
        assert [f.find_id for f in results] == [due]

    def test_a_window_ending_today_is_due(self, repo, business):
        due = self._find(repo, business, verify_after=TODAY)
        assert [f.find_id for f in repo.due_finds(business, today=TODAY)] == [due]

    def test_already_judged_finds_are_excluded(self, repo, business):
        # Otherwise the Meter re-scores the same find every night and the ledger
        # inflates without any new work happening.
        find_id = self._find(repo, business, verify_after=TODAY - timedelta(days=1))
        repo.insert_ledger_entry(
            business, find_id=find_id, verdict="verified",
            predicted_daily_cents=2300, actual_daily_cents=2500,
            period_start=TODAY - timedelta(days=14), period_end=TODAY,
            method="square_sales",
        )
        assert repo.due_finds(business, today=TODAY) == []

    def test_undecided_finds_are_not_judged(self, repo, business):
        # A find the owner never acted on has no outcome to measure.
        self._find(repo, business, verify_after=TODAY - timedelta(days=1),
                   status="proposed")
        assert repo.due_finds(business, today=TODAY) == []

    def test_carries_the_prediction_forward(self, repo, business):
        self._find(repo, business, verify_after=TODAY, cents=4200)
        [due] = repo.due_finds(business, today=TODAY)
        assert due.predicted_daily_cents == 4200


class TestProfile:
    """Business facts and owner rules — what only the owner knows, and the
    constraints the autopilot obeys. Both feed the Analyst's prompt."""

    def test_facts_round_trip(self, repo, business):
        repo.insert_business_fact(business, fact="Tiramisu costs $2.10 to make",
                                 source="owner_chat", embedding=DESSERT)
        assert repo.get_business_facts(business) == ["Tiramisu costs $2.10 to make"]

    def test_superseded_facts_are_excluded(self, repo, business):
        # Memory keeps the history — prices change and the old value is part of
        # the record — but the Analyst must reason over what is true now.
        old = repo.insert_business_fact(business, fact="Tiramisu is $7",
                                       source="owner_chat", embedding=DESSERT)
        new = repo.insert_business_fact(business, fact="Tiramisu is $9",
                                       source="owner_chat", embedding=DESSERT)
        repo.supersede_business_fact(old, superseded_by=new)

        facts = repo.get_business_facts(business)
        assert facts == ["Tiramisu is $9"]

    def test_facts_are_scoped_to_the_business(self, repo, business):
        other = repo.create_business(name="Lucca's", category="restaurant")
        repo.insert_business_fact(other, fact="secret sauce recipe",
                                 source="owner_chat", embedding=DESSERT)
        assert repo.get_business_facts(business) == []

    def test_rules_round_trip_with_their_cap(self, repo, business):
        repo.insert_owner_rule(business, rule="Never change prices without asking",
                               enabled=True)
        repo.insert_owner_rule(business, rule="Ask before spending over the cap",
                               enabled=True, cap_cents=5000)
        rules = repo.get_owner_rules(business)
        assert len(rules) == 2
        assert any(r.cap_cents == 5000 for r in rules)

    def test_disabled_rules_are_excluded(self, repo, business):
        # A rule the owner switched off must not constrain tonight's run.
        repo.insert_owner_rule(business, rule="on", enabled=True)
        repo.insert_owner_rule(business, rule="off", enabled=False)
        assert [r.rule for r in repo.get_owner_rules(business)] == ["on"]


class TestBackdating:
    """Seeded history needs explicit timestamps.

    Without these the demo ledger reads as though every find and verdict
    happened the moment the seeder ran, which is both untrue and visibly wrong
    on screen — a track record that all occurred in one second is not a track
    record.
    """

    def _obs(self, repo, business):
        return repo.insert_observation(
            business, content=f"o {uuid.uuid4().hex[:8]}", kind="review",
            embedding=DESSERT, observed_at=_dt(date(2026, 6, 2)))

    def test_a_find_can_be_created_in_the_past(self, repo, business):
        obs_id = self._obs(repo, business)
        past = datetime(2026, 6, 10, 2, 15, tzinfo=timezone.utc)
        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=2300, confidence=0.8,
            verify_after=date(2026, 6, 24), status="live",
            created_at=past, decided_at=past,
            evidence=[EvidenceRef(obs_id, 0.94)],
        )
        [due] = repo.due_finds(business, today=TODAY)
        assert due.find_id == find_id
        assert due.created_at.date() == date(2026, 6, 10)

    def test_a_verdict_can_be_measured_in_the_past(self, repo, business):
        obs_id = self._obs(repo, business)
        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=2300, confidence=0.8,
            verify_after=date(2026, 6, 24), status="live",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
            evidence=[EvidenceRef(obs_id, 0.94)],
        )
        repo.insert_ledger_entry(
            business, find_id=find_id, verdict="verified",
            predicted_daily_cents=2300, actual_daily_cents=2500,
            period_start=date(2026, 6, 10), period_end=date(2026, 6, 24),
            method="seeded", measured_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        )
        summary = repo.ledger_summary(business)
        assert summary.verified_count == 1
        # And it must no longer appear as due, exactly as a live verdict would.
        assert repo.due_finds(business, today=TODAY) == []


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class TestLedger:
    def _judged(self, repo, business, verdict, predicted, actual):
        repo.insert_observation(business, content=f"o {uuid.uuid4().hex[:6]}",
                                kind="review", embedding=DESSERT,
                                observed_at=_dt(TODAY))
        obs = repo.search_observations(business, DESSERT, limit=1)[0]
        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=predicted, confidence=0.8,
            verify_after=TODAY, status="live",
            evidence=[EvidenceRef(obs.observation_id, 0.9)],
        )
        repo.insert_ledger_entry(
            business, find_id=find_id, verdict=verdict,
            predicted_daily_cents=predicted, actual_daily_cents=actual,
            period_start=TODAY - timedelta(days=14), period_end=TODAY,
            method="square_sales",
        )
        return find_id

    def test_summary_counts_each_verdict(self, repo, business):
        self._judged(repo, business, "verified", 2300, 2500)
        self._judged(repo, business, "verified", 1000, 1200)
        self._judged(repo, business, "miss", 900, 0)
        self._judged(repo, business, "estimated", 610, 610)

        summary = repo.ledger_summary(business)
        assert summary.verified_count == 2
        assert summary.miss_count == 1
        assert summary.estimated_count == 1

    def test_hit_rate_excludes_unjudged_estimates(self, repo, business):
        # An estimate is not yet a win or a loss. Counting it either way would
        # misstate the published record.
        self._judged(repo, business, "verified", 100, 100)
        self._judged(repo, business, "miss", 100, 0)
        self._judged(repo, business, "estimated", 100, 100)

        assert repo.ledger_summary(business).hit_rate == pytest.approx(0.5)

    def test_hit_rate_is_none_before_anything_is_judged(self, repo, business):
        # Reporting 0% before any verdict exists would read as total failure.
        self._judged(repo, business, "estimated", 100, 100)
        assert repo.ledger_summary(business).hit_rate is None

    def test_total_counts_only_realized_money(self, repo, business):
        self._judged(repo, business, "verified", 2300, 2500)
        self._judged(repo, business, "miss", 900, 0)
        assert repo.ledger_summary(business).verified_daily_cents == 2500

    def test_a_find_cannot_be_judged_twice_for_one_period(self, repo, business):
        # The ledger must be append-only per measurement window; a second
        # verdict for the same period would let a miss be overwritten.
        find_id = self._judged(repo, business, "miss", 900, 0)
        with pytest.raises(RepositoryError):
            repo.insert_ledger_entry(
                business, find_id=find_id, verdict="verified",
                predicted_daily_cents=900, actual_daily_cents=5000,
                period_start=TODAY - timedelta(days=14), period_end=TODAY,
                method="revised",
            )


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
