"""Tavily as a signal source — the only live source a real tenant has.

It had no test at all until this file, which is why the three defects below
survived into production and put 124 rows of aggregator boilerplate into the
memory layer of three paying-attention businesses.

Everything asserted here was measured against the live API on 2026-08-07, for
Yellow Cow Korean BBQ in Gardena. The numbers are not invented:

**The street address was being used as the search locality.** ``business.city``
holds the whole address for anyone who signed up through the deployed flow, so
the "local dining trends" query ran as *"1835 W Redondo Beach Blvd, Gardena, CA
90247 restaurant dining trends"*. That pins the search to one building: the top
three results were MapQuest, Yelp and TripAdvisor directory pages **about the
tenant itself**, scoring 0.752, 0.701 and 0.656. Re-run against the locality
alone, the same slot returns rival KBBQ houses with review counts and prices.

**Every row was written as kind='trend'.** The reviews query works well — it
returned real customer sentiment at 0.515–0.726 — but the rows landed in
CockroachDB labelled a trend, with no rating, so nothing downstream could tell
a diner's complaint from a market report. `observation_kind` has had a 'review'
value the whole time.

**Tavily's own relevance score was discarded.** It is in every result and it
separates the corpus cleanly: the useful rows scored 0.38–0.85 and the junk
(chrome, a Houston closures listicle, a condo listing) scored 0.04–0.23.
Keeping the number and applying a floor is the cheapest quality win available.

One thing deliberately *not* tested here: stripping navigation chrome. Grubhub's
"Skip to NavigationSkip to About" scores 0.539 and no floor will catch it. That
is `clean_observation_text`'s job in the ingest-hygiene layer, and it already
does it — see test_ingest_hygiene.py. Two layers, one responsibility each.
"""

from __future__ import annotations

import pytest

from brasstacks.signals import (
    MAX_TAVILY_RESULTS,
    RawSignal,
    TavilyQuery,
    TavilySignalSource,
    build_query_plan,
    locality_of,
    offering_from_facts,
)


# ---------------------------------------------------------------------------
# Stub transport
# ---------------------------------------------------------------------------


class StubResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class StubHttp:
    """Stands in for httpx. Records every request; can fail chosen queries."""

    def __init__(self, results=None, per_query=None, fail_queries=()):
        self._results = results if results is not None else []
        self._per_query = per_query or {}
        self._fail = set(fail_queries)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        self.calls.append({"url": url, **kwargs})
        query = body.get("query", "")
        if query in self._fail:
            raise RuntimeError("tavily rate limited")
        results = self._per_query.get(query, self._results)
        return StubResponse({"results": results})

    @property
    def bodies(self) -> list[dict]:
        return [c.get("json") or {} for c in self.calls]

    @property
    def queries(self) -> list[str]:
        return [b.get("query", "") for b in self.bodies]


def result(content, *, score=0.8, url="https://example.com/a"):
    return {"content": content, "url": url, "score": score, "title": "t"}


# ---------------------------------------------------------------------------
# Locality — the defect that poisoned two of the three queries
# ---------------------------------------------------------------------------


class TestLocality:
    """A search locality is a town, not a doorway.

    Every case here is a real ``business.city`` value from the live cluster,
    except the last two, which are the shapes the seeded and hand-made tenants
    use. All five have to survive the same function.
    """

    @pytest.mark.parametrize("address, expected", [
        ("1835 W Redondo Beach Blvd, Gardena, CA 90247, United States",
         "Gardena, CA"),
        ("22757 Hawthorne Blvd, Torrance, CA 90505, United States",
         "Torrance, CA"),
        # No country segment, and a multi-word city that must not be truncated.
        ("31208 Palos Verdes Dr W, Rancho Palos Verdes, CA 90275",
         "Rancho Palos Verdes, CA"),
        # Already a locality. Passing it through unharmed matters because the
        # seeded demo tenant stores exactly this shape.
        ("Columbus, Ohio", "Columbus, Ohio"),
        ("Columbus", "Columbus"),
    ])
    def test_the_street_is_dropped_and_the_town_survives(self, address, expected):
        assert locality_of(address) == expected

    def test_the_zip_code_goes_with_the_street(self):
        """A ZIP is as pinning as a street number and just as useless to search."""
        assert "90247" not in (locality_of(
            "1835 W Redondo Beach Blvd, Gardena, CA 90247, United States") or "")

    @pytest.mark.parametrize("address", [None, "", "   ", ","])
    def test_nothing_in_nothing_out(self, address):
        assert locality_of(address) is None


class TestOffering:
    """What the business sells, recovered from the facts it wrote at signup.

    Read from ``business_fact`` rather than ``profile_data`` because the live
    cluster has the fact and not the JSON: all three tenants carry "What we sell
    is Korean BBQ."/"...is Sushi." while their ``profile_data.buyers.offers``
    is an empty list, emptied by a later profile edit. The sentence is the
    surviving copy, so it is the one worth parsing.
    """

    def test_it_reads_the_signup_sentence(self):
        facts = [
            "Yellow Cow Korean BBQ is a restaurant or café in Gardena, CA.",
            "What we sell is Korean BBQ.",
            "The owner's stated goal right now is to More demand.",
        ]
        assert offering_from_facts(facts) == "Korean BBQ"

    def test_absent_is_none_rather_than_a_guess(self):
        """A rivals query with no offering must say so, not invent a cuisine."""
        assert offering_from_facts(["The owner's goal is more demand."]) is None
        assert offering_from_facts([]) is None


# ---------------------------------------------------------------------------
# The query plan
# ---------------------------------------------------------------------------


PLAN_KW = dict(business_name="Yellow Cow Korean BBQ",
               locality="Gardena, CA",
               offering="Korean BBQ",
               category="restaurant_cafe")


class TestQueryPlan:
    def test_no_query_carries_a_street_address(self):
        """The regression that started this. Measured: a street-pinned query
        returns directory pages about the tenant, not the market around it."""
        plan = build_query_plan(**PLAN_KW)
        assert plan, "a configured business must produce queries"
        for query in plan:
            assert "1835" not in query.text
            assert "Redondo Beach Blvd" not in query.text

    def test_every_query_names_the_town(self):
        for query in build_query_plan(**PLAN_KW):
            assert "Gardena" in query.text

    def test_reviews_are_labelled_reviews(self):
        """`observation_kind` has always had a 'review' value; nothing used it.

        Without this the Analyst reads a customer complaint as a market trend,
        and `rating` is never populated on any row in the corpus.
        """
        kinds = {q.kind for q in build_query_plan(**PLAN_KW)}
        assert "review" in kinds

    def test_the_rival_query_names_the_offering_and_not_the_business(self):
        """Measured: "best Korean BBQ restaurants in Gardena, CA menu prices"
        returns Hanu Korean BBQ (4.7, 2.4k reviews) and Shilla Korean BBQ.
        The old query, which named the tenant, returned the tenant."""
        rivals = [q for q in build_query_plan(**PLAN_KW)
                  if q.kind in {"rival_price", "rival_menu"}]
        assert rivals, "the plan must look at the competition"
        assert any("Korean BBQ" in q.text for q in rivals)
        assert all("Yellow Cow" not in q.text for q in rivals)

    def test_it_asks_a_concrete_hypothesis_about_waits(self):
        """CLAUDE.md's retrieval rule, applied at ingest instead of retrieval.

        Concrete queries beat abstract ones 0.583 to 0.238 at *retrieval* time.
        The same holds at *ingest*: "wait time busy weekend crowded" returned
        "Thursday through Saturday gets packed with long wait" at 0.715, and the
        corpus currently contains no row like it at all.
        """
        text = " ".join(q.text.lower() for q in build_query_plan(**PLAN_KW))
        assert "wait" in text

    def test_an_unknown_offering_still_produces_a_usable_plan(self):
        """A tenant who skipped the question gets fewer queries, not a crash,
        and never a query with the word "None" in it."""
        plan = build_query_plan(**{**PLAN_KW, "offering": None})
        assert plan
        assert all("None" not in q.text for q in plan)

    def test_no_locality_means_no_plan(self):
        """Better to observe nothing than to search the whole country and store
        the result as if it were about this street."""
        assert build_query_plan(**{**PLAN_KW, "locality": None}) == ()


# ---------------------------------------------------------------------------
# The source itself
# ---------------------------------------------------------------------------


ONE_QUERY = (TavilyQuery(text="q1", kind="review", label="web:reviews"),)


class TestRequestShape:
    def test_the_key_travels_in_the_header_not_the_body(self):
        """Tavily's documented auth is a Bearer header. The body form is legacy,
        and a body parameter is the one that ends up in a logged payload."""
        http = StubHttp(results=[result("a")])
        TavilySignalSource(api_key="tvly-secret", client=http,
                           queries=ONE_QUERY).fetch(
            business_name="B", city="Gardena, CA", limit=10)

        assert http.calls[0]["headers"]["Authorization"] == "Bearer tvly-secret"
        assert "api_key" not in http.bodies[0]

    def test_max_results_never_exceeds_the_documented_ceiling(self):
        """Tavily caps max_results at 20 and rejects more. The old code computed
        `limit // len(queries)`, which asks for 50 the moment a source is given
        one query and Radar's default limit."""
        http = StubHttp(results=[result("a")])
        TavilySignalSource(api_key="k", client=http, queries=ONE_QUERY).fetch(
            business_name="B", city="Gardena, CA", limit=50)

        assert http.bodies[0]["max_results"] <= MAX_TAVILY_RESULTS

    def test_at_least_one_result_is_requested_per_query(self):
        """Integer division used to be able to reach zero, which asks Tavily for
        nothing and spends a call to receive it."""
        http = StubHttp(results=[result("a")])
        source = TavilySignalSource(
            api_key="k", client=http,
            queries=tuple(TavilyQuery(text=f"q{i}") for i in range(9)))
        source.fetch(business_name="B", city="Gardena, CA", limit=3)

        assert all(b["max_results"] >= 1 for b in http.bodies)

    def test_per_query_parameters_reach_the_request(self):
        http = StubHttp(results=[result("a")])
        query = TavilyQuery(text="q", topic="news", time_range="month",
                            search_depth="advanced",
                            exclude_domains=("zillow.com",))
        TavilySignalSource(api_key="k", client=http, queries=(query,)).fetch(
            business_name="B", city="Gardena, CA", limit=5)

        body = http.bodies[0]
        assert body["topic"] == "news"
        assert body["time_range"] == "month"
        assert body["search_depth"] == "advanced"
        assert "zillow.com" in body["exclude_domains"]


class TestScoreFloor:
    def test_low_scoring_results_are_dropped(self):
        """Measured floor evidence: 0.199 was a chef-bio carousel, 0.172 a
        "Get Directions" block, 0.066 OCR'd text from a Facebook image and
        0.040 an article about Houston. None of them are about this business."""
        http = StubHttp(results=[
            result("real review text", score=0.72),
            result("Restaurant chef Restaurant chef", score=0.199),
            result("Houston led every city in closures", score=0.040),
        ])
        signals = TavilySignalSource(
            api_key="k", client=http, queries=ONE_QUERY, min_score=0.35,
        ).fetch(business_name="B", city="Gardena, CA", limit=10)

        assert [s.content for s in signals] == ["real review text"]

    def test_a_missing_score_is_kept_rather_than_assumed_bad(self):
        """Absent is not zero. If Tavily ever stops returning the field, this
        must degrade to the old behaviour instead of silently storing nothing."""
        http = StubHttp(results=[{"content": "kept", "url": "https://e.com/x"}])
        signals = TavilySignalSource(
            api_key="k", client=http, queries=ONE_QUERY, min_score=0.35,
        ).fetch(business_name="B", city="Gardena, CA", limit=10)

        assert [s.content for s in signals] == ["kept"]


class TestSignalLabelling:
    def test_the_query_decides_the_kind_and_the_source_name(self):
        http = StubHttp(per_query={
            "reviews": [result("waited 40 minutes", score=0.7)],
            "rivals": [result("Hanu KBBQ $32 per person", score=0.7)],
        })
        source = TavilySignalSource(api_key="k", client=http, queries=(
            TavilyQuery(text="reviews", kind="review", label="web:reviews"),
            TavilyQuery(text="rivals", kind="rival_price", label="web:rivals"),
        ))
        signals = source.fetch(business_name="B", city="Gardena, CA", limit=10)

        by_content = {s.content: s for s in signals}
        assert by_content["waited 40 minutes"].kind == "review"
        assert by_content["waited 40 minutes"].source_name == "web:reviews"
        assert by_content["Hanu KBBQ $32 per person"].kind == "rival_price"
        assert by_content["Hanu KBBQ $32 per person"].source_name == "web:rivals"

    def test_the_url_is_carried_so_hygiene_can_run(self):
        """`_usable` in Radar only cleans rows that carry a source_url, so a
        dropped URL silently disables chrome-stripping for that row."""
        http = StubHttp(results=[result("x", url="https://www.yelp.com/biz/a")])
        signals = TavilySignalSource(api_key="k", client=http,
                                     queries=ONE_QUERY).fetch(
            business_name="B", city="Gardena, CA", limit=10)

        assert signals[0].source_url == "https://www.yelp.com/biz/a"


class TestPartialFailure:
    def test_one_failing_query_does_not_lose_the_others(self):
        """Radar already treats a whole source as best-effort. Inside the source
        the same rule has to hold, or a single rate-limited query costs the night
        every other query's observations — which is the difference between a
        thin night and a blind one.
        """
        http = StubHttp(per_query={"good": [result("kept", score=0.7)]},
                        fail_queries=("bad",))
        source = TavilySignalSource(api_key="k", client=http, queries=(
            TavilyQuery(text="bad"), TavilyQuery(text="good"),
        ))
        signals = source.fetch(business_name="B", city="Gardena, CA", limit=10)

        assert [s.content for s in signals] == ["kept"]

    def test_every_query_failing_raises_so_radar_records_the_failure(self):
        """Silently returning nothing would let a dead API look like a quiet
        night, and `RadarResult.failed_sources` would never name it."""
        http = StubHttp(fail_queries=("a", "b"))
        source = TavilySignalSource(api_key="k", client=http, queries=(
            TavilyQuery(text="a"), TavilyQuery(text="b"),
        ))
        with pytest.raises(Exception):
            source.fetch(business_name="B", city="Gardena, CA", limit=10)


class TestBackwardCompatibility:
    def test_plain_string_queries_still_work(self):
        """`_build_sources` and several tests construct this source with bare
        strings. Breaking that would be a bigger change than this one earns."""
        http = StubHttp(results=[result("a", score=0.9)])
        signals = TavilySignalSource(api_key="k", client=http,
                                     queries=("just a string",)).fetch(
            business_name="B", city="Gardena, CA", limit=5)

        assert http.queries == ["just a string"]
        assert signals[0].kind == "trend"

    def test_it_is_still_a_signal_source(self):
        from brasstacks.signals import SignalSource

        assert isinstance(TavilySignalSource(api_key="k"), SignalSource)
        assert TavilySignalSource(api_key="k").retention_hours is None
