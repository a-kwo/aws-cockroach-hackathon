"""Live competitor scouting via Google Places, and the line it must not cross.

This is deliberately **not** a SignalSource. Every other source Radar consults
becomes an observation — embedded, stored, retrieved months later. Google's
Places policy forbids exactly that: *"You must not pre-fetch, cache, or store
Places API content beyond the allowed exceptions,"* with `place_id` the only
exception.

So competitor state is fetched at reasoning time and handed to the Analyst in
its prompt, and nothing is written. That the path contains no repository call is
tested here, because a licence breach that only shows up in production is the
kind this project cannot afford.

It also happens to be the right shape on the merits: reviews are history and
want to accumulate; a rival's price is a fact about *now*, and a stale one is
worse than none.

Cost note — the field mask is load-bearing. Google bills Nearby Search by the
most expensive field requested, so asking for one unnecessary field silently
moves every call up an SKU. There is a test pinning it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from brasstacks.competitors import (
    PLACES_FIELD_MASK,
    Competitor,
    FakeCompetitorScout,
    PlacesCompetitorScout,
    describe_competitors,
)

MONDAY = date(2026, 7, 27)

PLACES_BODY = {"places": [
    {"id": "ChIJ_lucca", "displayName": {"text": "Lucca's"},
     "rating": 4.4, "userRatingCount": 812, "priceLevel": "PRICE_LEVEL_MODERATE",
     "primaryTypeDisplayName": {"text": "Italian restaurant"},
     "regularOpeningHours": {"weekdayDescriptions": [
         "Monday: 11:30 AM – 10:00 PM", "Tuesday: 11:30 AM – 10:00 PM",
         "Wednesday: Closed", "Thursday: 11:30 AM – 10:00 PM",
         "Friday: 11:30 AM – 11:00 PM", "Saturday: 5:00 – 11:00 PM",
         "Sunday: Closed"]}},
    {"id": "ChIJ_newplace", "displayName": {"text": "Brand New Cafe"},
     "primaryTypeDisplayName": {"text": "Cafe"}},
    {"id": "ChIJ_rosas", "displayName": {"text": "Rosa's Trattoria"},
     "rating": 4.1, "userRatingCount": 212},
]}


class StubHttp:
    def __init__(self, body=PLACES_BODY, fail=False):
        self._body, self._fail = body, fail
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._fail:
            raise RuntimeError("places is down")
        return StubResponse(self._body)


class StubResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def a_scout(**kwargs):
    http = StubHttp(**{k: v for k, v in kwargs.items() if k in ("body", "fail")})
    scout = PlacesCompetitorScout(
        api_key="places-key", client=http,
        latitude=39.96, longitude=-83.00, radius_m=1500,
        exclude_names=kwargs.get("exclude_names", ()))
    return scout, http


# --------------------------------------------------------------------------
# The request — the field mask is a billing decision
# --------------------------------------------------------------------------

class TestRequestShape:
    def test_asks_only_for_the_fields_we_use(self):
        # Google bills at the highest SKU any requested field belongs to. An
        # extra field here is a silent price rise on every call, forever.
        assert PLACES_FIELD_MASK == ",".join([
            "places.id",
            "places.displayName",
            "places.rating",
            "places.userRatingCount",
            "places.priceLevel",
            "places.primaryTypeDisplayName",
            "places.regularOpeningHours.weekdayDescriptions",
        ])

    def test_sends_the_key_and_mask_as_headers(self):
        scout, http = a_scout()
        scout.scan(on=MONDAY)

        headers = http.calls[0]["headers"]
        assert headers["X-Goog-Api-Key"] == "places-key"
        assert headers["X-Goog-FieldMask"] == PLACES_FIELD_MASK

    def test_restricts_to_restaurants_within_the_radius(self):
        scout, http = a_scout()
        scout.scan(on=MONDAY)

        body = http.calls[0]["json"]
        assert body["includedTypes"] == ["restaurant"]
        circle = body["locationRestriction"]["circle"]
        assert circle["center"] == {"latitude": 39.96, "longitude": -83.00}
        assert circle["radius"] == 1500.0
        assert body["maxResultCount"] == 20


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

class TestParsing:
    def test_reads_rating_count_and_type(self):
        scout, _ = a_scout()
        lucca = scout.scan(on=MONDAY)[0]

        assert lucca.name == "Lucca's"
        assert lucca.place_id == "ChIJ_lucca"
        assert lucca.rating == 4.4
        assert lucca.rating_count == 812
        assert lucca.kind == "Italian restaurant"

    def test_maps_the_price_enum_to_something_readable(self):
        scout, _ = a_scout()
        assert scout.scan(on=MONDAY)[0].price_level == "$$"

    def test_picks_out_todays_hours_only(self):
        # Twenty competitors times seven days is a lot of prompt for no gain;
        # the Analyst is reasoning about tonight.
        scout, _ = a_scout()
        assert scout.scan(on=MONDAY)[0].hours_today == "11:30 AM – 10:00 PM"

    def test_a_business_with_no_rating_yet_is_still_returned(self):
        # A brand-new competitor with no reviews is a signal, not a gap.
        scout, _ = a_scout()
        new = scout.scan(on=MONDAY)[1]

        assert new.name == "Brand New Cafe"
        assert new.rating is None
        assert new.price_level is None
        assert new.hours_today is None

    def test_excludes_the_owners_own_business(self):
        # Nearby Search returns her too. Feeding her own ratings back as a
        # "competitor" would be quietly wrong.
        scout, _ = a_scout(exclude_names=("Rosa's Trattoria",))
        assert "Rosa's Trattoria" not in [c.name for c in scout.scan(on=MONDAY)]

    def test_exclusion_ignores_case_and_padding(self):
        scout, _ = a_scout(exclude_names=("  rosa's trattoria ",))
        assert len(scout.scan(on=MONDAY)) == 2


class TestFailure:
    def test_an_outage_raises_for_the_caller_to_absorb(self):
        scout, _ = a_scout(fail=True)
        with pytest.raises(RuntimeError):
            scout.scan(on=MONDAY)


# --------------------------------------------------------------------------
# Rendering into the prompt
# --------------------------------------------------------------------------

class TestDescribeCompetitors:
    def test_renders_one_compact_line_each(self):
        text = describe_competitors([
            Competitor(place_id="a", name="Lucca's", rating=4.4, rating_count=812,
                       price_level="$$", kind="Italian restaurant",
                       hours_today="11:30 AM – 10:00 PM"),
        ])
        assert "Lucca's" in text
        assert "4.4" in text and "812" in text
        assert "$$" in text
        assert "11:30 AM" in text

    def test_says_so_when_a_rating_is_missing(self):
        text = describe_competitors([
            Competitor(place_id="b", name="Brand New Cafe", kind="Cafe")])
        assert "Brand New Cafe" in text
        assert "no rating" in text.lower()

    def test_empty_scan_renders_nothing(self):
        assert describe_competitors([]) == ""

    def test_states_that_this_is_today_not_history(self):
        # The Analyst must not describe a single snapshot as a trend. It has no
        # history here and the prompt has to say so.
        text = describe_competitors([Competitor(place_id="a", name="X")])
        assert "today" in text.lower()


class TestAnalystIntegration:
    """The licence boundary, asserted where it can actually break."""

    FIND = {
        "emoji": "x", "title": "t", "rationale": "r", "move": "m",
        "predicted_daily_cents": 1000, "confidence": 0.5,
        "verify_after_days": 14, "evidence_observation_ids": [],
    }

    def _repo_with_one_observation(self):
        from brasstacks.providers import FakeEmbedder
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        business_id = repo.create_business(name="Rosa's", category="restaurant")
        observation_id = repo.insert_observation(
            business_id, content="a customer complained about the wait",
            kind="review", embedding=FakeEmbedder().embed(["x"])[0],
            observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc))
        return repo, business_id, observation_id

    def _run(self, repo, business_id, observation_id, scout):
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.providers import FakeEmbedder, FakeReasoner

        reasoner = FakeReasoner([
            dict(self.FIND, evidence_observation_ids=[observation_id])])
        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business_id, today=MONDAY, scout=scout)
        return reasoner

    def test_competitors_reach_the_prompt(self):
        repo, business_id, observation_id = self._repo_with_one_observation()
        scout = FakeCompetitorScout([
            Competitor(place_id="a", name="Lucca's", rating=4.4,
                       hours_today="11:30 AM – 10:00 PM")])

        reasoner = self._run(repo, business_id, observation_id, scout)

        assert "Lucca's" in reasoner.calls[0]["user"]

    def test_competitors_never_become_observations(self):
        # The whole reason this is not a SignalSource. Google's terms permit
        # storing place_id and nothing else; if a competitor ever lands in the
        # observation table, that is a licence breach.
        repo, business_id, _ = self._repo_with_one_observation()
        before = len(repo._observations)
        scout = FakeCompetitorScout([
            Competitor(place_id="a", name="Lucca's", rating=4.4)])

        self._run(repo, business_id, repo._observations[0].observation_id, scout)

        assert len(repo._observations) == before
        assert "Lucca's" not in [o.content for o in repo._observations]

    def test_a_places_outage_does_not_cost_the_night(self):
        repo, business_id, observation_id = self._repo_with_one_observation()
        scout = FakeCompetitorScout(RuntimeError("places is down"))

        reasoner = self._run(repo, business_id, observation_id, scout)

        assert reasoner.calls, "the Analyst should still have reasoned"
        assert len(repo._finds) == 1

    def test_no_scout_configured_changes_nothing(self):
        repo, business_id, observation_id = self._repo_with_one_observation()
        reasoner = self._run(repo, business_id, observation_id, None)

        assert "TODAY" not in reasoner.calls[0]["user"]


class TestFakeCompetitorScout:
    def test_returns_what_it_was_given(self):
        fake = FakeCompetitorScout([Competitor(place_id="a", name="Lucca's")])
        assert [c.name for c in fake.scan(on=MONDAY)] == ["Lucca's"]
        assert fake.scans == 1

    def test_can_simulate_an_outage(self):
        fake = FakeCompetitorScout(RuntimeError("down"))
        with pytest.raises(RuntimeError):
            fake.scan(on=MONDAY)
