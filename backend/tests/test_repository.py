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
    OWNER_SEEN_STATUSES,
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


class TestReadingTheWholeCorpus:
    """`all_observations` — memory without a question attached.

    `business_state` needs this and retrieval cannot serve it. The three rows
    naming Yellow Cow's Dosirak set meal were in the corpus on the night the
    Analyst told the owner to launch a set meal, and no query returned them.
    """

    def test_it_returns_every_row_in_the_order_observed(self, repo, business):
        for day, content in enumerate(("first", "second", "third")):
            repo.insert_observation(
                business, content=content, kind="review", embedding=DESSERT,
                observed_at=_dt(TODAY) + timedelta(days=day))

        rows = repo.all_observations(business, limit=50)

        assert [r.content for r in rows] == ["first", "second", "third"]

    def test_it_carries_the_page_the_row_came_from(self, repo, business):
        # Hygiene, offer extraction and the domain flag all key off the host.
        repo.insert_observation(
            business, content="Featured items. Dosirak (Set Meal).",
            kind="trend", embedding=DESSERT, observed_at=_dt(TODAY),
            source_name="web", source_url="https://postmates.com/store/yc/Bq")

        [row] = repo.all_observations(business, limit=50)

        assert row.source_url == "https://postmates.com/store/yc/Bq"
        assert row.source_name == "web"
        assert row.kind == "trend"

    def test_it_is_scoped_to_the_business(self, repo, business):
        other = repo.create_business(name="Lucca's", category="restaurant")
        repo.insert_observation(business, content="mine", kind="review",
                                embedding=DESSERT, observed_at=_dt(TODAY))

        assert repo.all_observations(other, limit=50) == []

    def test_owner_conversation_is_not_an_observation(self, repo, business):
        # Chat shares the table. Reading the owner's own words back as market
        # memory would let the Analyst cite her question as evidence for its
        # answer — the same reason `count_observations` excludes them.
        repo.insert_chat_message(business, role="user", content="What is lunch set idea",
                                 created_at=_dt(TODAY), embedding=DESSERT)
        repo.insert_observation(business, content="a real row", kind="review",
                                embedding=DESSERT, observed_at=_dt(TODAY))

        assert [r.content for r in repo.all_observations(business, limit=50)] == [
            "a real row"]

    def test_the_limit_is_honoured(self, repo, business):
        for day in range(5):
            repo.insert_observation(
                business, content=f"row {day}", kind="review", embedding=DESSERT,
                observed_at=_dt(TODAY) + timedelta(days=day))

        assert len(repo.all_observations(business, limit=2)) == 2


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

    def test_retrieval_carries_the_url_the_row_came_from(self, repo, business):
        # Without this the Analyst cannot tell one page from another: every web
        # observation reaches the model as source_name "web". Find 7c4a9124
        # cited three fragments of one Grubhub fetch as three separate signals
        # because the URL stopped at insert_observation.
        repo.insert_observation(
            business, content="menu is not available right now", kind="trend",
            embedding=DESSERT, observed_at=_dt(TODAY), source_name="web",
            source_url="https://www.grubhub.com/restaurant/rosas/2033337")

        [hit] = repo.search_observations(business, DESSERT, limit=1)
        assert hit.source_url == "https://www.grubhub.com/restaurant/rosas/2033337"

    def test_a_row_stored_without_a_url_retrieves_without_one(self, repo, business):
        repo.insert_observation(business, content="hand seeded", kind="review",
                                embedding=DESSERT, observed_at=_dt(TODAY))
        [hit] = repo.search_observations(business, DESSERT, limit=1)
        assert hit.source_url is None

    def test_retrieval_carries_the_time_of_day_not_only_the_date(self, repo, business):
        # Three rows sharing an observed_at to the second are one fetch, not
        # three sightings. Truncating to a date is what let that read as three.
        moment = datetime(2026, 7, 28, 2, 14, 9, tzinfo=timezone.utc)
        repo.insert_observation(business, content="one fetch", kind="trend",
                                embedding=DESSERT, observed_at=moment)
        [hit] = repo.search_observations(business, DESSERT, limit=1)
        assert hit.observed_at.hour == 2
        assert hit.observed_at.minute == 14
        assert hit.observed_at.second == 9

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

    def test_rank_is_the_retrieval_position_when_one_is_given(self, repo, business):
        # `rank` claims, in the schema comment and on screen, to be position in
        # the retrieved set. It was the order the model happened to cite in:
        # find 8b4009e5 carries a 0.088 row at rank 0 and its 0.299 row at
        # rank 4. A judge checks exactly this, so the column now stores what it
        # says, and citation order is not preserved anywhere — it carries no
        # claim about retrieval.
        a = self._observation(repo, business, "first")
        repo.insert_observation(business, content="second", kind="review",
                                embedding=DESSERT_ISH, observed_at=_dt(TODAY))
        b = [r for r in repo.search_observations(business, DESSERT_ISH, limit=2)
             if r.content == "second"][0]

        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=100, confidence=0.5,
            verify_after=TODAY + timedelta(days=7),
            # Cited second-then-first, retrieved fourth-then-zeroth.
            evidence=[EvidenceRef(b.observation_id, 0.91, rank=4),
                      EvidenceRef(a.observation_id, 0.88, rank=0)],
        )

        evidence = repo.get_find_evidence(find_id)
        assert [e.rank for e in evidence] == [0, 4]
        assert [e.observation_id for e in evidence] == [a.observation_id,
                                                        b.observation_id]

    def test_the_rejected_alternative_is_stored_with_the_prediction(self, repo, business):
        # "The bot looked while we were shut" fits find 7c4a9124's evidence
        # better than "the storefront is broken", and the find recorded no sign
        # that the question was ever asked. The rival reading is now part of the
        # row, so a later reader can see what was considered and rejected.
        obs = self._observation(repo, business)
        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=100, confidence=0.5,
            verify_after=TODAY + timedelta(days=7),
            alternative_explanation=(
                "The page was captured at 08:07 local, before opening. "
                "Rejected: a 13:40 capture during service says the same."
            ),
            evidence=[EvidenceRef(obs.observation_id, 0.9)],
        )

        context = repo.get_find_context(business, find_id)
        assert context.alternative_explanation.startswith("The page was captured")

    def test_a_find_with_no_alternative_reads_back_as_none(self, repo, business):
        # Rules in the prompt, not a gate: a night that produced no alternative
        # still produces a find, and the column says nothing rather than "".
        obs = self._observation(repo, business)
        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=100, confidence=0.5,
            verify_after=TODAY + timedelta(days=7),
            evidence=[EvidenceRef(obs.observation_id, 0.9)],
        )

        assert repo.get_find_context(business, find_id).alternative_explanation is None

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


class TestRecentFinds:
    """The Analyst needs to see what it already proposed.

    Without this it re-proposes the same move every night — it remembers the
    business's observations but not its own recommendations, which is a strange
    gap in a product built on memory.
    """

    def _find(self, repo, business, title, *, status="proposed"):
        obs_id = repo.insert_observation(
            business, content=f"obs for {title}", kind="review",
            embedding=DESSERT, observed_at=_dt(TODAY))
        return repo.insert_find_with_evidence(
            business, title=title, rationale="r", move="m", emoji="x",
            predicted_daily_cents=1000, confidence=0.7,
            verify_after=TODAY + timedelta(days=14), status=status,
            evidence=[EvidenceRef(obs_id, 0.9)])

    def test_returns_recent_finds_newest_first(self, repo, business):
        self._find(repo, business, "older")
        self._find(repo, business, "newer")
        titles = [f.title for f in repo.recent_finds(business, limit=5)]
        assert titles == ["newer", "older"]

    def test_respects_the_limit(self, repo, business):
        for i in range(4):
            self._find(repo, business, f"find {i}")
        assert len(repo.recent_finds(business, limit=2)) == 2

    def test_includes_the_status_so_rejections_are_visible(self, repo, business):
        # A rejected find matters most: proposing it again would be the worst
        # possible repeat.
        self._find(repo, business, "rejected idea", status="rejected")
        [found] = repo.recent_finds(business, limit=5)
        assert found.status == "rejected"

    def test_scoped_to_the_business(self, repo, business):
        other = repo.create_business(name="Lucca's", category="restaurant")
        self._find(repo, other, "their idea")
        assert repo.recent_finds(business, limit=5) == []


class TestDecidingOnAFind:
    """The owner holds the leash. A find is a proposal until they act on it, and
    only an acted-on find is ever judged."""

    def _proposed(self, repo, business):
        obs_id = repo.insert_observation(
            business, content="tiramisu praise", kind="review",
            embedding=DESSERT, observed_at=_dt(TODAY))
        return repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=2300, confidence=0.8,
            verify_after=TODAY - timedelta(days=1), status="proposed",
            evidence=[EvidenceRef(obs_id, 0.9)])

    def test_a_proposed_find_is_never_judged(self, repo, business):
        self._proposed(repo, business)
        assert repo.due_finds(business, today=TODAY) == []

    def test_accepting_makes_it_judgeable(self, repo, business):
        find_id = self._proposed(repo, business)
        repo.set_find_status(find_id, status="accepted")
        assert [f.find_id for f in repo.due_finds(business, today=TODAY)] == [find_id]

    def test_deferring_to_the_later_jar_keeps_it_unjudged(self, repo, business):
        # "Save it for later" is not "do it", so there is no outcome to measure.
        find_id = self._proposed(repo, business)
        repo.set_find_status(find_id, status="later")
        assert repo.due_finds(business, today=TODAY) == []

    def test_rejecting_keeps_it_unjudged(self, repo, business):
        find_id = self._proposed(repo, business)
        repo.set_find_status(find_id, status="rejected")
        assert repo.due_finds(business, today=TODAY) == []

    def test_repeating_the_same_decision_is_idempotent(self, repo, business):
        find_id = self._proposed(repo, business)
        repo.set_find_status(find_id, status="accepted")
        repo.set_find_status(find_id, status="accepted")
        assert [f.find_id for f in repo.due_finds(business, today=TODAY)] == [find_id]

    def test_a_recorded_decision_cannot_be_rewritten(self, repo, business):
        find_id = self._proposed(repo, business)
        repo.set_find_status(find_id, status="accepted")
        with pytest.raises(RepositoryError, match="already decided"):
            repo.set_find_status(find_id, status="rejected")

    def test_a_pass_can_be_undone_to_do_it_with_both_timestamps_preserved(
        self, repo, business
    ):
        find_id = self._proposed(repo, business)
        passed_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        approved_at = datetime(2026, 8, 2, 13, 30, tzinfo=timezone.utc)

        repo.set_find_status(find_id, status="rejected", decided_at=passed_at)
        transition = repo.set_find_status(
            find_id, status="accepted", decided_at=approved_at)

        assert transition.previous_status == "rejected"
        assert transition.status == "accepted"
        assert transition.previous_decided_at == passed_at
        assert transition.decided_at == approved_at
        assert [f.find_id for f in repo.due_finds(
            business, today=TODAY)] == [find_id]

    def test_unknown_find_is_an_error(self, repo, business):
        with pytest.raises(RepositoryError):
            repo.set_find_status(str(uuid.uuid4()), status="accepted")


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

def _stored_actuals(repo, business_id):
    """Read `actual_daily_cents` straight out of whichever store is under test.

    The application reads ledger rows with the snapshot query in
    workflow_snapshot.py, not through Repository, so there is no interface
    method to assert against. Growing one for a test's sake would be inventing
    API; going to the store directly is what these two lines do instead.
    """
    ledger = getattr(repo, "_ledger", None)
    if ledger is not None:
        return [e.actual_daily_cents for e in ledger if e.business_id == business_id]
    with repo._conn.cursor() as cur:
        cur.execute(
            "SELECT actual_daily_cents FROM ledger_entry WHERE business_id = %s",
            (business_id,),
        )
        return [row[0] for row in cur.fetchall()]


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

    def test_an_unmeasured_verdict_round_trips_as_no_actual(self, repo, business):
        # `actual_daily_cents` was NOT NULL DEFAULT 0, so an estimate could only
        # be stored as a number — which is how the Meter ended up writing its
        # own prediction there. Both stores have to keep absence absent.
        self._judged(repo, business, "estimated", 610, None)
        assert _stored_actuals(repo, business) == [None]

    def test_a_measured_zero_is_kept_apart_from_no_measurement(self, repo, business):
        # These are different facts about the owner's money: one move earned
        # nothing, the other has never been checked.
        self._judged(repo, business, "miss", 900, 0)
        self._judged(repo, business, "estimated", 610, None)
        stored = _stored_actuals(repo, business)
        assert len(stored) == 2
        assert 0 in stored
        assert None in stored

    def test_verified_total_is_unmoved_by_rows_with_no_measurement(
            self, repo, business):
        self._judged(repo, business, "verified", 2300, 2500)
        self._judged(repo, business, "estimated", 4000, None)
        summary = repo.ledger_summary(business)
        assert summary.verified_daily_cents == 2500
        assert summary.estimated_count == 1

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


class TestPurgeObservations:
    """Licensed content has to be able to leave again.

    Yelp forbids retaining its content beyond 24 hours, which the rest of this
    schema is built to do exactly the opposite of. The purge is scoped to one
    source precisely so a licence term can never reach the corpus we own.
    """

    def _observe(self, repo, business, *, content, source_name, days_ago):
        return repo.insert_observation(
            business, content=content, kind="review", embedding=DESSERT,
            source_name=source_name,
            observed_at=_dt(TODAY - timedelta(days=days_ago)))

    def test_deletes_only_the_named_source_past_the_cutoff(self, repo, business):
        self._observe(repo, business, content="stale yelp", source_name="yelp", days_ago=3)
        self._observe(repo, business, content="fresh yelp", source_name="yelp", days_ago=0)
        self._observe(repo, business, content="our corpus", source_name="corpus", days_ago=400)

        removed = repo.purge_observations(
            business, source_name="yelp", older_than=_dt(TODAY - timedelta(days=1)))

        assert removed == 1
        assert repo.count_observations(business) == 2

    def test_returns_zero_when_nothing_qualifies(self, repo, business):
        self._observe(repo, business, content="fresh yelp", source_name="yelp", days_ago=0)
        assert repo.purge_observations(
            business, source_name="yelp",
            older_than=_dt(TODAY - timedelta(days=1))) == 0

    def test_does_not_reach_another_business(self, repo, business):
        other = repo.create_business(name=f"Other {uuid.uuid4().hex[:6]}",
                                     category="restaurant")
        self._observe(repo, other, content="their yelp", source_name="yelp", days_ago=9)

        assert repo.purge_observations(
            business, source_name="yelp",
            older_than=_dt(TODAY)) == 0
        assert repo.count_observations(other) == 1


class TestRecentFindsPriority:
    """What is running must never fall out of the Analyst's view.

    Found in production: after three weeks the window held twelve unacted-on
    proposals and hid every find that had actually been accepted — including six
    verified winners and the published miss. The Analyst promptly re-proposed a
    waitlist and a Tue–Thu set menu, both of which were already live and
    verified, because it could no longer see them.

    Recency is the wrong sort key. A proposal nobody acted on is weaker evidence
    of "already covered" than a move that has been earning for six weeks.
    """

    def _find(self, repo, business, *, title, status, day):
        observation_id = repo.insert_observation(
            business, content=f"note for {title}", kind="review",
            embedding=DESSERT, observed_at=_dt(TODAY - timedelta(days=60)))
        return repo.insert_find_with_evidence(
            business, title=title, rationale="r", move="m", emoji="x",
            predicted_daily_cents=1000, confidence=0.5,
            verify_after=TODAY + timedelta(days=14), status=status,
            created_at=_dt(TODAY - timedelta(days=day)),
            evidence=[EvidenceRef(observation_id, 0.4)])

    def test_live_finds_outrank_newer_proposals(self, repo, business):
        self._find(repo, business, title="old winner", status="live", day=60)
        for n in range(5):
            self._find(repo, business, title=f"new proposal {n}",
                       status="proposed", day=n)

        titles = [f.title for f in repo.recent_finds(business, limit=3)]

        assert "old winner" in titles, (
            "a live find six weeks old was crowded out by fresh proposals — "
            "this is exactly how the Analyst re-proposed its own verified moves"
        )

    def test_accepted_also_outranks_proposals(self, repo, business):
        self._find(repo, business, title="being drafted", status="accepted", day=90)
        for n in range(5):
            self._find(repo, business, title=f"noise {n}", status="proposed", day=n)

        assert "being drafted" in [
            f.title for f in repo.recent_finds(business, limit=3)]

    def test_newest_first_within_each_group(self, repo, business):
        self._find(repo, business, title="older live", status="live", day=30)
        self._find(repo, business, title="newer live", status="live", day=2)

        titles = [f.title for f in repo.recent_finds(business, limit=5)]
        assert titles.index("newer live") < titles.index("older live")

    def test_proposals_still_appear_when_there_is_room(self, repo, business):
        self._find(repo, business, title="running", status="live", day=40)
        self._find(repo, business, title="fresh idea", status="proposed", day=1)

        assert {"running", "fresh idea"} <= {
            f.title for f in repo.recent_finds(business, limit=10)}


#: The status the withholding gate will write. Deliberately not a constant in
#: `brasstacks`: the rule under test is an allowlist of the statuses the owner
#: reached, so this file can name a verdict the production code has never heard
#: of and still get the right answer.
NEVER_SHOWN_STATUS = "withheld"


class TestFindsTheOwnerNeverSaw:
    """"Already proposed" has to mean she saw it, not merely that a row exists.

    `recent_finds` feeds the Analyst's do-not-repeat list. Today every stored
    find has been on someone's board, so this class changes nothing. It exists
    for the night the quality gates start withholding: a find held back for a
    fixable reason — a capture taken while the shop was shut, a second source
    that was never found — must not come back tomorrow as an instruction never
    to raise it, or the corrected version can never be proposed.
    """

    def _find(self, repo, business, title, *, status):
        observation_id = repo.insert_observation(
            business, content=f"the note behind {title}", kind="trend",
            embedding=DESSERT, observed_at=_dt(TODAY))
        return repo.insert_find_with_evidence(
            business, title=title, rationale="r", move="m", emoji="x",
            predicted_daily_cents=1000, confidence=0.5,
            verify_after=TODAY + timedelta(days=14), status=status,
            evidence=[EvidenceRef(observation_id, 0.5)])

    def _never_shown(self, repo, business, title):
        """A find in a status the owner was never shown.

        The status itself belongs to the gates, which do not exist yet, so
        `find_status` has no such enum value and a real cluster cannot store
        one. The Postgres half of this contract skips rather than lying about
        that; the in-memory half runs every time, and it is the half standing
        between a withheld find and the do-not-repeat list.
        """
        try:
            return self._find(repo, business, title, status=NEVER_SHOWN_STATUS)
        except RepositoryError:
            pytest.skip(
                f"this cluster's find_status enum has no {NEVER_SHOWN_STATUS!r} "
                "value yet — it arrives with the withholding gates"
            )

    @pytest.mark.parametrize("status", OWNER_SEEN_STATUSES)
    def test_every_status_the_owner_can_reach_still_counts_as_proposed(
            self, repo, business, status):
        # Nothing stored today falls outside this list, which is why the change
        # is a no-op against the live corpus.
        self._find(repo, business, "on the board", status=status)

        [found] = repo.recent_finds(business, limit=5)
        assert found.status == status
        assert found.seen_by_owner is True

    def test_a_find_the_owner_never_saw_is_not_already_proposed(self, repo, business):
        self._never_shown(repo, business, "held back for a fixable reason")

        assert repo.recent_finds(business, limit=20) == []

    def test_it_is_still_retrievable_and_marked_as_never_shown(self, repo, business):
        # Withholding is not forgetting. The row stays queryable — it just
        # arrives labelled, because "we considered this and did not show it"
        # and "you already proposed this" want different sentences.
        self._never_shown(repo, business, "held back")

        [found] = repo.recent_finds(business, limit=20, include_unseen=True)
        assert found.title == "held back"
        assert found.seen_by_owner is False

    def test_never_shown_finds_sort_behind_the_board(self, repo, business):
        self._find(repo, business, "on the board", status="proposed")
        self._never_shown(repo, business, "held back")

        rows = repo.recent_finds(business, limit=1, include_unseen=True)
        assert [f.title for f in rows] == ["on the board"]

    def test_scoped_to_the_business(self, repo, business):
        other = repo.create_business(name=f"Lucca's {uuid.uuid4().hex[:6]}",
                                     category="restaurant")
        self._never_shown(repo, other, "their held-back idea")

        assert repo.recent_finds(business, limit=20, include_unseen=True) == []


# ---------------------------------------------------------------------------
# Owner conversation memory — durable, searchable, and tenant-scoped
# ---------------------------------------------------------------------------

class TestConversationMemory:
    def test_chat_round_trips_without_inflating_market_signal_counts(self, repo, business):
        moment = _dt(TODAY)
        owner_id = repo.insert_chat_message(
            business, role="user", content="Never discount the tasting menu",
            created_at=moment, embedding=DESSERT, find_id="find-1")
        assistant_id = repo.insert_chat_message(
            business, role="assistant", content="I will treat that as a guardrail.",
            created_at=moment, find_id="find-1")

        messages = repo.recent_chat_messages(business, limit=10, find_id="find-1")
        assert [message.message_id for message in messages] == [owner_id, assistant_id]
        assert [message.role for message in messages] == ["user", "assistant"]
        assert repo.count_chat_messages(business) == 2
        assert repo.count_observations(business) == 0

    def test_semantic_chat_retrieval_searches_owner_messages_only(self, repo, business):
        moment = _dt(TODAY)
        relevant_id = repo.insert_chat_message(
            business, role="user", content="Weekend staffing is capped at four people",
            created_at=moment, embedding=DESSERT)
        repo.insert_chat_message(
            business, role="assistant", content="Understood.",
            created_at=moment, embedding=DESSERT)
        repo.insert_chat_message(
            business, role="user", content="Parking is not a priority",
            created_at=moment, embedding=PARKING)

        matches = repo.search_chat_messages(business, DESSERT, limit=5)
        assert matches[0].message_id == relevant_id
        assert all(message.role == "user" for message in matches)

    def test_chat_memory_never_crosses_businesses(self, repo, business):
        other = repo.create_business(name="Other", category="restaurant")
        repo.insert_chat_message(
            other, role="user", content="Our private constraint",
            created_at=_dt(TODAY), embedding=DESSERT)

        assert repo.recent_chat_messages(business, limit=10) == []
        assert repo.search_chat_messages(business, DESSERT, limit=10) == []
        assert repo.count_chat_messages(business) == 0

    def test_find_context_is_tenant_scoped(self, repo, business):
        observation_id = repo.insert_observation(
            business, content="Guests ask for group packages", kind="review",
            embedding=DESSERT, observed_at=_dt(TODAY))
        find_id = repo.insert_find_with_evidence(
            business, title="Offer a group package", summary="Package the group meal.",
            rationale="Reviews show repeated group demand.",
            move="Set one package price. Publish it.", emoji="↗",
            predicted_daily_cents=2200, confidence=.72,
            verify_after=TODAY + timedelta(days=14),
            evidence=[EvidenceRef(observation_id, .88)])
        other = repo.create_business(name="Other", category="restaurant")

        context = repo.get_find_context(business, find_id)
        assert context is not None
        assert context.title == "Offer a group package"
        assert context.confidence == pytest.approx(.72)
        assert repo.get_find_context(other, find_id) is None


# ---------------------------------------------------------------------------
# Artifacts — the done-for-you deliverable, and where it lives
# ---------------------------------------------------------------------------

class TestArtifacts:
    def _a_find(self, repo, business) -> str:
        observation_id = repo.insert_observation(
            business, content="Slow replies to reviews", kind="review",
            embedding=DESSERT, observed_at=_dt(TODAY - timedelta(days=3)))
        return repo.insert_find_with_evidence(
            business, title="Reply to every recent low review",
            rationale="Unanswered reviews read as indifference.",
            move="I will draft a reply to each review from the last 30 days.",
            emoji="✍️", predicted_daily_cents=1500, confidence=0.6,
            verify_after=TODAY + timedelta(days=14), status="accepted",
            evidence=[EvidenceRef(observation_id, 0.51)])

    def test_stores_an_artifact_against_its_find(self, repo, business):
        find_id = self._a_find(repo, business)
        artifact_id = repo.insert_artifact(
            find_id=find_id, kind="review_reply",
            title="Draft replies for 4 reviews",
            preview="Thank you for telling us about the wait...",
            s3_bucket="brasstacks-artifacts", s3_key=f"{find_id}/replies.md")

        assert artifact_id
        stored = repo.get_artifacts(find_id)
        assert len(stored) == 1
        assert stored[0].kind == "review_reply"
        assert stored[0].title == "Draft replies for 4 reviews"
        assert stored[0].s3_key.endswith("replies.md")
        assert stored[0].preview.startswith("Thank you")

    def test_a_find_with_no_artifact_returns_empty(self, repo, business):
        find_id = self._a_find(repo, business)
        assert repo.get_artifacts(find_id) == []

    def test_artifacts_come_back_newest_first(self, repo, business):
        find_id = self._a_find(repo, business)
        repo.insert_artifact(find_id=find_id, kind="review_reply",
                             title="First pass")
        repo.insert_artifact(find_id=find_id, kind="review_reply",
                             title="Second pass")

        assert [a.title for a in repo.get_artifacts(find_id)] == [
            "Second pass", "First pass"]

    def test_an_artifact_must_belong_to_a_real_find(self, repo, business):
        # The artifact is the deliverable for a specific promise. One with no
        # find behind it is a document nobody asked for.
        with pytest.raises(RepositoryError):
            repo.insert_artifact(
                find_id=str(uuid.uuid4()), kind="review_reply", title="Orphan")

    def test_s3_location_is_optional(self, repo, business):
        # A preview alone is a legitimate state: the draft exists and is
        # readable even if the upload failed. Losing the row because S3 was
        # unavailable would be worse than recording it without a key.
        find_id = self._a_find(repo, business)
        repo.insert_artifact(find_id=find_id, kind="review_reply",
                             title="Local only", preview="Dear guest,")

        stored = repo.get_artifacts(find_id)[0]
        assert stored.s3_bucket is None
        assert stored.s3_key is None
        assert stored.preview == "Dear guest,"


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def test_owner_email_for_business_prefers_the_account_that_requested_the_task():
    from brasstacks.repository import InMemoryRepository

    repo = InMemoryRepository()
    business = repo.create_business(name="Three shops", category="retail")
    first = repo.create_account(business, username="first", password_hash="x")
    second = repo.create_account(business, username="second", password_hash="x")
    repo.update_account_profile(
        first, display_name="First", email="first@example.com"
    )
    repo.update_account_profile(
        second, display_name="Second", email="peter.flp.2006@gmail.com"
    )

    assert repo.owner_email_for_business(
        business, preferred_account_id=second
    ) == "peter.flp.2006@gmail.com"
    assert repo.owner_email_for_business(business) == "first@example.com"


def test_profile_fact_replacement_preserves_unrelated_owner_memory():
    from brasstacks.repository import InMemoryRepository

    repo = InMemoryRepository()
    business = repo.create_business(name="Rosa's", category="restaurant")
    chat_fact = repo.insert_business_fact(
        business,
        fact="The owner will not add weekend headcount.",
        source="owner_chat",
        embedding=[1.0] + [0.0] * 1023,
    )
    first = repo.replace_business_profile_facts(
        business,
        facts=["Rosa's is a restaurant in Columbus."],
        embeddings=[[1.0] + [0.0] * 1023],
        source="owner_chat",
    )
    second = repo.replace_business_profile_facts(
        business,
        facts=["Rosa's serves families in Columbus."],
        embeddings=[[0.0, 1.0] + [0.0] * 1022],
        source="owner_chat",
    )

    assert repo._facts[chat_fact]["superseded_by"] is None
    assert repo._facts[first[0]]["superseded_by"] == second[0]
    assert repo._facts[second[0]]["profile_managed"] is True


def test_find_context_round_trips_the_owner_feed_brief(repo, business):
    observation_id = repo.insert_observation(
        business, content="Customers want a fixed-price set", kind="review",
        embedding=DESSERT, observed_at=datetime.now(timezone.utc),
    )
    brief = {
        "effort": "medium",
        "category": "Menu & offerings",
        "move_type": "Product improvement",
        "price_point": "$24 fixed-price set",
        "goal": "Improve value perception",
        "beneficiary": "Takeout customers",
        "success_signal": "Higher reorder rate",
        "tags": ["Takeout", "Customer value"],
    }
    find_id = repo.insert_find_with_evidence(
        business, title="Add a fixed-price set", rationale="Customers asked.",
        move="Define and list one set.", emoji="🍱",
        predicted_daily_cents=1600, confidence=.72, verify_after=TODAY,
        evidence=[EvidenceRef(observation_id, .8)], feed_brief=brief,
    )

    context = repo.get_find_context(business, find_id)
    assert context.feed_brief == brief


# ---------------------------------------------------------------------------
# Outcomes the owner measured
# ---------------------------------------------------------------------------

class TestOwnerReportedOutcomes:
    """The only path by which a verdict can ever be anything but an estimate.

    NoOutcomeSource is the honest default and it can never produce a verified
    win. These rows are what an owner counted — item sales, covers, a POS
    export — and they are what the Meter judges against when the window closes.
    """

    def _find(self, repo, business, *, status="accepted", cents=2300,
              verify_after=None):
        repo.insert_observation(business, content=f"obs {uuid.uuid4().hex[:6]}",
                                kind="review", embedding=DESSERT,
                                observed_at=_dt(TODAY))
        obs = repo.search_observations(business, DESSERT, limit=1)[0]
        return repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=cents, confidence=0.8,
            verify_after=verify_after or TODAY, status=status,
            evidence=[EvidenceRef(obs.observation_id, 0.9)],
        )

    def test_a_reported_week_is_stored_with_its_daily_figure(self, repo, business):
        find_id = self._find(repo, business)
        report = repo.record_find_outcome(
            business, find_id=find_id, amount_cents=7000, basis="week",
            note="Counted from the till.",
        )

        assert report.amount_cents == 7000
        assert report.basis == "week"
        # Derived once, in the repository, so both stores agree on the number
        # the Meter will judge.
        assert report.daily_cents == 1000
        assert report.note == "Counted from the till."

    def test_the_latest_report_per_find_is_what_comes_back(self, repo, business):
        find_id = self._find(repo, business)
        repo.record_find_outcome(business, find_id=find_id,
                                 amount_cents=7000, basis="week")
        repo.record_find_outcome(business, find_id=find_id,
                                 amount_cents=14000, basis="week")

        [latest] = repo.find_outcome_reports(business)
        assert latest.amount_cents == 14000

    def test_a_correction_never_deletes_what_was_reported_before(
            self, repo, business):
        # Same rule the decision log keeps: an owner correcting a figure is a
        # new fact, not a licence to erase the old one.
        find_id = self._find(repo, business)
        repo.record_find_outcome(business, find_id=find_id,
                                 amount_cents=7000, basis="week")
        repo.record_find_outcome(business, find_id=find_id,
                                 amount_cents=14000, basis="week")

        history = repo.find_outcome_history(business, find_id=find_id)
        assert [r.amount_cents for r in history] == [14000, 7000]

    def test_a_measured_zero_is_kept(self, repo, business):
        # The published miss. Zero must survive as a number, not become absence.
        find_id = self._find(repo, business)
        report = repo.record_find_outcome(business, find_id=find_id,
                                          amount_cents=0, basis="week")
        assert report.amount_cents == 0
        assert report.daily_cents == 0
        assert [r.daily_cents for r in repo.find_outcome_reports(business)] == [0]

    def test_another_tenants_find_cannot_be_reported_on(self, repo, business):
        other = repo.create_business(name=f"Rival {uuid.uuid4().hex[:6]}",
                                     category="restaurant")
        find_id = self._find(repo, other)
        with pytest.raises(RepositoryError):
            repo.record_find_outcome(business, find_id=find_id,
                                     amount_cents=7000, basis="week")
        assert repo.find_outcome_reports(other) == []

    def test_a_find_the_owner_never_acted_on_has_no_outcome(self, repo, business):
        # Same rule as the Meter's inbox: nothing was done, so there is nothing
        # that could have earned anything.
        find_id = self._find(repo, business, status="proposed")
        with pytest.raises(RepositoryError):
            repo.record_find_outcome(business, find_id=find_id,
                                     amount_cents=7000, basis="week")

    def test_reporting_before_the_window_closes_is_allowed_and_waits(
            self, repo, business):
        # The owner may have the number early. It is stored, and it changes
        # nothing until the measurement window it belongs to has elapsed.
        find_id = self._find(repo, business,
                             verify_after=TODAY + timedelta(days=30))
        repo.record_find_outcome(business, find_id=find_id,
                                 amount_cents=7000, basis="week")

        assert len(repo.find_outcome_reports(business)) == 1
        assert repo.due_finds(business, today=TODAY) == []

    def test_an_unknown_period_is_refused(self, repo, business):
        find_id = self._find(repo, business)
        with pytest.raises(ValueError):
            repo.record_find_outcome(business, find_id=find_id,
                                     amount_cents=7000, basis="fortnight")


class TestRemeasuringAnEstimate:
    """An estimate is a placeholder. A measurement replaces it; a verdict that
    was itself measured is never rewritten."""

    def _estimated(self, repo, business, *, predicted=2300):
        repo.insert_observation(business, content=f"o {uuid.uuid4().hex[:6]}",
                                kind="review", embedding=DESSERT,
                                observed_at=_dt(TODAY))
        obs = repo.search_observations(business, DESSERT, limit=1)[0]
        find_id = repo.insert_find_with_evidence(
            business, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=predicted, confidence=0.8,
            verify_after=TODAY, status="accepted",
            evidence=[EvidenceRef(obs.observation_id, 0.9)],
        )
        repo.insert_ledger_entry(
            business, find_id=find_id, verdict="estimated",
            predicted_daily_cents=predicted, actual_daily_cents=None,
            period_start=TODAY - timedelta(days=14), period_end=TODAY,
            method="modelled from the prediction; no sales data connected",
            measured_at=_dt(TODAY),
        )
        return find_id

    def test_nothing_is_remeasurable_until_someone_reports_a_number(
            self, repo, business):
        self._estimated(repo, business)
        assert repo.remeasurable_finds(business) == []

    def test_an_estimate_with_a_newer_report_comes_back_for_judging(
            self, repo, business):
        find_id = self._estimated(repo, business)
        repo.record_find_outcome(business, find_id=find_id,
                                 amount_cents=7000, basis="week",
                                 reported_at=_dt(TODAY + timedelta(days=1)))

        [again] = repo.remeasurable_finds(business)
        assert again.find_id == find_id
        # The window has to be the one already on the ledger row, or the update
        # would miss it and a second verdict would be written for the same find.
        assert again.measurement_start == TODAY - timedelta(days=14)
        assert again.verify_after == TODAY

    def test_a_report_older_than_the_verdict_is_already_accounted_for(
            self, repo, business):
        # Otherwise every night re-judges every estimate that ever had a report,
        # forever.
        find_id = self._estimated(repo, business)
        repo.record_find_outcome(business, find_id=find_id,
                                 amount_cents=7000, basis="week",
                                 reported_at=_dt(TODAY - timedelta(days=1)))
        assert repo.remeasurable_finds(business) == []

    def test_an_estimate_becomes_the_measured_verdict(self, repo, business):
        find_id = self._estimated(repo, business)
        repo.update_ledger_entry(
            business, find_id=find_id,
            period_start=TODAY - timedelta(days=14), period_end=TODAY,
            verdict="verified", actual_daily_cents=1000,
            method="owner-reported sales, entered per week",
            measured_at=_dt(TODAY + timedelta(days=1)),
        )

        summary = repo.ledger_summary(business)
        assert summary.verified_count == 1
        assert summary.estimated_count == 0
        assert summary.verified_daily_cents == 1000

    def test_a_measured_verdict_is_never_rewritten(self, repo, business):
        # The append-only rule that matters: a published miss cannot be quietly
        # turned into a win by reporting a better number afterwards.
        find_id = self._estimated(repo, business)
        repo.update_ledger_entry(
            business, find_id=find_id,
            period_start=TODAY - timedelta(days=14), period_end=TODAY,
            verdict="miss", actual_daily_cents=0,
            method="owner-reported sales, entered per week",
        )
        with pytest.raises(RepositoryError):
            repo.update_ledger_entry(
                business, find_id=find_id,
                period_start=TODAY - timedelta(days=14), period_end=TODAY,
                verdict="verified", actual_daily_cents=9000,
                method="owner-reported sales, entered per week",
            )
        assert repo.ledger_summary(business).miss_count == 1

    def test_another_tenant_cannot_rewrite_this_ledger(self, repo, business):
        find_id = self._estimated(repo, business)
        other = repo.create_business(name=f"Rival {uuid.uuid4().hex[:6]}",
                                     category="restaurant")
        with pytest.raises(RepositoryError):
            repo.update_ledger_entry(
                other, find_id=find_id,
                period_start=TODAY - timedelta(days=14), period_end=TODAY,
                verdict="verified", actual_daily_cents=9000,
                method="owner-reported sales, entered per week",
            )
        assert repo.ledger_summary(business).estimated_count == 1
