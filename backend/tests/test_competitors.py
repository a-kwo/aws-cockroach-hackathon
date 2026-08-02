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


#: Place Details returns up to five reviews. Only the text is of any use to the
#: Analyst; author attribution is personal data we have no reason to hold even
#: for the length of a prompt.
def details_body(place_id, *texts):
    return {"id": place_id,
            "reviews": [{"text": {"text": t, "languageCode": "en"},
                         "rating": 4,
                         "authorAttribution": {"displayName": "someone"}}
                        for t in texts]}


class StubHttp:
    def __init__(self, body=PLACES_BODY, fail=False, details=None,
                 details_fail=()):
        self._body, self._fail = body, fail
        self._details = details or {}
        self._details_fail = set(details_fail)
        self.calls: list[dict] = []
        self.detail_calls: list[str] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._fail:
            raise RuntimeError("places is down")
        return StubResponse(self._body)

    def get(self, url, **kwargs):
        place_id = url.rstrip("/").rsplit("/", 1)[-1]
        self.detail_calls.append(place_id)
        if place_id in self._details_fail:
            raise RuntimeError("details is down")
        return StubResponse(self._details.get(place_id, {"id": place_id}))


class StubResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def a_scout(**kwargs):
    http = StubHttp(**{k: v for k, v in kwargs.items()
                       if k in ("body", "fail", "details", "details_fail")})
    scout = PlacesCompetitorScout(
        api_key="places-key", client=http,
        latitude=39.96, longitude=-83.00, radius_m=1500,
        exclude_names=kwargs.get("exclude_names", ()),
        **{k: v for k, v in kwargs.items() if k == "detail_limit"})
    return scout, http


# --------------------------------------------------------------------------
# Place Details — the reviews, fetched live and never stored
# --------------------------------------------------------------------------

class TestReviewDetails:
    def test_reads_review_text_for_a_rival(self):
        scout, _ = a_scout(details={
            "ChIJ_lucca": details_body("ChIJ_lucca",
                                       "The lunch special is the best value downtown",
                                       "Waited forty minutes on a Saturday")})

        lucca = next(c for c in scout.scan(on=MONDAY) if c.name == "Lucca's")

        assert lucca.reviews == (
            "The lunch special is the best value downtown",
            "Waited forty minutes on a Saturday",
        )

    def test_details_field_mask_is_pinned(self):
        # Same billing trap as the nearby mask, and worse: `reviews` is already
        # the most expensive SKU, so an extra field here costs on every rival.
        from brasstacks.competitors import PLACES_DETAILS_FIELD_MASK

        scout, http = a_scout(details={"ChIJ_lucca": details_body("ChIJ_lucca", "x")})
        scout.scan(on=MONDAY)

        assert PLACES_DETAILS_FIELD_MASK == "id,reviews"

    def test_only_the_most_reviewed_rivals_cost_a_details_call(self):
        # One details call per rival per night would be the single most
        # expensive thing this system does. Ranked by review count because a
        # place with 812 reviews says more about the street than one with none.
        scout, http = a_scout(detail_limit=1)

        scout.scan(on=MONDAY)

        assert http.detail_calls == ["ChIJ_lucca"]

    def test_a_details_outage_costs_that_rival_its_reviews_and_nothing_more(self):
        scout, _ = a_scout(details_fail=("ChIJ_lucca",))

        rivals = scout.scan(on=MONDAY)

        assert [c.name for c in rivals] == ["Lucca's", "Brand New Cafe",
                                            "Rosa's Trattoria"]
        assert next(c for c in rivals if c.name == "Lucca's").reviews == ()

    def test_reviews_reach_the_prompt(self):
        text = describe_competitors([
            Competitor(place_id="a", name="Lucca's", rating=4.4,
                       reviews=("Waited forty minutes on a Saturday",))])

        assert "Waited forty minutes on a Saturday" in text

    def test_a_rival_with_no_reviews_renders_without_an_empty_heading(self):
        text = describe_competitors([Competitor(place_id="a", name="Lucca's")])

        assert "says:" not in text


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

    def test_forbids_quoting_a_rivals_review_verbatim(self):
        # find.rationale is stored forever. A model-written characterisation of
        # what rivals' customers say is derived analysis; a verbatim review in
        # that column is cached Places content, which the licence forbids. This
        # is the only route by which it could get there.
        text = describe_competitors([
            Competitor(place_id="a", name="X", reviews=("some review text",))])

        assert "do not quote" in text.lower()
        assert "verbatim" in text.lower()


class TestRadarIntegration:
    """The licence boundary, asserted where the fetch actually happens.

    Radar is the only agent that touches the outside world, so the scan lives
    here. That makes the boundary sharper, not looser: Radar's whole job is to
    write what it sees to memory, and this is the one thing it must see and not
    write.
    """

    def _repo(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        return repo, repo.create_business(name="Rosa's", category="restaurant")

    def _run(self, scout):
        from brasstacks.agents.radar import run_radar
        from brasstacks.providers import FakeEmbedder
        from brasstacks.signals import RawSignal

        class OneSource:
            name = "corpus"
            retention_hours = None

            def fetch(self, *, business_name, city, limit):
                return [RawSignal(content="a customer complained about the wait",
                                  kind="review", source_name="review_site")]

        repo, business_id = self._repo()
        result = run_radar(repo=repo, embedder=FakeEmbedder(),
                           business_id=business_id, sources=[OneSource()],
                           scout=scout, today=MONDAY)
        return repo, result

    def test_radar_collects_the_street(self):
        scout = FakeCompetitorScout([
            Competitor(place_id="a", name="Lucca's", rating=4.4)])

        _, result = self._run(scout)

        assert [c.name for c in result.competitors] == ["Lucca's"]

    def test_competitors_never_become_observations(self):
        # Google's terms permit storing place_id and nothing else. If a
        # competitor ever lands in the observation table, that is a licence
        # breach — and Radar is now the agent holding the loaded gun.
        scout = FakeCompetitorScout([
            Competitor(place_id="a", name="Lucca's", rating=4.4,
                       reviews=("their tiramisu is better",))])

        repo, _ = self._run(scout)

        stored = [o.content for o in repo._observations]
        assert len(stored) == 1, "only the signal source should have been stored"
        assert not any("Lucca's" in s for s in stored)
        assert not any("tiramisu is better" in s for s in stored)

    def test_a_places_outage_does_not_cost_the_night(self):
        repo, result = self._run(FakeCompetitorScout(RuntimeError("places is down")))

        assert result.competitors == ()
        assert result.stored == 1, "the observation should still have been written"

    def test_no_scout_configured_is_not_an_error(self):
        _, result = self._run(None)

        assert result.competitors == ()


class TestAnalystIntegration:
    """The Analyst is handed the street; it no longer goes and gets it."""

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

    def _run(self, repo, business_id, observation_id, competitors=()):
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.providers import FakeEmbedder, FakeReasoner

        reasoner = FakeReasoner([
            dict(self.FIND, evidence_observation_ids=[observation_id])])
        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business_id, today=MONDAY,
                    competitors=competitors)
        return reasoner

    def test_competitors_reach_the_prompt(self):
        repo, business_id, observation_id = self._repo_with_one_observation()

        reasoner = self._run(repo, business_id, observation_id, [
            Competitor(place_id="a", name="Lucca's", rating=4.4,
                       hours_today="11:30 AM – 10:00 PM")])

        assert "Lucca's" in reasoner.calls[0]["user"]

    def test_rival_reviews_reach_the_prompt(self):
        repo, business_id, observation_id = self._repo_with_one_observation()

        reasoner = self._run(repo, business_id, observation_id, [
            Competitor(place_id="a", name="Lucca's",
                       reviews=("their lunch special is unbeatable",))])

        assert "their lunch special is unbeatable" in reasoner.calls[0]["user"]

    def test_nothing_handed_over_changes_nothing(self):
        repo, business_id, observation_id = self._repo_with_one_observation()
        reasoner = self._run(repo, business_id, observation_id)

        assert "TODAY" not in reasoner.calls[0]["user"]


class TestBuildForABusiness:
    """Where the scout looks is a property of the tenant, not the deployment."""

    class Cfg:
        google_maps_api_key = "k"
        places_latitude = 39.9612
        places_longitude = -82.9988
        places_radius_m = 1500
        places_exclude_name = None

    def test_the_businesss_own_coordinates_win(self):
        from brasstacks.competitors import build_competitor_scout

        scout = build_competitor_scout(self.Cfg(), business={
            "name": "Nonna's", "latitude": 41.8781, "longitude": -87.6298})

        assert (scout._latitude, scout._longitude) == (41.8781, -87.6298)

    def test_falls_back_to_configuration_when_the_business_has_none(self):
        # A tenant seeded before signup existed has no coordinates. It should
        # keep working rather than silently scouting nobody.
        from brasstacks.competitors import build_competitor_scout

        scout = build_competitor_scout(self.Cfg(), business={"name": "Rosa's"})

        assert (scout._latitude, scout._longitude) == (39.9612, -82.9988)

    def test_the_business_excludes_itself_by_name(self):
        # Nearby Search returns the tenant's own restaurant. Feeding its own
        # ratings back as a rival's is quietly wrong, and for a real business
        # this is no longer hypothetical the way it was for a fictional one.
        from brasstacks.competitors import build_competitor_scout

        scout = build_competitor_scout(self.Cfg(), business={
            "name": "Nonna's", "latitude": 41.0, "longitude": -87.0})

        assert "nonna's" in scout._exclude

    def test_no_key_configured_is_still_none(self):
        from brasstacks.competitors import build_competitor_scout

        class NoKey(self.Cfg):
            google_maps_api_key = None

        assert build_competitor_scout(NoKey(), business={
            "name": "x", "latitude": 1.0, "longitude": 2.0}) is None


class TestFakeCompetitorScout:
    def test_returns_what_it_was_given(self):
        fake = FakeCompetitorScout([Competitor(place_id="a", name="Lucca's")])
        assert [c.name for c in fake.scan(on=MONDAY)] == ["Lucca's"]
        assert fake.scans == 1

    def test_can_simulate_an_outage(self):
        fake = FakeCompetitorScout(RuntimeError("down"))
        with pytest.raises(RuntimeError):
            fake.scan(on=MONDAY)
