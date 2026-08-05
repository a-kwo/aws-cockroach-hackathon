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


def _blend(embedder, query, *, step):
    """A vector near *query*'s embedding, one notch further away per step.

    FakeEmbedder is deliberately not semantic, so a test that needs a known
    retrieval order has to build one. Mixing a growing fraction of an unrelated
    vector into the query's own embedding walks similarity down a predictable
    line without any test pinning an exact score.
    """
    target, away = embedder.embed([query, "an entirely unrelated sentence"])
    weight = 0.05 * step
    return [(1 - weight) * a + weight * b for a, b in zip(target, away)]


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
        # Both cited rows are stored, and they come back ordered by retrieval
        # position. This read `== [ids[0], ids[1]]` — the order the model wrote
        # its citations in — which is exactly the meaning `rank` was carrying
        # while claiming to be position in the retrieved set.
        assert {e.observation_id for e in evidence} == {ids[0], ids[1]}
        assert [e.rank for e in evidence] == sorted(e.rank for e in evidence)

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

    def test_ask_memory_reaches_the_analyst_without_an_extra_embedding_call(self, repo, business):
        from brasstacks.analyst_trace import parse_analyst_trace

        self._corpus(repo, business)
        embedder = FakeEmbedder()
        vectors = embedder.embed(list(ANALYST_QUERIES))
        centroid = [
            sum(vector[index] for vector in vectors) / len(vectors)
            for index in range(len(vectors[0]))
        ]
        memory_id = repo.insert_chat_message(
            business, role="user",
            content="I cannot add weekend headcount; prefer prep changes.",
            created_at=NOW, embedding=centroid)
        # Ignore the vectors prepared for fixture setup; the run itself should
        # still embed exactly the six established market questions and reuse
        # their centroid for owner-memory retrieval.
        embedder.embedded.clear()
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                    business_id=business, today=TODAY)

        prompt = reasoner.calls[0]["user"]
        assert "I cannot add weekend headcount" in prompt
        assert len(embedder.embedded) == len(ANALYST_QUERIES)
        [run] = repo.recent_runs(business, limit=1)
        trace = parse_analyst_trace(run.note)
        assert memory_id in trace["owner_memory_ids"]

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

    def test_the_prompt_says_where_and_when_each_row_came_from(self, repo, business):
        # Every one of the 124 stored web observations reached the model as
        # source_name "web", with the time truncated to a date. So three
        # fragments of one Grubhub fetch, sharing a URL and an observed_at to
        # the microsecond, presented as three independent signals — and find
        # 7c4a9124 counted them as three.
        embedder = FakeEmbedder()
        [vector] = embedder.embed(["menu unavailable"])
        repo.insert_observation(
            business, content="This menu is not available right now",
            kind="trend", embedding=vector, observed_at=NOW, source_name="web",
            source_url="https://www.grubhub.com/restaurant/rosas/2033337")
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                    business_id=business, today=TODAY)

        prompt = reasoner.calls[0]["user"]
        assert "grubhub.com" in prompt
        assert "2026-07-28T02:00:00Z" in prompt

    def test_a_row_with_no_url_falls_back_to_the_source_that_stored_it(self, repo, business):
        # Yelp rows and owner uploads have a source but no URL. Showing nothing
        # for them would be a second kind of blindness.
        embedder = FakeEmbedder()
        [vector] = embedder.embed(["a slow saturday"])
        repo.insert_observation(
            business, content="waited an hour on Saturday", kind="review",
            embedding=vector, observed_at=NOW, source_name="yelp_fusion")
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                    business_id=business, today=TODAY)

        assert "yelp_fusion" in reasoner.calls[0]["user"]

    def test_one_page_cannot_take_over_the_prompt(self, repo, business):
        # Four of the five rows find 7c4a9124 cited were one storefront on one
        # platform. The cap runs after ranking, so the strongest two fragments
        # of a page survive and the rest make room for a different source.
        embedder = FakeEmbedder()
        storefront = "https://www.grubhub.com/restaurant/rosas/2033337"
        ids = [
            repo.insert_observation(
                business, content=f"storefront fragment {index}", kind="trend",
                embedding=_blend(embedder, ANALYST_QUERIES[0], step=index),
                observed_at=NOW, source_name="web", source_url=storefront)
            for index in range(3)
        ]
        elsewhere = repo.insert_observation(
            business, content="a rival raised its lunch price", kind="rival_price",
            embedding=_blend(embedder, ANALYST_QUERIES[0], step=5),
            observed_at=NOW, source_name="web",
            source_url="https://www.yelp.com/biz/luccas")
        reasoner = FakeReasoner([find_payload()])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        prompt = reasoner.calls[0]["user"]
        assert ids[0] in prompt and ids[1] in prompt
        assert ids[2] not in prompt
        assert elsewhere in prompt
        assert result.retrieved == 3

    def test_the_mirror_of_a_storefront_counts_against_the_same_cap(self, repo, business):
        # seamless.com/menu/…/2033337 is grubhub.com/restaurant/…/2033337. Two
        # rows from the pair are two rows from one storefront.
        embedder = FakeEmbedder()
        first = repo.insert_observation(
            business, content="grubhub says the menu is unavailable", kind="trend",
            embedding=_blend(embedder, ANALYST_QUERIES[0], step=0),
            observed_at=NOW, source_name="web",
            source_url="https://www.grubhub.com/restaurant/rosas/2033337")
        second = repo.insert_observation(
            business, content="seamless shows the same closed menu", kind="trend",
            embedding=_blend(embedder, ANALYST_QUERIES[0], step=1),
            observed_at=NOW, source_name="web",
            source_url="https://www.seamless.com/menu/rosas/2033337")
        third = repo.insert_observation(
            business, content="seamless lists no delivery window", kind="trend",
            embedding=_blend(embedder, ANALYST_QUERIES[0], step=2),
            observed_at=NOW, source_name="web",
            source_url="https://www.seamless.com/menu/rosas/2033337?tab=hours")
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        prompt = reasoner.calls[0]["user"]
        assert first in prompt and second in prompt
        assert third not in prompt

    def test_the_receipt_counts_what_the_source_cap_removed(self, repo, business):
        # The receipt is the honest half of the cap: a row that never reached
        # the model must be visible as removed, not quietly absent.
        from brasstacks.analyst_trace import parse_analyst_trace

        embedder = FakeEmbedder()
        storefront = "https://www.grubhub.com/restaurant/rosas/2033337"
        for index in range(4):
            repo.insert_observation(
                business, content=f"storefront fragment {index}", kind="trend",
                embedding=_blend(embedder, ANALYST_QUERIES[0], step=index),
                observed_at=NOW, source_name="web", source_url=storefront)
        reasoner = FakeReasoner([find_payload()])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        [run] = repo.recent_runs(business, limit=1)
        trace = parse_analyst_trace(run.note)
        assert trace["source_capped"] == 2
        assert trace["unique_hits"] == 2 == result.retrieved

    def test_a_corpus_with_no_urls_is_never_capped(self, repo, business):
        # The live seeded rows carry no URL. If a missing URL grouped them, the
        # cap would delete most of memory on the way to the prompt — this stage
        # must not suppress a find.
        from brasstacks.analyst_trace import parse_analyst_trace

        ids = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload()])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        [run] = repo.recent_runs(business, limit=1)
        assert parse_analyst_trace(run.note)["source_capped"] == 0
        assert result.retrieved == len(ids)

    def test_stored_rank_is_the_retrieval_position_not_the_citation_order(
        self, repo, business
    ):
        # find_evidence.rank claimed to be retrieval position and stored the
        # order the model happened to write its citations in — find 8b4009e5
        # has a 0.088 row at rank 0 and its 0.299 row at rank 4.
        embedder = FakeEmbedder()
        ids = [
            repo.insert_observation(
                business, content=f"row {index}", kind="review",
                embedding=_blend(embedder, ANALYST_QUERIES[0], step=index),
                observed_at=NOW)
            for index in range(3)
        ]
        # Cited weakest first, which is what the model did.
        reasoner = FakeReasoner([
            find_payload(evidence_observation_ids=[ids[2], ids[0]])])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        evidence = repo.get_find_evidence(result.find_id)
        assert [e.observation_id for e in evidence] == [ids[0], ids[2]]
        assert [e.rank for e in evidence] == [0, 2]

    def test_no_outage_claim_without_the_local_time_and_the_open_question(
        self, repo, business
    ):
        # The root cause of find 7c4a9124, one level up from the schedule: the
        # model was handed UTC stamps, no hours and no timezone, and read a
        # storefront captured at 08:07 local — an hour before opening — as
        # broken. It cannot check what it was never given.
        self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        system = reasoner.calls[0]["system"]
        assert "unavailable" in system and "broken" in system
        assert "whether the business was open at that instant" in system
        assert "do not make the claim" in system

        prompt = reasoner.calls[0]["user"]
        # The two things the check needs, and the honest note that neither is a
        # stored field: hours live in owner facts and in the rows, and no
        # business row carries a timezone.
        assert "UTC" in prompt
        assert "Columbus" in prompt                  # the only timezone clue there is
        assert "Opening hours are not a stored field" in prompt

    def test_the_prompt_forbids_calling_one_page_several_sources(self, repo, business):
        # Find 7c4a9124 cited four rows that were one storefront — three
        # fragments of a single fetch plus the same store on Seamless — and its
        # rationale called them corroborating platforms.
        self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        system = reasoner.calls[0]["system"]
        assert "one document read once" in system
        assert "independent confirmation" in system

    def test_the_prompt_forbids_a_specific_the_find_does_not_cite(self, repo, business):
        # A dish, a price or an opening hour taken from a retrieved row the
        # find does not cite is untraceable: find_evidence is the receipt, and
        # a detail outside it cannot be checked by anyone.
        self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        system = reasoner.calls[0]["system"]
        assert "must be carried by an observation you cite" in system

    def test_the_lead_find_must_name_the_alternative_it_rejects(self, repo, business):
        from brasstacks.agents.analyst import FIND_SCHEMA

        self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload()])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        system = reasoner.calls[0]["system"]
        assert "alternative_explanation" in system
        assert "strongest rival reading" in system
        assert "alternative_explanation" in FIND_SCHEMA["properties"]
        assert "alternative_explanation" in FIND_SCHEMA["required"]

    def test_the_alternative_the_model_wrote_is_stored_on_the_find(self, repo, business):
        ids = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(
            evidence_observation_ids=[ids[0]],
            alternative_explanation=(
                "The shop was shut when the page was fetched. Rejected: the "
                "same message appears in a 13:40 capture during service."
            ))])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        context = repo.get_find_context(business, result.find_id)
        assert context.alternative_explanation.startswith("The shop was shut")

    def test_a_night_without_an_alternative_still_produces_its_finds(self, repo, business):
        # This stage adds rules to the prompt and a column, not a gate. The
        # gates as originally designed withheld 9 of 9 finds, so a model that
        # ignores the field must still leave the owner with a deck.
        ids = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(evidence_observation_ids=[ids[0]])])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                             business_id=business, today=TODAY)

        assert result.find_id is not None
        assert repo.count_finds(business) == 1
        assert repo.get_find_context(business, result.find_id).alternative_explanation is None

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

    def test_an_estimated_verdict_stores_no_actual(self, repo, business):
        # The defect this class of test exists for: `NoOutcomeSource` returned
        # the prediction as the outcome, and the Meter wrote it into a column
        # called `actual`. The first find to come due would have had its own
        # forecast reported back to the owner as a measurement. Nothing
        # measured means nothing stored.
        self._live_find(repo, business, predicted=2300)

        run_meter(repo=repo, outcomes=NoOutcomeSource(), business_id=business,
                  today=TODAY)

        [entry] = repo._ledger
        assert entry.verdict == "estimated"
        assert entry.actual_daily_cents is None
        assert entry.predicted_daily_cents == 2300

    def test_a_verified_verdict_stores_the_measured_actual(self, repo, business):
        find_id = self._live_find(repo, business, predicted=2300)
        outcomes = RecordedOutcomeSource({
            find_id: Outcome(actual_daily_cents=2500, has_outcome_data=True,
                             method="owner-reported item sales")})

        run_meter(repo=repo, outcomes=outcomes, business_id=business, today=TODAY)

        [entry] = repo._ledger
        assert entry.actual_daily_cents == 2500

    def test_a_miss_keeps_the_prediction_and_records_the_real_zero(
            self, repo, business):
        # A measured zero is a fact, not an absence. Blanking it would make a
        # published miss indistinguishable from a find nobody has checked.
        find_id = self._live_find(repo, business, predicted=1200)
        outcomes = RecordedOutcomeSource({
            find_id: Outcome(actual_daily_cents=0, has_outcome_data=True,
                             method="owner-reported item sales")})

        run_meter(repo=repo, outcomes=outcomes, business_id=business, today=TODAY)

        [entry] = repo._ledger
        assert entry.verdict == "miss"
        assert entry.predicted_daily_cents == 1200
        assert entry.actual_daily_cents == 0

    def test_a_find_with_no_recorded_outcome_stores_no_actual(self, repo, business):
        # The realistic case once one owner has reported sales and another has
        # not: the unreported find falls through to an estimate and must not
        # pick up a number on the way.
        self._live_find(repo, business, predicted=2300)

        run_meter(repo=repo, outcomes=RecordedOutcomeSource({}),
                  business_id=business, today=TODAY)

        [entry] = repo._ledger
        assert entry.verdict == "estimated"
        assert entry.actual_daily_cents is None

    def test_a_source_that_disclaims_its_number_cannot_smuggle_it_into_actual(
            self, repo, business):
        # Mirrors the judge's rule that has_outcome_data=False beats a supplied
        # figure. The write has to obey it too, or the ledger records a number
        # the verdict says does not exist.
        find_id = self._live_find(repo, business, predicted=2300)

        class Overconfident:
            def measure(self, find, *, business_id):
                return Outcome(actual_daily_cents=9999, has_outcome_data=False,
                               method="guessed")

        run_meter(repo=repo, outcomes=Overconfident(), business_id=business,
                  today=TODAY)

        [entry] = repo._ledger
        assert entry.find_id == find_id
        assert entry.verdict == "estimated"
        assert entry.actual_daily_cents is None

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


def test_analyst_requests_a_structured_owner_feed_brief(repo, business):
    from brasstacks.agents.analyst import FIND_SCHEMA

    embedder = FakeEmbedder()
    ids = []
    for text in ("customers praise tiramisu", "rivals charge more for dessert"):
        [vector] = embedder.embed([text])
        ids.append(repo.insert_observation(
            business, content=text, kind="review", embedding=vector,
            observed_at=NOW,
        ))
    reasoner = FakeReasoner([find_payload(evidence_observation_ids=ids)])

    run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                business_id=business, today=TODAY)

    system = reasoner.calls[0]["system"]
    brief = FIND_SCHEMA["properties"]["feed_brief"]
    assert "feed_brief" in FIND_SCHEMA["required"]
    assert set(brief["properties"]) == {
        "effort", "category", "move_type", "price_point", "goal",
        "beneficiary", "success_signal", "tags",
    }
    assert "At a glance" in system
    assert "low`, `medium`, or `high" in system
    assert "must not introduce a fact" in system


def test_analyst_stores_the_structured_feed_brief(repo, business):
    embedder = FakeEmbedder()
    [vector] = embedder.embed(["customers praise tiramisu"])
    observation_id = repo.insert_observation(
        business, content="customers praise tiramisu", kind="review",
        embedding=vector, observed_at=NOW,
    )
    reasoner = FakeReasoner([find_payload(
        evidence_observation_ids=[observation_id],
        feed_brief={
            "effort": "low",
            "category": "Menu & offerings",
            "move_type": "Pricing improvement",
            "price_point": "$9 dessert",
            "goal": "Lift dessert margin",
            "beneficiary": "Dinner customers",
            "success_signal": "Higher dessert revenue per cover",
            "tags": ["Pricing", "Dessert"],
        },
    )])

    result = run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                         business_id=business, today=TODAY)
    context = repo.get_find_context(business, result.find_id)

    assert context.feed_brief["effort"] == "low"
    assert context.feed_brief["move_type"] == "Pricing improvement"
    assert context.feed_brief["tags"] == ["Pricing", "Dessert"]
