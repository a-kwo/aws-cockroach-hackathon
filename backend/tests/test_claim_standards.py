"""What kind of claim a find is making, and what that kind has to survive.

An audit of the nine finds live on 2026-08-03 found four false and one weak.
None of them was stopped by a similarity threshold — measured, no admission rule
that leaves the product alive removes any of them, because their evidence scores
*well*. What separates them is the kind of assertion they make:

* **7c4a9124** told Yellow Cow its delivery menu was broken. Its three Grubhub
  rows are the second, third and fourth strongest in the tenant. They are also
  `page_state` rows captured at 08:40 local on a Sunday, two hours and twenty
  minutes before the restaurant opened. A closed shop's storefront saying "this
  menu isn't available right now" is the shop being shut.
* **cbac2b29** told Palsaik its Apple Maps listing shows "Claim This Place".
  That row scores 0.147 — under every absolute floor anyone proposed — and it is
  verbatim, durable, and checkable in thirty seconds. It is the cheapest true
  win in the dataset and it must survive.

So a find declares what kind of claim it is making, and each kind is held to a
standard its evidence can actually be measured against. The design rule
throughout is **demotion over rejection**: a find that cannot support
"your menu is broken right now" can often still support "customers keep saying
this", and an owner who opens the board to nothing every morning churns. An
earlier design of these gates, measured against this exact corpus, withheld 9 of
9. Withholding is the last resort, not the default.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from brasstacks.finds import (
    CLAIM_TYPES,
    EvidenceFact,
    InvalidFindError,
    asserts_current_state,
    parse_find,
)

TODAY = date(2026, 8, 4)
# 08:40 local in California, which is when Radar actually fetched Yellow Cow's
# Grubhub storefront on the night find 7c4a9124 was written.
CLOSED_CAPTURE = datetime(2026, 8, 2, 15, 40, tzinfo=timezone.utc)


def fact(observation_id, *, statement_type="review", source=None, open_at=None):
    return EvidenceFact(observation_id=observation_id,
                        statement_type=statement_type,
                        source_identity=source,
                        captured_while_open=open_at)


def payload(**overrides):
    base = {
        "emoji": "🍱",
        "title": "Photograph the weekday lunch bento",
        "claim_type": "opportunity",
        "summary": "Reviewers name the lunch bento but no listing shows a photo.",
        "rationale": "Two reviewers name it and no listing carries a picture.",
        "move": "Photograph three bentos on the patio in daylight.",
        "predicted_daily_cents": 1400,
        "confidence": 0.4,
        "verify_after_days": 14,
        "evidence_observation_ids": ["a"],
    }
    base.update(overrides)
    return base


def judge(payload_, facts):
    return parse_find(payload_, today=TODAY,
                      known_observation_ids=[f.observation_id for f in facts],
                      evidence_facts={f.observation_id: f for f in facts})


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

class TestTheFourTiers:
    def test_the_tiers_are_named_and_ordered_by_burden(self):
        assert CLAIM_TYPES == ("current_state", "pattern", "opportunity",
                               "listing_fact")

    def test_a_find_carries_the_tier_it_landed_in(self):
        found = judge(payload(), [fact("a", source="yelp.com/biz/rosas")])

        assert found.claim_type == "opportunity"
        assert found.claim.withheld is False
        assert found.claim.demoted is False


# ---------------------------------------------------------------------------
# current_state
# ---------------------------------------------------------------------------

class TestCurrentState:
    """"Your delivery menu is broken." "Your Apple Maps listing is unclaimed."

    The gate has to be precise, because the blunt version — "a capture taken
    outside opening hours cannot support a current-state claim" — was measured
    against this corpus and withheld EVERYTHING. Every stored observation in the
    cluster was captured before its tenant opened; Radar sweeps at 06:00 and
    18:00 and restaurants open at 11:00. `statement_type = page_state` is the
    precise gate. The blunt one is unusable.
    """

    def test_a_page_state_row_captured_while_shut_cannot_carry_it(self):
        # Find 7c4a9124's exact shape: a storefront banner read as an outage.
        found = judge(
            payload(claim_type="current_state",
                    evidence_observation_ids=["a", "b"]),
            [fact("a", statement_type="page_state", source="grubhub.com#2033337",
                  open_at=False),
             fact("b", statement_type="review", source="yelp.com/biz/rosas")],
        )

        assert found.claim_type != "current_state"
        assert found.claim.demoted is True
        assert "page-state" in found.claim.reason
        assert "shut" in found.claim.reason

    def test_the_same_row_captured_during_service_does_carry_it(self):
        found = judge(
            payload(claim_type="current_state",
                    evidence_observation_ids=["a", "b"]),
            [fact("a", statement_type="page_state", source="grubhub.com#2033337",
                  open_at=True),
             fact("b", statement_type="review", source="yelp.com/biz/rosas")],
        )

        assert found.claim_type == "current_state"
        assert found.claim.demoted is False

    def test_an_unknown_capture_answer_is_not_a_yes(self):
        # No business record stores a timezone and opening hours are not a
        # stored field. "We could not tell" has to read as "cannot claim it",
        # or the gate is decorative.
        found = judge(
            payload(claim_type="current_state",
                    evidence_observation_ids=["a", "b"]),
            [fact("a", statement_type="page_state", source="grubhub.com#2033337",
                  open_at=None),
             fact("b", statement_type="review", source="yelp.com/biz/rosas")],
        )

        assert found.claim_type != "current_state"

    def test_a_durable_fact_carries_it_whatever_time_the_page_was_fetched(self):
        # THE test that stops this becoming the blunt gate. Find cbac2b29's
        # Apple Maps row was captured at 08:07 local, hours before Palsaik
        # opened — and "Claim This Place" is as true at 08:07 as at 20:00.
        found = judge(
            payload(claim_type="current_state",
                    title="Claim your Apple Maps listing",
                    summary="Your Apple Maps profile is unclaimed.",
                    evidence_observation_ids=["a"]),
            [fact("a", statement_type="listing",
                  source="maps.apple.com/place?place-id=ICBB1268B3CBE6815",
                  open_at=False)],
        )

        assert found.claim_type == "current_state"
        assert found.claim.demoted is False

    def test_a_promotion_is_not_reported_as_a_demotion(self):
        # Find cbac2b29's shape, and the reason `demoted` is not simply
        # `tier != requested`. This find is the cheapest true win in the whole
        # dataset; filing it beside the four the audit called false, in the one
        # field an operator scans, would bury it.
        found = judge(
            payload(claim_type="opportunity",
                    summary="Your Apple Maps profile is unclaimed.",
                    evidence_observation_ids=["a", "b"]),
            [fact("a", statement_type="listing", source="maps.apple.com/place"),
             fact("b", statement_type="hours", source="facebook.com/palsaik")],
        )

        assert found.claim_type == "current_state"
        assert found.claim.demoted is False
        assert found.claim.reasons == ()


# ---------------------------------------------------------------------------
# pattern
# ---------------------------------------------------------------------------

class TestPattern:
    """"Customers repeatedly mention the wait." Repeatedly means two sources."""

    def test_two_distinct_sources_carry_it(self):
        found = judge(
            payload(claim_type="pattern", evidence_observation_ids=["a", "b"]),
            [fact("a", source="yelp.com/biz/rosas"),
             fact("b", source="tripadvisor.com/rosas")],
        )

        assert found.claim_type == "pattern"

    def test_a_storefront_and_its_mirror_are_one_source(self):
        # seamless.com/menu/rosas/2033337 IS grubhub.com/restaurant/rosas/2033337
        # — same catalogue, same store id, two front doors. Find 7c4a9124 cited
        # both and read them as two platforms confirming each other.
        found = judge(
            payload(claim_type="pattern", evidence_observation_ids=["a", "b"]),
            [fact("a", source="grubhub.com#2033337"),
             fact("b", source="grubhub.com#2033337")],
        )

        assert found.claim_type == "opportunity"
        assert found.claim.demoted is True

    def test_rows_we_cannot_attribute_do_not_prove_independence(self):
        # The seeded corpus and every owner upload have no URL, and
        # source_identity returns None for them by design. The retrieval
        # backstop counts each of those as its own source, because it is
        # measuring how broad the prompt is. This is the opposite question —
        # whether two rows confirm each other — and a row we cannot attribute
        # cannot be shown to be independent of the row beside it.
        found = judge(
            payload(claim_type="pattern", evidence_observation_ids=["a", "b"]),
            [fact("a"), fact("b")],
        )

        assert found.claim_type == "opportunity"


# ---------------------------------------------------------------------------
# opportunity
# ---------------------------------------------------------------------------

class TestOpportunity:
    """"A fixed-price lunch set would sell." Weak evidence is allowed here.

    What is not allowed is using the label as a side door. If the prose asserts
    that something is broken right now, the find is making a current-state claim
    whatever it calls itself, and it is judged as one.
    """

    def test_one_weak_row_is_enough(self):
        found = judge(payload(evidence_observation_ids=["a"]), [fact("a")])

        assert found.claim_type == "opportunity"
        assert found.claim.withheld is False

    def test_prose_that_asserts_a_broken_channel_is_judged_as_current_state(self):
        # Find 7c4a9124's summary, verbatim in shape: the label would have been
        # the weakest tier and the sentence is the strongest possible claim.
        found = judge(
            payload(claim_type="opportunity",
                    title="Fix the delivery menu so orders can be placed",
                    summary=('Your delivery listing is showing "menu isn\'t '
                             'available right now" during trading hours.'),
                    evidence_observation_ids=["a", "b"]),
            [fact("a", statement_type="page_state", source="grubhub.com#2033337",
                  open_at=False),
             fact("b", statement_type="hours", source="facebook.com/yellowcow")],
        )

        assert found.claim.withheld is True
        assert found.claim_type is None

    def test_withholding_is_a_verdict_not_an_exception(self):
        # The other two finds of the night must still be stored. Raising here
        # would cost the whole deck, which is how gates end up withholding 9 of 9.
        found = judge(
            payload(claim_type="opportunity",
                    summary="Your online ordering is broken right now.",
                    evidence_observation_ids=["a"]),
            [fact("a", statement_type="page_state", source="grubhub.com#2033337",
                  open_at=False)],
        )

        assert found.claim.withheld is True
        assert found.title  # still a fully parsed find, ready to be stored

    def test_an_assertion_backed_by_an_open_capture_survives_intact(self):
        found = judge(
            payload(claim_type="opportunity",
                    summary="Your online ordering is broken right now.",
                    evidence_observation_ids=["a"]),
            [fact("a", statement_type="page_state", source="grubhub.com#2033337",
                  open_at=True)],
        )

        assert found.claim_type == "current_state"
        assert found.claim.withheld is False


class TestWhatCountsAsAssertingCurrentState:
    """Tight on purpose. A loose reading promotes ordinary opportunity finds
    into the strictest tier and then withholds them, which is the failure mode
    this whole tranche was written to avoid."""

    @pytest.mark.parametrize("text", [
        'showing "menu isn\'t available right now"',
        "your delivery storefront is down",
        "the online menu is not working",
        "there is no way to order from the page",
        "your Apple Maps profile is unclaimed",
        "turn every paused or sold-out item back on",
        "the listing is temporarily closed",
    ])
    def test_these_assert_it(self, text):
        assert asserts_current_state(text) is True

    @pytest.mark.parametrize("text", [
        # Every one of these is real copy from the nine live finds.
        "Your patio view is the thing guests rave about, but your website and "
        "delivery listings sell takeout instead",
        "Delivery reviewers are complaining about portion size for the price",
        "Your lunch specials and bentos already get named in reviews but are "
        "barely visible online",
        "Your best-selling delivery items are tteokbokki and the two soups",
        "You open at 11AM but read as a $$$ dinner house",
        "Your room is already described as lively and built for gatherings",
        "Set the open hours to match Mon/Wed/Sun 11-9, Tue closed",
    ])
    def test_these_do_not(self, text):
        assert asserts_current_state(text) is False


# ---------------------------------------------------------------------------
# listing_fact
# ---------------------------------------------------------------------------

class TestListingFact:
    """The cold-start tier. On night one a tenant has had one sweep and can
    honestly say "your Apple Maps listing shows Claim This Place" and very
    little else. That is worth showing, and it is worth no money."""

    def test_one_source_no_page_state_and_no_revenue_figure(self):
        found = judge(
            payload(claim_type="listing_fact",
                    title="Claim your Apple Maps listing",
                    summary="Apple Maps shows Claim This Place on your profile.",
                    predicted_daily_cents=1600,
                    evidence_observation_ids=["a"]),
            [fact("a", statement_type="listing",
                  source="maps.apple.com/place?place-id=ICBB1268B3CBE6815",
                  open_at=False)],
        )

        assert found.claim_type == "listing_fact"
        assert found.predicted_daily_cents == 0
        assert any("revenue" in reason for reason in found.claim.reasons)

    def test_the_unclaimed_wording_does_not_promote_it(self):
        # "unclaimed" asserts a current state, and this tier is *by
        # construction* a weaker claim than current_state — one verbatim durable
        # fact, one source, no money on it. Promoting it would withhold the
        # cheapest true win in the dataset on a word.
        found = judge(
            payload(claim_type="listing_fact",
                    summary="Your Apple Maps profile is unclaimed.",
                    evidence_observation_ids=["a"]),
            [fact("a", statement_type="listing", source="maps.apple.com/place")],
        )

        assert found.claim_type == "listing_fact"

    def test_two_sources_is_not_a_single_checkable_fact(self):
        found = judge(
            payload(claim_type="listing_fact", predicted_daily_cents=1600,
                    evidence_observation_ids=["a", "b"]),
            [fact("a", source="maps.apple.com/place"),
             fact("b", source="yelp.com/biz/rosas")],
        )

        assert found.claim_type == "pattern"
        assert found.predicted_daily_cents == 1600

    def test_a_perishable_row_is_not_a_durable_fact(self):
        found = judge(
            payload(claim_type="listing_fact",
                    evidence_observation_ids=["a"]),
            [fact("a", statement_type="page_state",
                  source="grubhub.com#2033337", open_at=False)],
        )

        assert found.claim_type == "opportunity"
        assert found.claim.demoted is True


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

class TestAMissingOrWrongLabelDoesNotCostTheNight:
    def test_an_absent_claim_type_is_inferred(self):
        body = payload()
        del body["claim_type"]

        found = judge(body, [fact("a")])

        assert found.claim_type == "opportunity"

    def test_an_unrecognised_claim_type_is_inferred(self):
        found = judge(payload(claim_type="revenue_claim"), [fact("a")])

        assert found.claim_type == "opportunity"

    def test_an_inferred_label_still_meets_the_current_state_standard(self):
        body = payload(summary="Your delivery storefront is down.",
                       evidence_observation_ids=["a"])
        del body["claim_type"]

        found = judge(body, [fact("a", statement_type="page_state",
                                  source="grubhub.com#2033337", open_at=False)])

        assert found.claim.withheld is True

    def test_a_caller_with_no_evidence_facts_gets_the_label_it_asked_for(self):
        # Every path that predates this tranche calls parse_find with two
        # keyword arguments. Failing those would take out the Ask handler and
        # the replay tooling to enforce a standard they have no data for.
        found = parse_find(payload(claim_type="pattern"), today=TODAY,
                           known_observation_ids=["a"])

        assert found.claim_type == "pattern"
        assert found.claim.withheld is False

    def test_the_money_rules_still_run_first(self):
        with pytest.raises(InvalidFindError):
            judge(payload(claim_type="listing_fact",
                          predicted_daily_cents=23.5), [fact("a")])


# ---------------------------------------------------------------------------
# Was it open? — the input the standard cannot be enforced without
# ---------------------------------------------------------------------------

class TestTheLocalClock:
    """No business row stores a timezone, and all three live tenants have a
    NULL `region` with the state buried in the address string. So the offset is
    derived, in pure Python, from the address and the standard US rule — no new
    dependency, because the Lambda image installs psycopg, anthropic and boto3
    and a zoneinfo database is not worth widening that for one lookup."""

    def test_a_california_address_in_august_is_seven_hours_behind(self):
        from brasstacks.agents.analyst import local_utc_offset_minutes

        # The live `city` column, verbatim.
        place = "1835 W Redondo Beach Blvd, Gardena, CA 90247, United States"
        assert local_utc_offset_minutes(place, CLOSED_CAPTURE) == -420

    def test_the_same_address_in_january_is_eight_hours_behind(self):
        from brasstacks.agents.analyst import local_utc_offset_minutes

        winter = datetime(2026, 1, 15, 15, 40, tzinfo=timezone.utc)
        assert local_utc_offset_minutes("Torrance, CA 90505", winter) == -480

    def test_arizona_does_not_move(self):
        from brasstacks.agents.analyst import local_utc_offset_minutes

        summer = datetime(2026, 8, 2, 15, 40, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 15, 15, 40, tzinfo=timezone.utc)
        assert local_utc_offset_minutes("Phoenix, AZ 85004", summer) == -420
        assert local_utc_offset_minutes("Phoenix, AZ 85004", winter) == -420

    def test_an_address_we_cannot_place_returns_nothing(self):
        from brasstacks.agents.analyst import local_utc_offset_minutes

        # Guessing would be worse than admitting it: a guessed offset makes an
        # openness answer that reads as measured.
        assert local_utc_offset_minutes("Lyon, France", CLOSED_CAPTURE) is None
        assert local_utc_offset_minutes(None, CLOSED_CAPTURE) is None


class TestWasTheBusinessOpen:
    def _state(self, *days):
        from brasstacks.business_state import BusinessState, DayHours, HoursClaim

        return BusinessState(
            name="Yellow Cow Korean BBQ", declared_domain=None, offers=(),
            days=tuple(
                DayHours(weekday=weekday,
                         claims=(HoursClaim(weekday=weekday, opens=opens,
                                            closes=closes,
                                            source="facebook.com/yellowcow"),))
                for weekday, opens, closes in days),
            undated_hours=(), domains=())

    def test_a_capture_before_the_doors_open_is_shut(self):
        from brasstacks.agents.analyst import capture_was_open

        # 2026-08-02 15:40 UTC is 08:40 on a Sunday in Gardena. Yellow Cow's
        # Facebook page says Sunday 11AM–9PM. This is find 7c4a9124's capture.
        state = self._state((6, 11 * 60, 21 * 60))

        assert capture_was_open(CLOSED_CAPTURE, state, -420) is False

    def test_a_capture_during_service_is_open(self):
        from brasstacks.agents.analyst import capture_was_open

        state = self._state((6, 11 * 60, 21 * 60))
        lunchtime = datetime(2026, 8, 2, 19, 30, tzinfo=timezone.utc)

        assert capture_was_open(lunchtime, state, -420) is True

    def test_a_weekday_memory_says_nothing_about_is_unknown(self):
        from brasstacks.agents.analyst import capture_was_open

        # Monday only. Sunday is not "closed", it is unrecorded, and the
        # difference is the whole reason this returns three values.
        state = self._state((0, 11 * 60, 21 * 60))

        assert capture_was_open(CLOSED_CAPTURE, state, -420) is None

    def test_a_trading_day_that_ends_after_midnight_still_counts(self):
        from brasstacks.agents.analyst import capture_was_open

        # Saturday 17:00–02:00. A capture at 01:00 on Sunday is inside it.
        state = self._state((5, 17 * 60, 26 * 60))
        after_midnight = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)

        assert capture_was_open(after_midnight, state, -420) is True

    def test_without_an_offset_there_is_no_answer(self):
        from brasstacks.agents.analyst import capture_was_open

        state = self._state((6, 11 * 60, 21 * 60))

        assert capture_was_open(CLOSED_CAPTURE, state, None) is None
        assert capture_was_open(CLOSED_CAPTURE, None, -420) is None
        assert capture_was_open(None, state, -420) is None


# ---------------------------------------------------------------------------
# A whole night, with the standards wired in
# ---------------------------------------------------------------------------

GRUBHUB = "https://www.grubhub.com/restaurant/yellow-cow/2033337"
FACEBOOK = "https://www.facebook.com/yellowcowkbbq"

# The two rows that mattered on the night find 7c4a9124 was written, near
# enough. The first is what Grubhub renders on a shut storefront; the second is
# the Facebook page that says when the shop actually opens.
STOREFRONT = ("Yellow Cow Korean BBQ 4.9 (257 ratings) $0 delivery fee. "
              "This menu isn't available right now. Preorder for 6:30pm.")
HOURS = ("THE food combo of the summer. Mon, Wed, Sun - 11AM~9PM  "
         "Tue - Closed  Thur, Sat - 11AM~9:30PM  Fri - 11AM~10PM "
         "(310) 329-7343 1835 W Redondo Beach Blvd")


def _night_repo():
    from brasstacks.providers import FakeEmbedder
    from brasstacks.repository import InMemoryRepository

    repo = InMemoryRepository()
    business = repo.create_business(
        name="Yellow Cow Korean BBQ", category="restaurant",
        city="1835 W Redondo Beach Blvd, Gardena, CA 90247, United States")
    embedder = FakeEmbedder()
    ids = []
    for content, url in ((STOREFRONT, GRUBHUB), (HOURS, FACEBOOK)):
        [vector] = embedder.embed([content])
        ids.append(repo.insert_observation(
            business, content=content, kind="trend", embedding=vector,
            observed_at=CLOSED_CAPTURE, source_name="web", source_url=url))
    return repo, business, ids


def _outage_find(ids, **overrides):
    body = payload(
        title="Fix the delivery menu so orders can be placed",
        claim_type="current_state",
        summary=('Your delivery listing is showing "menu isn\'t available '
                 'right now" during trading hours.'),
        move="Open the delivery dashboard and check the store status.",
        predicted_daily_cents=4000,
        evidence_observation_ids=list(ids))
    body.update(overrides)
    return body


class TestTheNightAppliesTheStandards:
    def test_the_model_is_asked_which_kind_of_claim_it_is_making(self):
        from brasstacks.agents.analyst import FIND_SCHEMA, SYSTEM_PROMPT

        assert "claim_type" in FIND_SCHEMA["properties"]
        assert "claim_type" in FIND_SCHEMA["required"]
        for tier in CLAIM_TYPES:
            assert tier in SYSTEM_PROMPT

    def test_a_storefront_captured_before_opening_does_not_reach_the_board(self):
        # The whole chain, end to end: Radar's stored text is classified
        # page_state, the Gardena address gives -420, Sunday 08:40 is before the
        # 11AM the Facebook row states, and the summary asserts an outage. No
        # tier fits, so nothing is proposed.
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.providers import FakeEmbedder, FakeReasoner

        repo, business, ids = _night_repo()
        reasoner = FakeReasoner([_outage_find(ids)])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY)

        assert result.find_id is None
        assert repo.recent_finds(business, limit=10) == []
        [outcome] = result.outcomes
        assert outcome.verdict.withheld is True
        assert "shut" in outcome.verdict.reason

    def test_the_other_finds_of_the_night_still_reach_the_board(self):
        # One bad card must not cost the deck. This is the difference between a
        # gate and the earlier design that withheld 9 of 9.
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.providers import FakeEmbedder, FakeReasoner

        repo, business, ids = _night_repo()
        survivor = payload(title="Put the lunch combo on the storefront",
                           evidence_observation_ids=[ids[1]])
        reasoner = FakeReasoner([{"finds": [_outage_find(ids), survivor]}])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY)

        assert [row.title for row in repo.recent_finds(business, limit=10)] == [
            "Put the lunch combo on the storefront"]
        # `find_id` is the night's headline, and a withheld card has no headline
        # to offer — the first card that survived does.
        assert result.find_id == result.find_ids[0]
        assert [o.verdict.withheld for o in result.outcomes] == [True, False]

    def test_the_receipt_says_how_many_were_held_back(self):
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.analyst_trace import parse_analyst_trace
        from brasstacks.providers import FakeEmbedder, FakeReasoner

        repo, business, ids = _night_repo()
        reasoner = FakeReasoner([_outage_find(ids)])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        [run] = repo.recent_runs(business, limit=1)
        trace = parse_analyst_trace(run.note)
        assert trace["claims_withheld"] == 1
        assert trace["claims_demoted"] == 0

    def test_a_demoted_find_is_still_proposed(self):
        # Same evidence, wording that does not assert an outage. It cannot be a
        # current_state claim, but two distinct sources make it a pattern, and a
        # pattern is worth the owner's morning.
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.providers import FakeEmbedder, FakeReasoner

        repo, business, ids = _night_repo()
        reasoner = FakeReasoner([_outage_find(
            ids,
            title="Review how the delivery storefront presents at open",
            summary="Delivery browsers see a preorder prompt before 11AM.",
        )])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY)

        assert result.find_id is not None
        [outcome] = result.outcomes
        assert outcome.claim_type == "pattern"
        assert outcome.verdict.demoted is True

    def test_a_withheld_find_is_kept_with_the_reason_it_was_withheld_for(self):
        # The durable half, owned by the tranche that landed beside this one.
        # Nobody can answer "why was the board empty on the 4th?" from a
        # candidate dropped on the floor.
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.providers import FakeEmbedder, FakeReasoner
        from brasstacks.repository import WITHHELD_STATUS

        repo, business, ids = _night_repo()
        reasoner = FakeReasoner([_outage_find(ids)])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY)

        [held] = repo.recent_finds(business, limit=10, include_unseen=True)
        context = repo.get_find_context(business, held.find_id)
        assert context.status == WITHHELD_STATUS
        assert "shut" in context.withheld_reason
        # Kept, and still not the night's headline nor on the owner's board.
        assert result.find_id is None
        assert repo.recent_finds(business, limit=10) == []

    def test_it_is_judged_and_reported_even_where_it_cannot_be_stored(self, monkeypatch):
        # The deployed Lambda image and the local harness upgrade separately,
        # and a night must not fail because the two halves landed in either
        # order. That mistake cost us a night over the ledger's actual column
        # and again over `find.alternative_explanation`, so the Analyst probes
        # for the column instead of naming it and hoping.
        from brasstacks import repository as repository_module
        from brasstacks.agents.analyst import run_analyst
        from brasstacks.providers import FakeEmbedder, FakeReasoner

        monkeypatch.delattr(repository_module, "WITHHELD_STATUS")
        repo, business, ids = _night_repo()
        reasoner = FakeReasoner([_outage_find(ids)])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY)

        assert repo.recent_finds(business, limit=10, include_unseen=True) == []
        assert result.outcomes[0].verdict.withheld is True
        assert result.outcomes[0].find_id is None
        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "ok"
