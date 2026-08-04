"""Which retrieved rows are strong enough to enter the prompt.

Every number here was set by a measurement against the live corpus on
2026-08-03 — six live Titan embeddings of `ANALYST_QUERIES` per tenant, the
shipped retrieval SQL, the shipped per-source cap — and the measurement's whole
point was that the obvious thresholds are wrong:

* An **absolute** floor of 0.30 leaves Palsaik with ZERO retrieved rows (its top
  row scores 0.299) and Yellow Cow with three. Cosine similarity is orthogonal
  to verifiability: the Apple Maps "Claim This Place" row, the cheapest true win
  in the entire dataset, scores 0.147.
* 0.20 kills that Apple Maps row. 0.15 kills it on the corpus that is actually
  in the cluster today (0.147 < 0.150). 0.10 is the highest absolute floor that
  keeps every known-good marker with margin, and it costs only the tail — a
  driving-directions card, a rival ramen listing, a marketing bio.
* A **relative** bar of 0.50x also loses Apple Maps pre-hygiene, by 0.003. 0.40x
  keeps it by +0.027, and lands the bar at 0.118–0.139 on all three tenants.

So the rule is `similarity >= max(0.10, 0.40 * this tenant's top)`, with a
backstop that refuses to let the bar shrink the prompt below eight rows or four
distinct sources. A gate that quietly empties the prompt is how "unique context
sent" drops with no explanation, and an owner who opens the board to nothing
every morning churns — which is what an earlier design of these gates, measured,
actually did to 9 of 9 real finds.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brasstacks.agents.analyst import (
    ABSOLUTE_REJECT,
    ANALYST_QUERIES,
    MIN_PROMPT_ROWS,
    MIN_PROMPT_SOURCES,
    RELATIVE_ADMISSION,
    apply_similarity_floor,
    run_analyst,
)
from brasstacks.analyst_trace import parse_analyst_trace
from brasstacks.providers import FakeEmbedder, FakeReasoner
from brasstacks.repository import InMemoryRepository, Retrieved

NOW = datetime(2026, 8, 2, 15, 40, tzinfo=timezone.utc)
TODAY = NOW.date()


def row(similarity: float, *, url: str | None = None, ident: str = "") -> Retrieved:
    """One retrieved row at a chosen similarity.

    `ident` only exists to give two rows different URLs without the test having
    to invent plausible ones; when it is empty the row is unattributed, which is
    what every seeded row and every owner upload in the live corpus looks like.
    """
    return Retrieved(
        observation_id=f"obs-{ident or similarity}", content="a row",
        kind="trend", similarity=similarity, rank=0, observed_at=NOW,
        source_name="web", source_url=url,
    )


def ladder(*similarities: float) -> list[Retrieved]:
    """Rows in descending similarity, each from its own web source."""
    return [
        row(value, url=f"https://site{index}.example/page", ident=str(index))
        for index, value in enumerate(similarities)
    ]


class TestTheNumbersThemselves:
    def test_the_floor_is_the_measured_pair_not_a_round_number(self):
        # Pinned because each was chosen against a specific known-good row that
        # the next value up would have deleted: 0.15 loses Apple Maps (0.147),
        # and 0.50x loses it too (bar 0.150 on Palsaik pre-hygiene).
        assert ABSOLUTE_REJECT == 0.10
        assert RELATIVE_ADMISSION == 0.40
        assert MIN_PROMPT_ROWS == 8
        assert MIN_PROMPT_SOURCES == 4


class TestTheBar:
    def test_rows_under_forty_percent_of_the_tenants_top_are_removed(self):
        # Ten rows so the backstop is not the thing under test.
        rows = ladder(0.50, 0.45, 0.44, 0.43, 0.42, 0.41, 0.40, 0.30, 0.19, 0.15)

        decision = apply_similarity_floor(rows)

        assert decision.threshold == pytest.approx(0.20)
        assert [r.similarity for r in decision.removed] == [0.19, 0.15]
        assert decision.low_confidence is False

    def test_the_bar_moves_with_the_tenant_not_with_a_constant(self):
        # Palsaik's top row is 0.295 and Yellow Cow's is 0.335. A single
        # absolute bar cannot serve both; 0.40x of each can.
        palsaik = apply_similarity_floor(ladder(*[0.295 - 0.005 * i for i in range(12)]))
        yellow_cow = apply_similarity_floor(ladder(*[0.335 - 0.005 * i for i in range(12)]))

        assert palsaik.threshold == pytest.approx(0.118)
        assert yellow_cow.threshold == pytest.approx(0.134)

    def test_the_cheapest_true_win_in_the_dataset_survives(self):
        # maps.apple.com "Claim This Place" — verbatim, durable, checkable in
        # thirty seconds, and it scores 0.147 against a tenant top of 0.299.
        # Any rule that drops this row is not a quality gate, it is a product
        # that cannot recommend the one thing it is certain about.
        rows = ladder(0.299, 0.295, 0.286, 0.282, 0.277, 0.263, 0.260, 0.250,
                      0.147, 0.094)

        decision = apply_similarity_floor(rows)

        assert 0.147 in [r.similarity for r in decision.admitted]
        assert [r.similarity for r in decision.removed] == [0.094]

    def test_nothing_below_the_absolute_floor_rides_in_on_a_weak_tenant(self):
        # The absolute floor is the guard for a thin or brand-new tenant whose
        # top similarity collapses. At a top of 0.20 the relative bar would be
        # 0.08, and 0.08 is noise on this embedding model.
        rows = ladder(0.20, 0.19, 0.18, 0.17, 0.16, 0.15, 0.14, 0.13, 0.09, 0.05)

        decision = apply_similarity_floor(rows)

        assert decision.threshold == pytest.approx(ABSOLUTE_REJECT)
        assert [r.similarity for r in decision.removed] == [0.09, 0.05]


class TestTheBackstop:
    def test_it_refuses_to_take_the_prompt_below_eight_rows(self):
        # Measured worst case under the rule is Yellow Cow at 9 admitted rows,
        # so 8 is a backstop with teeth that does not fire on any live tenant.
        rows = ladder(0.50, 0.49, 0.48, 0.47, 0.11, 0.11, 0.11, 0.11, 0.11)

        decision = apply_similarity_floor(rows)

        assert len(decision.admitted) == MIN_PROMPT_ROWS
        assert decision.low_confidence is True

    def test_it_tops_up_in_similarity_order(self):
        rows = ladder(0.50, 0.49, 0.48, 0.47, 0.15, 0.14, 0.13, 0.12, 0.11)

        decision = apply_similarity_floor(rows)

        assert [r.similarity for r in decision.admitted] == [
            0.50, 0.49, 0.48, 0.47, 0.15, 0.14, 0.13, 0.12]
        assert [r.similarity for r in decision.removed] == [0.11]

    def test_it_tops_up_until_four_distinct_sources(self):
        # MAX_ROWS_PER_SOURCE is 2, so eight rows can be as few as four sources;
        # four is the tightest number consistent with min_rows = 8 rather than a
        # second, silently stricter row cap.
        storefront = "https://www.grubhub.com/restaurant/rosas/2033337"
        mirror = "https://www.seamless.com/menu/rosas/2033337"
        rows = [
            row(0.50, url=storefront, ident="a"),
            row(0.49, url=mirror, ident="b"),
            row(0.11, url="https://yelp.com/biz/rosas", ident="c"),
            row(0.10, url="https://tripadvisor.com/rosas", ident="d"),
            row(0.10, url="https://doordash.com/store/rosas", ident="e"),
        ]

        decision = apply_similarity_floor(rows, min_rows=2)

        # Grubhub and Seamless are one storefront, so the two strongest rows are
        # one source and the backstop has to reach three rows further down.
        assert len(decision.admitted) == 5
        assert decision.sources == MIN_PROMPT_SOURCES
        assert decision.low_confidence is True

    def test_the_floor_never_empties_the_prompt(self):
        # FakeEmbedder is not semantic and a fresh tenant's first sweep can miss
        # badly, so "every row is below the absolute floor" is a state that
        # happens. Withholding the whole night for it hands the owner an empty
        # board with no explanation; the honest answer is to send the strongest
        # rows and say the night was thin.
        rows = ladder(0.04, 0.03, 0.02)

        decision = apply_similarity_floor(rows)

        assert [r.similarity for r in decision.admitted] == [0.04, 0.03, 0.02]
        assert decision.removed == ()
        assert decision.low_confidence is True

    def test_an_empty_retrieval_stays_empty(self):
        decision = apply_similarity_floor([])

        assert decision.admitted == ()
        assert decision.low_confidence is True


class TestCountingSources:
    def test_a_row_with_no_url_is_its_own_source(self):
        # The seeded corpus and every owner upload carry no URL, and
        # source_identity returns None for them on purpose. Two choices were
        # available and neither falls out of a dict comprehension: collapse them
        # all into one source and the backstop fires forever for a URL-less
        # tenant, or count each separately. This counter answers "how broad is
        # the prompt", so each row counts — breadth is what it measures. The
        # opposite choice is made in finds.py for the `pattern` standard, where
        # the question is independent confirmation rather than breadth.
        rows = [row(0.5, ident=str(index)) for index in range(4)]

        assert apply_similarity_floor(rows).sources == 4


# ---------------------------------------------------------------------------
# End to end, through a real night
# ---------------------------------------------------------------------------

def _blend(embedder, query, *, step):
    """A vector near *query*'s embedding, one notch further away per step.

    Same device as test_agents: FakeEmbedder is deliberately not semantic, so a
    test that needs a known retrieval order has to build one. Step 15 lands at
    0.341 against a top of 1.0, which is the first step under the 0.40x bar.
    """
    target, away = embedder.embed([query, "an entirely unrelated sentence"])
    weight = 0.05 * step
    return [(1 - weight) * a + weight * b for a, b in zip(target, away)]


def find_payload(**overrides):
    base = {
        "emoji": "🍰",
        "title": "Tiramisu → $9",
        "claim_type": "opportunity",
        "rationale": "Reviews call it the best in the city and rivals charge more.",
        "move": "Reprice tiramisu to $9.",
        "predicted_daily_cents": 2300,
        "confidence": 0.85,
        "verify_after_days": 14,
        "evidence_observation_ids": [],
    }
    base.update(overrides)
    return base


class TestTheNightRecordsWhatTheBarDid:
    """A gate that silently shrinks the prompt is how "unique context sent"
    drops with no explanation anyone can find six weeks later."""

    @pytest.fixture
    def wide_corpus(self):
        repo = InMemoryRepository()
        business = repo.create_business(name="Rosa's", category="restaurant",
                                        city="Columbus")
        embedder = FakeEmbedder()
        ids = [
            repo.insert_observation(
                business, content=f"row {step}", kind="review",
                embedding=_blend(embedder, ANALYST_QUERIES[0], step=step),
                observed_at=NOW, source_name="web",
                source_url=f"https://site{step}.example/page")
            for step in range(16)
        ]
        return repo, business, ids

    def _run(self, repo, business):
        reasoner = FakeReasoner([find_payload()])
        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY, per_query_limit=20)
        return result, reasoner

    def test_the_weakest_row_never_reaches_the_model(self, wide_corpus):
        repo, business, ids = wide_corpus

        result, reasoner = self._run(repo, business)

        prompt = reasoner.calls[0]["user"]
        assert ids[14] in prompt
        assert ids[15] not in prompt
        assert result.retrieved == 15

    def test_the_receipt_says_how_many_the_bar_removed_and_where_it_sat(
        self, wide_corpus
    ):
        repo, business, _ids = wide_corpus

        self._run(repo, business)

        [run] = repo.recent_runs(business, limit=1)
        trace = parse_analyst_trace(run.note)
        assert trace["floor_removed"] == 1
        assert trace["floor_threshold"] == pytest.approx(0.40, abs=1e-3)
        assert trace["low_confidence"] is False
        assert trace["unique_hits"] == 15

    def test_a_thin_night_is_flagged_rather_than_hidden(self):
        # Three unrelated rows against six queries: FakeEmbedder scores them
        # near zero, so the whole retrieval sits under the absolute floor. The
        # night still runs, and the receipt is where that is admitted.
        repo = InMemoryRepository()
        business = repo.create_business(name="Rosa's", category="restaurant",
                                        city="Columbus")
        embedder = FakeEmbedder()
        for text in ["tiramisu is the best", "waited an hour", "wish they did lunch"]:
            [vector] = embedder.embed([text])
            repo.insert_observation(business, content=text, kind="review",
                                    embedding=vector, observed_at=NOW)
        result, _ = self._run(repo, business)

        [run] = repo.recent_runs(business, limit=1)
        trace = parse_analyst_trace(run.note)
        assert result.retrieved == 3
        assert trace["floor_removed"] == 0
        assert trace["low_confidence"] is True

    def test_rank_is_renumbered_after_the_bar(self, wide_corpus):
        # `find_evidence.rank` says, in the schema and on screen, that it is the
        # row's position in the retrieved set. A row the model never saw has no
        # retrieval position, so the numbering has to happen last.
        repo, business, ids = wide_corpus
        reasoner = FakeReasoner([find_payload(
            evidence_observation_ids=[ids[14], ids[0]])])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY, per_query_limit=20)

        evidence = repo.get_find_evidence(result.find_id)
        assert [e.rank for e in evidence] == [0, 14]
