"""Yelp as a signal source, and the retention rule that comes with it.

Yelp's API Terms of Use forbid storing their content for more than 24 hours.
That is in direct tension with this product's architecture, which is a permanent
accumulating memory — so the tension is resolved explicitly rather than ignored:
a source may declare a retention window, and Radar enforces it on every run.

The compliance behaviour is tested as hard as the parsing, because a quiet
failure here is a terms-of-service breach sitting in a public repository.

Two API facts shape the source and are worth stating where they will be read:

* **Three reviews, 160 characters each.** Yelp truncates, and picks which three
  by its own ranking. This is not a firehose and the corpus should not be
  designed as though it were.
* **`time_created` carries no timezone.** It is treated as UTC, which is
  approximate; nothing here depends on sub-day precision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brasstacks.agents.radar import run_radar
from brasstacks.providers import FakeEmbedder
from brasstacks.repository import InMemoryRepository
from brasstacks.signals import RawSignal, YelpSignalSource

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

SEARCH_BODY = {"businesses": [
    {"id": "rosas-trattoria-columbus", "name": "Rosa's Trattoria",
     "rating": 4.0, "review_count": 212,
     "url": "https://www.yelp.com/biz/rosas-trattoria-columbus"},
    {"id": "someone-else", "name": "Not Rosa's", "rating": 3.0},
]}

REVIEWS_BODY = {"reviews": [
    {"id": "r1", "rating": 2, "text": "Waited 90 minutes on a Saturday...",
     "time_created": "2026-07-20 19:31:05",
     "url": "https://www.yelp.com/biz/rosas-trattoria-columbus?hrid=r1"},
    {"id": "r2", "rating": 5, "text": "The tiramisu is worth the trip.",
     "time_created": "2026-07-18 21:02:44",
     "url": "https://www.yelp.com/biz/rosas-trattoria-columbus?hrid=r2"},
], "total": 2}


class StubHttp:
    """Stands in for httpx: routes by URL, records what was asked."""

    def __init__(self, search=SEARCH_BODY, reviews=REVIEWS_BODY, fail_on=None):
        self._search, self._reviews, self._fail_on = search, reviews, fail_on
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._fail_on and self._fail_on in url:
            raise RuntimeError("yelp is down")
        body = self._reviews if "/reviews" in url else self._search
        return StubResponse(body)


class StubResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def a_source(**kwargs):
    http = StubHttp(**{k: v for k, v in kwargs.items() if k in ("search", "reviews", "fail_on")})
    return YelpSignalSource(api_key="yelp-key", client=http, now=lambda: NOW), http


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class TestYelpSignalSource:
    def test_finds_the_business_then_fetches_its_reviews(self):
        source, http = a_source()
        signals = source.fetch(business_name="Rosa's Trattoria",
                               city="Columbus", limit=10)

        assert "businesses/search" in http.calls[0]["url"]
        assert "rosas-trattoria-columbus/reviews" in http.calls[1]["url"]
        assert [s.kind for s in signals] == ["review", "review"]

    def test_authenticates_with_a_bearer_token(self):
        source, http = a_source()
        source.fetch(business_name="Rosa's", city="Columbus", limit=10)

        assert http.calls[0]["headers"]["Authorization"] == "Bearer yelp-key"

    def test_carries_rating_url_and_timestamp_onto_the_signal(self):
        source, _ = a_source()
        first = source.fetch(business_name="Rosa's", city="Columbus", limit=10)[0]

        assert first.rating == 2.0
        assert first.source_name == "yelp"
        assert first.source_url.endswith("hrid=r1")
        assert first.observed_at == datetime(2026, 7, 20, 19, 31, 5, tzinfo=timezone.utc)
        assert "Waited 90 minutes" in first.content

    def test_takes_the_first_match_not_every_search_result(self):
        # The search returns nearby businesses too. Reviewing the wrong
        # restaurant is worse than reviewing none.
        source, http = a_source()
        source.fetch(business_name="Rosa's", city="Columbus", limit=10)

        review_calls = [c for c in http.calls if "/reviews" in c["url"]]
        assert len(review_calls) == 1

    def test_no_match_yields_no_signals_and_no_review_call(self):
        # The demo tenant is fictional, so this is the *expected* path for it,
        # not an error. It must not raise and must not invent anything.
        source, http = a_source(search={"businesses": []})
        assert source.fetch(business_name="Ghost Diner", city="Nowhere", limit=5) == []
        assert not [c for c in http.calls if "/reviews" in c["url"]]

    def test_blank_review_text_is_skipped(self):
        source, _ = a_source(reviews={"reviews": [
            {"id": "x", "rating": 4, "text": "   ", "time_created": "2026-07-20 10:00:00"}]})
        assert source.fetch(business_name="R", city="C", limit=5) == []

    def test_respects_the_limit(self):
        source, _ = a_source()
        assert len(source.fetch(business_name="R", city="C", limit=1)) == 1

    def test_a_yelp_outage_propagates_for_radar_to_absorb(self):
        # Radar already treats every source as best-effort; the source should
        # not swallow failures and silently report zero reviews.
        source, _ = a_source(fail_on="/reviews")
        with pytest.raises(RuntimeError):
            source.fetch(business_name="R", city="C", limit=5)


# --------------------------------------------------------------------------
# The retention rule — this is a compliance test, not a nicety
# --------------------------------------------------------------------------

class TestRetention:
    def test_the_source_declares_a_24_hour_window(self):
        source, _ = a_source()
        assert source.retention_hours == 24

    def test_the_corpus_source_declares_none(self):
        from brasstacks.signals import CorpusSignalSource
        assert getattr(CorpusSignalSource, "retention_hours", None) is None

    def _observation(self, repo, business_id, *, content, source_name, age_hours):
        return repo.insert_observation(
            business_id, content=content, kind="review",
            embedding=FakeEmbedder().embed([content])[0],
            source_name=source_name,
            observed_at=NOW - timedelta(hours=age_hours))

    def test_radar_purges_yelp_rows_past_the_window(self, ):
        repo = InMemoryRepository()
        business_id = repo.create_business(name="Rosa's", category="restaurant")
        self._observation(repo, business_id, content="old yelp review",
                          source_name="yelp", age_hours=30)
        self._observation(repo, business_id, content="fresh yelp review",
                          source_name="yelp", age_hours=2)

        source, _ = a_source(search={"businesses": []})
        run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business_id,
                  sources=[source], now=NOW, business_name="Rosa's")

        kept = [o.content for o in repo._observations]
        assert "old yelp review" not in kept, (
            "a Yelp observation older than 24h survived — this is a terms-of-"
            "service breach, not a stale-cache problem"
        )
        assert "fresh yelp review" in kept

    def test_purging_never_touches_other_sources(self):
        # The committed corpus is ours and is meant to accumulate forever.
        repo = InMemoryRepository()
        business_id = repo.create_business(name="Rosa's", category="restaurant")
        self._observation(repo, business_id, content="ancient corpus note",
                          source_name="corpus", age_hours=24 * 90)

        source, _ = a_source(search={"businesses": []})
        run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business_id,
                  sources=[source], now=NOW, business_name="Rosa's")

        assert "ancient corpus note" in [o.content for o in repo._observations]

    def test_no_purge_happens_without_a_retaining_source(self):
        # Radar must not delete Yelp rows on a night Yelp was not consulted —
        # that would be a surprising side effect of an unrelated run.
        repo = InMemoryRepository()
        business_id = repo.create_business(name="Rosa's", category="restaurant")
        self._observation(repo, business_id, content="old yelp review",
                          source_name="yelp", age_hours=30)

        class Quiet:
            name = "corpus"

            def fetch(self, **_):
                return [RawSignal(content="something", kind="trend")]

        run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business_id,
                  sources=[Quiet()], now=NOW, business_name="Rosa's")

        assert "old yelp review" in [o.content for o in repo._observations]

    def test_the_purge_is_reported_in_the_run_note(self):
        # The audit trail should show compliance happening, not imply it.
        repo = InMemoryRepository()
        business_id = repo.create_business(name="Rosa's", category="restaurant")
        self._observation(repo, business_id, content="old yelp review",
                          source_name="yelp", age_hours=30)

        source, _ = a_source(search={"businesses": []})
        result = run_radar(repo=repo, embedder=FakeEmbedder(),
                           business_id=business_id, sources=[source], now=NOW,
                           business_name="Rosa's")

        assert result.expired == 1
        assert "expired" in result.note
