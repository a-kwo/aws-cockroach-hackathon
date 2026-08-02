"""The three agents that make up a night.

Radar observes, the Analyst reasons, the Meter judges. Every test here runs
against the in-memory repository and fake providers, so the whole nightly loop is
exercised with no cluster and no credentials.

The behaviours that matter most:

* Radar must not let one bad source abort a run. A search API outage should cost
  us that source's signals, not the night.
* The Analyst must issue several concrete queries. Measured against the seeded
  corpus, abstract strategic questions retrieve 2.5x worse and surface the wrong
  observations entirely (see CLAUDE.md). One "what should we do?" query is a
  design bug, not a tuning detail.
* The Meter must not invent outcome data. With nothing connected it records an
  ESTIMATE, never a verified win.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.agents.analyst import ANALYST_QUERIES, run_analyst
from brasstacks.agents.meter import run_meter
from brasstacks.agents.radar import run_radar
from brasstacks.outcomes import NoOutcomeSource, Outcome, RecordedOutcomeSource
from brasstacks.providers import FakeEmbedder, FakeReasoner, ModelRefusedError
from brasstacks.repository import EvidenceRef, InMemoryRepository
from brasstacks.signals import RawSignal

TODAY = date(2026, 7, 28)
NOW = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def business(repo):
    business_id = repo.create_business(name="Rosa's Trattoria", category="restaurant",
                                       city="Columbus", goal_monthly_cents=800000)
    repo.insert_business_fact(business_id, fact="Tiramisu is priced at $7.00",
                              source="owner_chat",
                              embedding=FakeEmbedder().embed(["x"])[0])
    repo.insert_owner_rule(business_id, rule="Never change prices without asking")
    return business_id


class StubSource:
    def __init__(self, name, signals=None, error=None):
        self.name = name
        self._signals = signals or []
        self._error = error
        self.calls = 0

    def fetch(self, *, business_name, city, limit):
        self.calls += 1
        if self._error:
            raise self._error
        return list(self._signals)


def signal(content, kind="review"):
    return RawSignal(content=content, kind=kind, source_name="review_site",
                     observed_at=NOW)


# ---------------------------------------------------------------------------
# Radar
# ---------------------------------------------------------------------------

class TestRadar:
    def test_stores_new_signals_and_reports_counts(self, repo, business):
        source = StubSource("reviews", [signal("great tiramisu"),
                                        signal("slow on Saturday")])
        result = run_radar(repo=repo, embedder=FakeEmbedder(),
                           business_id=business, sources=[source], now=NOW)

        assert result.observed == 2
        assert result.stored == 2
        assert result.duplicates == 0
        assert repo.count_observations(business) == 2

    def test_second_night_stores_nothing_new(self, repo, business):
        # Radar re-reads the same reviews every night. Night two must not double
        # the corpus.
        source = StubSource("reviews", [signal("great tiramisu")])
        run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business,
                  sources=[source], now=NOW)
        second = run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business,
                           sources=[source], now=NOW)

        assert second.observed == 1
        assert second.stored == 0
        assert second.duplicates == 1
        assert repo.count_observations(business) == 1

    def test_one_failing_source_does_not_abort_the_night(self, repo, business):
        # A search API outage should cost us that source's signals, not the run.
        good = StubSource("reviews", [signal("great tiramisu")])
        bad = StubSource("web", error=RuntimeError("503 from search API"))

        result = run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business,
                           sources=[bad, good], now=NOW)

        assert result.stored == 1
        assert result.failed_sources == ("web",)
        # The run is still 'ok' — partial observation is a normal night, not a
        # failure. The failure is recorded in the note for the audit trail.
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "ok"
        assert "web" in run.note

    def test_all_sources_failing_marks_the_run_failed(self, repo, business):
        # Observing nothing at all is a real failure and must be visible.
        bad = StubSource("web", error=RuntimeError("down"))
        result = run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business,
                           sources=[bad], now=NOW)

        assert result.stored == 0
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "failed"

    def test_no_signals_is_a_successful_quiet_night(self, repo, business):
        result = run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business,
                           sources=[StubSource("reviews", [])], now=NOW)
        assert result.stored == 0
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "ok"

    def test_blank_signals_are_dropped_before_embedding(self, repo, business):
        # Titan rejects empty input; one blank scraped row must not fail the run.
        embedder = FakeEmbedder()
        source = StubSource("reviews", [signal("real content"), signal("   ")])
        result = run_radar(repo=repo, embedder=embedder, business_id=business,
                           sources=[source], now=NOW)

        assert result.stored == 1
        assert embedder.embedded == ["real content"]

    def test_duplicates_within_one_batch_are_collapsed(self, repo, business):
        source = StubSource("reviews", [signal("same text"), signal("same text")])
        result = run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business,
                           sources=[source], now=NOW)
        assert result.stored == 1
        assert result.duplicates == 1


# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------

def find_payload(**overrides):
    base = {
        "emoji": "🍰",
        "title": "Tiramisu → $9",
        "rationale": "Reviews call it the best in the city and rivals charge more.",
        "move": "Reprice tiramisu to $9.",
        "predicted_daily_cents": 2300,
        "confidence": 0.85,
        "verify_after_days": 14,
        "evidence_observation_ids": [],
    }
    base.update(overrides)
    return base


class TestAnalyst:
    def _corpus(self, repo, business):
        embedder = FakeEmbedder()
        ids = []
        for text in ["tiramisu is the best in the city",
                     "waited an hour on Saturday",
                     "wish they did lunch"]:
            [vector] = embedder.embed([text])
            ids.append(repo.insert_observation(
                business, content=text, kind="review", embedding=vector,
                observed_at=NOW))
        return ids

    def test_issues_multiple_concrete_queries(self, repo, business):
        # The core retrieval finding: one abstract query retrieves the wrong
        # observations. Several concrete ones are an architectural requirement.
        assert len(ANALYST_QUERIES) >= 4
        embedder = FakeEmbedder()
        self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(evidence_observation_ids=[])])

        run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                    business_id=business, today=TODAY)

        # One embed call per query, plus none wasted.
        assert len(embedder.embedded) >= 4
        for query in ANALYST_QUERIES:
            assert query in embedder.embedded

    def test_writes_a_find_with_retrieved_evidence(self, repo, business):
        ids = self._corpus(repo, business)
        reasoner = FakeReasoner([
            find_payload(evidence_observation_ids=[ids[0], ids[1]])])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        assert result.find_id
        evidence = repo.get_find_evidence(result.find_id)
        assert [e.observation_id for e in evidence] == [ids[0], ids[1]]

    def test_evidence_carries_the_retrieval_similarity(self, repo, business):
        # find_evidence.similarity must be the real retrieval score, not a
        # placeholder — it is what the Evidence viewer displays.
        ids = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(evidence_observation_ids=[ids[0]])])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)
        [evidence] = repo.get_find_evidence(result.find_id)
        assert evidence.similarity != 0.0

    def test_the_prompt_carries_the_owners_facts_and_rules(self, repo, business):
        self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload()])
        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        prompt = reasoner.calls[0]["user"]
        assert "Tiramisu is priced at $7.00" in prompt
        assert "Never change prices without asking" in prompt

    def test_the_prompt_carries_retrieved_observations_with_ids(self, repo, business):
        # The model can only cite what it was shown, and it must cite by id.
        ids = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload()])
        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        prompt = reasoner.calls[0]["user"]
        assert any(observation_id in prompt for observation_id in ids)

    def test_observations_retrieved_by_several_queries_appear_once(self, repo, business):
        # The same review is often relevant to multiple hypotheses. Sending it
        # five times wastes context and skews the model toward it.
        embedder = FakeEmbedder()
        [vector] = embedder.embed(["tiramisu"])
        repo.insert_observation(business, content="tiramisu", kind="review",
                                embedding=vector, observed_at=NOW)
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        prompt = reasoner.calls[0]["user"]
        assert prompt.count("tiramisu") == 1

    def test_a_refusal_fails_the_run_without_writing_a_find(self, repo, business):
        self._corpus(repo, business)
        reasoner = FakeReasoner([ModelRefusedError("policy")])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        assert result.find_id is None
        assert result.error and "refus" in result.error.lower()
        assert repo.count_finds(business) == 0
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "failed"

    def test_an_invalid_find_fails_the_run_without_writing_anything(self, repo, business):
        # A model returning dollars where cents were asked for must not become a
        # 100x-wrong ledger entry.
        self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(predicted_daily_cents=23.5)])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        assert result.find_id is None
        assert repo.count_finds(business) == 0
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "failed"

    def test_a_hallucinated_citation_is_rejected(self, repo, business):
        self._corpus(repo, business)
        reasoner = FakeReasoner([
            find_payload(evidence_observation_ids=["not-a-real-observation"])])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        assert result.find_id is None
        assert repo.count_finds(business) == 0

    def test_empty_memory_produces_no_find_and_no_model_call(self, repo, business):
        # Nothing to reason over. Calling the model anyway would invite it to
        # invent a recommendation with no evidence.
        reasoner = FakeReasoner([])  # would raise if called

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        assert result.find_id is None
        assert result.retrieved == 0
        assert reasoner.calls == []

    def test_the_prompt_lists_what_was_already_proposed(self, repo, business):
        # Observed live: without this the Analyst proposed a waitlist find on
        # night 1 and again on night 3. It remembers the business's observations
        # but not its own recommendations.
        ids = self._corpus(repo, business)
        first = FakeReasoner([find_payload(evidence_observation_ids=[ids[0]])])
        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=first,
                    business_id=business, today=TODAY)

        second = FakeReasoner([find_payload(
            title="Something else", evidence_observation_ids=[ids[1]])])
        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=second,
                    business_id=business, today=TODAY)

        prompt = second.calls[0]["user"]
        assert "do not repeat" in prompt.lower()
        assert "Tiramisu → $9" in prompt

    def test_a_find_defaults_to_proposed_awaiting_the_owner(self, repo, business):
        # The owner holds the leash: a fresh find is a proposal, not a decision.
        ids = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(evidence_observation_ids=[ids[0]])])
        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        assert repo.due_finds(business, today=TODAY + timedelta(days=60)) == []
        assert result.find_id is not None


# ---------------------------------------------------------------------------
# Meter
# ---------------------------------------------------------------------------

class TestMeter:
    def _live_find(self, repo, business, *, predicted, days_ago=20, window=14):
        embedder = FakeEmbedder()
        [vector] = embedder.embed([f"obs for {predicted}"])
        observation_id = repo.insert_observation(
            business, content=f"obs for {predicted}", kind="review",
            embedding=vector, observed_at=NOW - timedelta(days=days_ago))
        created = NOW - timedelta(days=days_ago)
        return repo.insert_find_with_evidence(
            business, title=f"find {predicted}", rationale="r", move="m", emoji="x",
            predicted_daily_cents=predicted, confidence=0.8,
            verify_after=(NOW - timedelta(days=days_ago - window)).date(),
            status="live", created_at=created,
            evidence=[EvidenceRef(observation_id, 0.9)])

    def test_records_a_verified_win_when_the_outcome_beats_the_bar(self, repo, business):
        find_id = self._live_find(repo, business, predicted=2300)
        outcomes = RecordedOutcomeSource({
            find_id: Outcome(actual_daily_cents=2500, has_outcome_data=True,
                             method="owner-reported item sales")})

        result = run_meter(repo=repo, outcomes=outcomes, business_id=business,
                           today=TODAY)

        assert result.judged == 1
        assert result.verified == 1
        assert repo.ledger_summary(business).verified_count == 1

    def test_records_a_miss_when_nothing_materialised(self, repo, business):
        find_id = self._live_find(repo, business, predicted=1200)
        outcomes = RecordedOutcomeSource({
            find_id: Outcome(actual_daily_cents=0, has_outcome_data=True,
                             method="owner-reported item sales")})

        result = run_meter(repo=repo, outcomes=outcomes, business_id=business,
                           today=TODAY)

        assert result.misses == 1
        assert repo.ledger_summary(business).miss_count == 1

    def test_without_outcome_data_it_estimates_rather_than_claiming_a_win(
            self, repo, business):
        # The honest default. With nothing connected the Meter must never
        # manufacture a verified result.
        self._live_find(repo, business, predicted=2300)

        result = run_meter(repo=repo, outcomes=NoOutcomeSource(),
                           business_id=business, today=TODAY)

        assert result.estimated == 1
        assert result.verified == 0
        summary = repo.ledger_summary(business)
        assert summary.estimated_count == 1
        assert summary.hit_rate is None  # an estimate is not yet a win or a loss

    def test_a_judged_find_is_not_judged_again(self, repo, business):
        find_id = self._live_find(repo, business, predicted=2300)
        outcomes = RecordedOutcomeSource({
            find_id: Outcome(2500, True, "owner-reported")})

        run_meter(repo=repo, outcomes=outcomes, business_id=business, today=TODAY)
        second = run_meter(repo=repo, outcomes=outcomes, business_id=business,
                           today=TODAY)

        assert second.judged == 0
        assert repo.ledger_summary(business).verified_count == 1

    def test_a_find_still_inside_its_window_is_left_alone(self, repo, business):
        self._live_find(repo, business, predicted=2300, days_ago=2, window=14)
        result = run_meter(repo=repo, outcomes=NoOutcomeSource(),
                           business_id=business, today=TODAY)
        assert result.judged == 0

    def test_nothing_due_is_a_successful_quiet_run(self, repo, business):
        result = run_meter(repo=repo, outcomes=NoOutcomeSource(),
                           business_id=business, today=TODAY)
        assert result.judged == 0
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "ok"

    def test_the_predicted_value_is_snapshotted_onto_the_verdict(self, repo, business):
        # The ledger must stay honest even if the find is later edited: a miss
        # has to remain a miss against the number that was actually predicted.
        find_id = self._live_find(repo, business, predicted=2300)
        outcomes = RecordedOutcomeSource({find_id: Outcome(2500, True, "reported")})
        run_meter(repo=repo, outcomes=outcomes, business_id=business, today=TODAY)

        entries = repo._ledger  # in-memory introspection is fine in a unit test
        assert entries[0].predicted_daily_cents == 2300

    def test_one_unmeasurable_find_does_not_abort_the_others(self, repo, business):
        good = self._live_find(repo, business, predicted=2300)
        self._live_find(repo, business, predicted=900, days_ago=21)

        class Flaky:
            def measure(self, find, *, business_id):
                if find.find_id == good:
                    return Outcome(2500, True, "reported")
                raise RuntimeError("sales API timed out")

        result = run_meter(repo=repo, outcomes=Flaky(), business_id=business,
                           today=TODAY)

        assert result.verified == 1
        assert result.failed == 1
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "ok"  # partial measurement is not a failed night


# ---------------------------------------------------------------------------
# The night — the whole spine, in order
# ---------------------------------------------------------------------------

class TestRunNight:
    """The loop the product is: observe, reason, do, measure.

    Order is the thing being asserted here. Each agent depends on the state the
    previous one left, and a reordering would not fail any single-agent test.
    """

    FIND = {
        "emoji": "✍️",
        "title": "Reply to every recent low review",
        "rationale": "Unanswered reviews read as indifference.",
        "move": "I will draft a reply to each review from the last 30 days.",
        "predicted_daily_cents": 1500,
        "confidence": 0.55,
        "verify_after_days": 14,
        "evidence_observation_ids": [],
    }

    def _signals(self):
        return [StubSource([RawSignal(
            content="Nobody ever replies to our reviews.", kind="review",
            observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc))])]

    def test_runs_every_agent_in_order(self, repo, business):
        from brasstacks.artifacts import FakeArtifactStore
        from brasstacks.night import run_night

        # Seed the observation the find will cite. Radar re-observes the same
        # content during the night, so this also exercises dedup end to end.
        content = "Nobody ever replies to our reviews."
        observation_id = repo.insert_observation(
            business, content=content, kind="review",
            embedding=FakeEmbedder().embed([content])[0],
            observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc))

        find = dict(self.FIND, evidence_observation_ids=[observation_id])
        store = FakeArtifactStore()

        result = run_night(
            repo=repo, embedder=FakeEmbedder(),
            reasoner=FakeReasoner([find, {"title": "Draft replies",
                                          "body": "Thank you for telling us."}]),
            outcomes=NoOutcomeSource(), business_id=business, today=TODAY,
            sources=self._signals(), store=store, accept_proposals=True)

        agents = [r.agent for r in repo.recent_runs(business, limit=10)]
        assert agents == ["meter", "maker", "analyst", "radar"]  # newest first
        assert result.analyst.find_id
        assert result.maker.artifact_id
        assert store.puts, "the Maker should have written the draft"

    def test_the_maker_is_skipped_when_no_store_is_configured(self, repo, business):
        # S3 being unconfigured must cost us the drafts, not the night.
        from brasstacks.night import run_night

        result = run_night(
            repo=repo, embedder=FakeEmbedder(),
            reasoner=FakeReasoner([dict(self.FIND, evidence_observation_ids=[])]),
            outcomes=NoOutcomeSource(), business_id=business, today=TODAY,
            sources=self._signals(), store=None)

        assert result.maker is None
        assert "maker" not in [r.agent for r in repo.recent_runs(business, limit=10)]


def test_analyst_run_records_a_query_by_query_retrieval_receipt(repo, business):
    """Operators must be able to reconstruct how a recommendation was made.

    A single aggregate ("24 retrieved") is not enough: the receipt records how
    many rows each market question returned, how many raw matches were merged,
    how many unique observations entered the prompt, and how many were cited.
    """
    from brasstacks.analyst_trace import parse_analyst_trace

    ids = TestAnalyst()._corpus(repo, business)
    reasoner = FakeReasoner([
        find_payload(evidence_observation_ids=[ids[0], ids[1]])
    ])

    result = run_analyst(
        repo=repo,
        embedder=FakeEmbedder(),
        reasoner=reasoner,
        business_id=business,
        today=TODAY,
    )

    [run] = repo.recent_runs(business, limit=1)
    trace = parse_analyst_trace(run.note)

    assert trace is not None
    assert len(trace["query_hits"]) == len(ANALYST_QUERIES)
    assert trace["raw_hits"] == sum(trace["query_hits"])
    assert trace["unique_hits"] == result.retrieved
    assert trace["cited_hits"] == 2
    assert trace["find_id"] == result.find_id
    assert trace["queries"] == list(ANALYST_QUERIES)
    assert trace["per_query_limit"] == 6


class TestFindsSchemaIsAcceptedByTheProvider:
    """Schema keywords the API rejects, pinned.

    The Anthropic structured-output endpoint refuses `maxItems` on an array and
    fails the whole request with a 400 — which costs a night, since the Analyst
    only discovers it after retrieval has already run. The cap is enforced in
    code instead, where it cannot break a request.
    """

    UNSUPPORTED = ("maxItems", "minItems", "maxLength", "minLength",
                   "pattern", "format")

    def _walk(self, node, path="finds_schema"):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in self.UNSUPPORTED:
                    yield f"{path}.{key}"
                yield from self._walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from self._walk(value, f"{path}[{i}]")

    def test_the_schema_uses_no_keyword_the_api_rejects(self):
        from brasstacks.agents.analyst import FINDS_SCHEMA

        assert list(self._walk(FINDS_SCHEMA)) == []

    def test_the_cap_is_enforced_in_code_instead(self):
        from brasstacks.agents.analyst import MAX_FINDS_PER_NIGHT

        assert MAX_FINDS_PER_NIGHT == 3
