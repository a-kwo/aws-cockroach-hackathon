"""What the business already has, before anything is recommended to it.

Three of the nine finds live in the cluster on 2026-08-03 are wrong for one
reason, and it is the same reason: the Analyst was told what the market says
about the business and never told what the business *is*.

* **818fdb2d** told Yellow Cow to launch a fixed-price weekday lunch set. Its
  own corpus names the Dosirak set meal three times — on Postmates as a featured
  item, on Uber Eats as the most popular item, on Tripadvisor as something it
  offers. None of the three was cited. The move was not a launch.
* **c651141b** critiqued asakacatogo.com. The owner declared asakakaiyo.com.
  Nothing in the prompt could tell the model those were different domains, so a
  page nobody had attributed was criticised as the owner's own.
* **cbac2b29** called four sources "mutually contradictory hours" at the highest
  confidence of the nine (0.60) on the weakest evidence (top similarity 0.232).
  Two of the four carry no weekday at all — they are Grubhub pickup windows —
  and the rest overlap on three days. The contradiction was an artifact of
  nobody ever reconciling the sources per weekday.

Everything here is a pure function of a tenant profile and its stored rows. No
repository, no embedder, no model call: a claim about what a business already
sells has to be arguable in a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from brasstacks.business_state import (
    build_business_state,
    declared_domain,
    describe_business_state,
    parse_hours,
)

ASAKA = "Asaka"
ASAKA_ADDRESS = "31208 Palos Verdes Dr W, Rancho Palos Verdes, CA 90275"
PALSAIK = "Palsaik Korean BBQ"
PALSAIK_ADDRESS = "22757 Hawthorne Blvd, Torrance, CA 90505, United States"
YELLOW_COW = "Yellow Cow Korean BBQ"
YELLOW_COW_ADDRESS = "1835 W Redondo Beach Blvd, Gardena, CA 90247, United States"

CAPTURED = datetime(2026, 8, 2, 15, 7, 11, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Row:
    """The shape both `Retrieved` and the repository's stored row share."""

    observation_id: str
    content: str
    source_url: str | None = None
    kind: str = "trend"
    observed_at: datetime = CAPTURED
    statement_type: str | None = None


def state(rows, *, name=PALSAIK, address=PALSAIK_ADDRESS, facts=(), profile=None):
    return build_business_state(
        business={"name": name, "city": address, "profile_data": profile or {}},
        facts=list(facts),
        observations=list(rows),
    )


class TestTheDeclaredDomain:
    def test_reads_the_website_off_the_owner_profile(self):
        profile = {"business": {"website": "https://www.asakakaiyo.com/"}}
        assert declared_domain({"profile_data": profile}, []) == "asakakaiyo.com"

    def test_falls_back_to_the_owner_fact_sentence(self):
        # Where all three live tenants actually keep it. `profile_data.website`
        # is null for every one of them; onboarding wrote the sentence instead.
        assert declared_domain(
            {"profile_data": {}},
            ["What we sell is Sushi.",
             "The business website is https://www.asakakaiyo.com/."],
        ) == "asakakaiyo.com"

    def test_is_none_when_the_owner_never_declared_one(self):
        # Palsaik and Yellow Cow. "None" has to stay distinguishable from "the
        # owner declared this host", or every scraped page becomes theirs.
        assert declared_domain({"profile_data": {}}, ["What we sell is Korean BBQ."]) is None


class TestWhoseDomainIsThis:
    def test_the_declared_host_is_marked_as_the_owners(self):
        rows = [Row("a", "Order online at Asaka.", "https://www.asakakaiyo.com/order")]
        found = state(rows, name=ASAKA, address=ASAKA_ADDRESS,
                      facts=["The business website is https://www.asakakaiyo.com/."])

        assert [(d.host, d.declared) for d in found.domains] == [("asakakaiyo.com", True)]

    def test_a_subdomain_of_the_declared_site_is_still_the_owners(self):
        rows = [Row("a", "Asaka order page.", "https://order.asakakaiyo.com/menu")]
        found = state(rows, name=ASAKA, address=ASAKA_ADDRESS,
                      facts=["The business website is https://www.asakakaiyo.com/."])

        assert [d.declared for d in found.domains] == [True]

    def test_the_undeclared_lookalike_is_flagged(self):
        # The c651141b failure, exactly. asakacatogo.com carries Asaka's address
        # and phone number, so it may well be theirs — but the owner did not say
        # so, and the Analyst must attribute it before criticising it.
        rows = [Row("a", "Hours of Operation (Takeout). Asaka.",
                    "https://www.asakacatogo.com")]
        found = state(rows, name=ASAKA, address=ASAKA_ADDRESS,
                      facts=["The business website is https://www.asakakaiyo.com/."])

        note = found.domains[0]
        assert (note.host, note.declared, note.resembles_name) == (
            "asakacatogo.com", False, True)

    def test_a_platform_is_undeclared_but_does_not_pretend_to_be_the_business(self):
        rows = [Row("a", "Asaka Sushi & Grill, 4.9 stars.",
                    "https://www.yelp.com/biz/asaka-rancho-palos-verdes")]
        found = state(rows, name=ASAKA, address=ASAKA_ADDRESS,
                      facts=["The business website is https://www.asakakaiyo.com/."])

        note = found.domains[0]
        assert (note.declared, note.resembles_name) == (False, False)

    def test_with_no_declared_domain_nothing_is_the_owners(self):
        rows = [Row("a", "Palsaik Korean BBQ, 22757 Hawthorne Blvd.",
                    "https://palsaikkoreanbbqca.com/location")]
        found = state(rows)

        assert found.declared_domain is None
        assert [d.declared for d in found.domains] == [False]

    def test_one_note_per_host_carrying_every_row_on_it(self):
        rows = [Row("a", "Palsaik Korean BBQ hours.", "https://palsaikkoreanbbqca.com/a"),
                Row("b", "Palsaik Korean BBQ menu.", "https://palsaikkoreanbbqca.com/b")]
        found = state(rows)

        assert len(found.domains) == 1
        assert found.domains[0].observation_ids == ("a", "b")


class TestReadingHoursOffAPage:
    """Every string here is verbatim from a live observation."""

    def test_a_semicolon_list_of_named_days(self):
        # c62cebb0, palsaikkoreanbbqca.com/location
        claims = parse_hours(
            "Address 22757 Hawthorne Blvd, Torrance, CA 90505, USA · Hours "
            "Monday: 11:30 AM – 9:30 PM; Tuesday: 11:30 AM – 9:30 PM · Dining "
            "Dine-in · Takeout.")

        assert [(c.weekday, c.opens, c.closes) for c in claims] == [
            (0, 11 * 60 + 30, 21 * 60 + 30),
            (1, 11 * 60 + 30, 21 * 60 + 30),
        ]

    def test_a_comma_run_of_abbreviated_days_with_bracketed_windows(self):
        # 4627feb9, facebook.com/palsaik.california
        claims = parse_hours(
            "⏰Open : Mon,Tue,Thur,Sun(11:30am-10pm), Wed(5pm-10pm), )")

        assert [(c.weekday, c.opens, c.closes) for c in claims] == [
            (0, 690, 1320), (1, 690, 1320), (3, 690, 1320), (6, 690, 1320),
            (2, 17 * 60, 1320),
        ]

    def test_a_twenty_four_hour_window(self):
        # 9217ab42, fantuanorder.com
        claims = parse_hours("Korean3923.2km. Open. Thursday : 11:30-21:30.")

        assert [(c.weekday, c.opens, c.closes) for c in claims] == [(3, 690, 1290)]

    def test_a_range_of_days_with_the_window_on_the_next_line(self):
        # 38bffe87, asakacatogo.com — the layout the site actually ships.
        claims = parse_hours(
            "Hours of Operation (Takeout)\n\nMonday - Thursday\n\n"
            "11:30 am - 08:30 pm\n\nFriday - Sunday\n\n11:30 am - 08:45 pm")

        assert [(c.weekday, c.closes) for c in claims] == [
            (0, 1230), (1, 1230), (2, 1230), (3, 1230),
            (4, 1245), (5, 1245), (6, 1245),
        ]
        assert {c.service for c in claims} == {"takeout"}

    def test_a_closed_day_is_a_claim_not_a_silence(self):
        # be6b38f6, facebook.com/yellowcowkbbq. "Tue - Closed" is a fact about
        # Tuesday; dropping it would let a find propose a Tuesday promotion.
        claims = parse_hours(
            "⏰ Mon, Wed, Sun - 11AM~9PM Tue - Closed Thur, Sat - 11AM~9:30PM "
            "Fri - 11AM~10PM")

        by_day = {c.weekday: (c.opens, c.closes) for c in claims}
        assert by_day[0] == (660, 1260)
        assert by_day[1] == (None, None)
        assert by_day[3] == (660, 1290)
        assert by_day[4] == (660, 1320)

    def test_a_window_with_no_weekday_is_kept_apart(self):
        # 98c2d304 and b1e06301, grubhub.com. Two of the four sources find
        # cbac2b29 called mutually contradictory say nothing about any weekday.
        claims = parse_hours(
            "22757 Hawthorne Blvd Torrance, CA 90505 (310) 791-0300 Hours Today "
            "Pickup: 11:45am–8:45pm Delivery: 11:45am–8:45pm.")

        assert all(c.weekday is None for c in claims)
        assert {c.service for c in claims} == {"pickup", "delivery"}

    def test_a_labelled_service_window_keeps_its_label(self):
        # 2b815e9b, postmates.com. Asaka already runs a lunch menu until 3pm.
        claims = parse_hours("11:30 AM - 3:00 PM • Lunch Menu ()")

        assert [(c.weekday, c.opens, c.closes, c.service) for c in claims] == [
            (None, 690, 900, "lunch menu")]

    def test_a_phone_number_is_not_an_opening_time(self):
        assert parse_hours("Tel: 310-791-0300 📞 (310)-791-0300") == ()

    def test_a_price_range_is_not_an_opening_time(self):
        assert parse_hours("saik Korean BBQ- $15.99 Perrier $4.99. $10 - $22") == ()

    def test_a_bare_pair_of_numbers_is_not_an_opening_time(self):
        # No colon, no meridiem, no cue. "4 - 5" is a rating spread.
        assert parse_hours("rated 4 - 5 by 632 diners") == ()


class TestReconcilingHoursAcrossSources:
    def test_day_disjoint_sources_are_not_a_conflict(self):
        # The cbac2b29 failure. One source covers Thursday, another covers
        # Monday and Tuesday. They cannot contradict each other.
        found = state([
            Row("a", "Hours Monday: 11:30 AM – 9:30 PM; Tuesday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
            Row("b", "Open. Thursday : 11:30-21:30.",
                "https://www.fantuanorder.com/store/palsaik"),
        ])

        assert found.conflicts == ()
        assert {d.weekday for d in found.days} == {0, 1, 3}

    def test_two_sources_covering_one_day_differently_do_conflict(self):
        found = state([
            Row("a", "Hours Monday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
            Row("b", "⏰Open : Mon(11:30am-10pm)",
                "https://www.facebook.com/palsaik.california"),
        ])

        assert [d.weekday for d in found.conflicts] == [0]

    def test_one_source_repeating_itself_is_not_two_sources(self):
        # Both Grubhub rows are one storefront read once — the mistake
        # provenance.py exists to stop, arriving here by another route.
        found = state([
            Row("a", "Hours Monday: 11:30am-8:45pm",
                "https://www.grubhub.com/restaurant/palsaik/2169422"),
            Row("b", "Hours Monday: 11:45am-8:45pm",
                "https://www.grubhub.com/menu/palsaik/2169422"),
        ])

        assert found.conflicts == ()

    def test_a_dayless_window_never_contradicts_a_weekday(self):
        found = state([
            Row("a", "Hours Monday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
            Row("b", "Hours Today Pickup: 11:45am–8:45pm",
                "https://www.grubhub.com/restaurant/palsaik/2169422"),
        ])

        assert found.conflicts == ()
        assert [(c.service, c.opens) for c in found.undated_hours] == [("pickup", 705)]

    def test_two_services_on_one_day_are_not_a_disagreement(self):
        # Asaka's takeout window closes before its dining room does. That is a
        # service difference, not two sites disagreeing about the same fact.
        found = state([
            Row("a", "Hours of Operation (Takeout)\n\nMonday\n\n11:30 am - 08:30 pm",
                "https://www.asakacatogo.com"),
            Row("b", "Monday:\n\n9:00am - 9:00pm",
                "http://places.singleplatform.com/asaka-4/menu"),
            ], name=ASAKA, address=ASAKA_ADDRESS)

        assert found.conflicts == ()

    def test_one_source_saying_the_same_thing_twice_is_recorded_once(self):
        # asakacatogo.com is stored twice, whole. Both copies state the same
        # takeout window for all seven days, and printing fourteen identical
        # claims would push the block past the retrieved rows it sits above.
        page = ("Hours of Operation (Takeout)\n\nMonday - Thursday\n\n"
                "11:30 am - 08:30 pm")
        found = state([Row("38bffe87", page, "https://www.asakacatogo.com"),
                       Row("d1427ddc", page, "https://www.asakacatogo.com")],
                      name=ASAKA, address=ASAKA_ADDRESS)

        monday = next(day for day in found.days if day.weekday == 0)
        assert len(monday.claims) == 1
        assert found.conflicts == ()

    def test_a_closed_with_no_day_and_no_time_says_nothing(self):
        # mapquest and Apple Maps both ship a bare "Closed" badge. Kept, it
        # reads to the model as "this business is shut", which is a claim about
        # the moment the page was fetched and about nothing else.
        found = state([Row("a", "Palsaik Korean BBQ · Closed · Korean",
                           "https://www.mapquest.com/us/california/palsaik")])

        assert found.undated_hours == ()

    def test_the_label_beside_a_window_beats_one_further_up_the_page(self):
        # 2b815e9b, postmates.com, verbatim layout. Reading the second window as
        # another lunch window made Asaka look like it served lunch until 8:30pm.
        found = state([Row("2b815e9b",
                           "11:30 AM - 3:00 PM • Lunch Menu ()\n\n"
                           "11:30 AM - 8:30 PM • Menu",
                           "https://postmates.com/store/asaka-sushi-%26-grill/CyXU")],
                      name=ASAKA, address=ASAKA_ADDRESS)

        assert [(c.service, c.closes) for c in found.undated_hours] == [
            ("lunch menu", 900), ("menu", 1230)]

    def test_the_word_menu_higher_up_the_page_does_not_label_a_window(self):
        # m.yelp.com carries "Full menu" in its header and Palsaik's weekday
        # hours below it. Labelling those as the menu's hours put them in their
        # own service group, where they could no longer disagree with anybody.
        found = state([
            Row("a", "Full menu\n\nMonday: 11:30 AM – 9:30 PM",
                "https://m.yelp.com/biz/palsaik-korean-bbq-torrance-6"),
            Row("b", "⏰Open : Mon(11:30am-10pm)",
                "https://www.facebook.com/palsaik.california"),
        ])

        assert [c.service for c in found.days[0].claims] == [None, None]
        assert [d.weekday for d in found.conflicts] == [0]

    def test_the_days_come_back_in_week_order(self):
        found = state([
            Row("a", "Sunday: 11:30 AM – 9:30 PM. Monday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
        ])

        assert [d.weekday for d in found.days] == [0, 6]


class TestWhatTheBusinessAlreadySells:
    def test_the_owner_profile_is_an_origin_of_its_own(self):
        found = state([], facts=["What we sell is Korean BBQ."])

        assert [(o.name, o.origin) for o in found.offers] == [("Korean BBQ", "owner")]

    def test_the_owners_listed_offers_count_too(self):
        found = state([], profile={"buyers": {"offers": ["Party catering"]}})

        assert [(o.name, o.origin) for o in found.offers] == [("Party catering", "owner")]

    def test_the_featured_item_list_names_the_set_meal(self):
        # 83838c9f, postmates.com. This is the row find 818fdb2d needed and did
        # not have: the fixed-price set it told the owner to launch is the
        # storefront's first featured item.
        found = state(
            [Row("83838c9f",
                 "Featured items. Dosirak (Set Meal) - 도시락세트 · Set Meal · "
                 "Appetizers · Specialties · Fried Rice & Bibimbap · Soups & "
                 "Noodles · Grilled Beef · Grilled Pork.",
                 "https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf")],
            name=YELLOW_COW, address=YELLOW_COW_ADDRESS)

        names = [o.name for o in found.offers]
        assert "Dosirak (Set Meal)" in names
        assert "Grilled Pork" in names
        assert "Featured items" not in names

    def test_every_row_naming_an_offer_is_cited_for_it(self):
        # Three rows, one offer. The citation set is what makes the block
        # answerable — "you already sell this, here is where memory says so".
        found = state([
            Row("83838c9f", "Featured items. Dosirak (Set Meal) - 도시락세트 · "
                            "Set Meal · Appetizers · Grilled Pork.",
                "https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf"),
            Row("6945b86c", "Experience one of the most popular menu items among "
                            "Uber Eats users at this evening go-to: the Dosirak.",
                "https://www.ubereats.com/store/yellow-cow-korean-bbq/Bq_kf"),
            Row("d4533352", "Solid Korean BBQ With Good Service & Value Mar 2022. "
                            "Yellow Cow offered both dosirak and bibimbap.",
                "https://www.tripadvisor.com/Restaurant_Review-d828626-Yellow_Cow"),
            ], name=YELLOW_COW, address=YELLOW_COW_ADDRESS)

        dosirak = next(o for o in found.offers if o.name == "Dosirak (Set Meal)")
        assert dosirak.observation_ids == ("83838c9f", "6945b86c", "d4533352")
        assert dosirak.origin == "market"

    def test_a_menu_heading_above_a_price_is_an_offer(self):
        # 3649bd5e, places.singleplatform.com — Asaka's own menu.
        found = state(
            [Row("3649bd5e",
                 "## Main Menu\n\n### Appetizers\n\n#### Miso Soup\n\n $1.95\n\n"
                 "nourishing soup made of fermented soy beans pasta\n\n"
                 "#### Edamame\n\n $3.95",
                 "http://places.singleplatform.com/asaka-4/menu")],
            name=ASAKA, address=ASAKA_ADDRESS)

        assert {"Miso Soup", "Edamame"} <= {o.name for o in found.offers}

    def test_a_competitor_carousel_sells_this_business_nothing(self):
        # guide.michelin.com, verbatim. Every heading is somebody else.
        found = state(
            [Row("aae9e111",
                 "### Sushi Sonagi\n\nGardena, CA, USA\n\n$$$$ · Japanese\n\n"
                 "### Sweet Rice\n\nGardena, CA, USA\n\n$ · Thai",
                 "https://guide.michelin.com/us/en/california/restaurants")],
            name=ASAKA, address=ASAKA_ADDRESS)

        assert found.offers == ()

    def test_the_business_does_not_sell_itself(self):
        found = state(
            [Row("38bffe87", "## Location\n\n### Asaka\n\nOPEN NOW\n\n$10.00",
                 "https://www.asakacatogo.com")],
            name=ASAKA, address=ASAKA_ADDRESS)

        assert "Asaka" not in {o.name for o in found.offers}
        assert "Location" not in {o.name for o in found.offers}

    def test_a_transient_banner_is_not_something_the_business_sells(self):
        # 5193b576 and its two siblings, grubhub.com. "### This menu isn't
        # available right now" is a heading on the tenant's own storefront, so
        # every structural test passes it. What it is not is a product.
        found = state(
            [Row("5193b576",
                 "### This menu isn't available right now\n\n$12.00",
                 "https://www.grubhub.com/restaurant/yellow-cow-korean-bbq/2033337")],
            name=YELLOW_COW, address=YELLOW_COW_ADDRESS)

        assert found.offers == ()

    def test_a_heading_that_introduces_other_things_is_not_an_offer(self):
        # b8bbe899, maps.roadtrippers.com. "Related", "More", "Other", "Similar"
        # and "Nearby" open a list of somebody else's business, whatever the
        # carousel is called this week.
        found = state(
            [Row("b8bbe899", "### Related Trip Guides\n\n$12.00",
                 "https://maps.roadtrippers.com/us/torrance-ca/palsaik-korean-bbq")])

        assert found.offers == ()

    def test_a_rating_chip_disqualifies_the_list_it_sits_in(self):
        # 5d1f9db5, ubereats.com: "4.9 x (200+) • Japanese • Sushi • Asian •
        # Group Friendly" is the storefront's attribute strip. Read as a menu it
        # sold Asaka "Japanese" and "Asian", neither of which is a dish.
        found = state(
            [Row("5d1f9db5",
                 "4.9 x (200+) • Japanese • Sushi • Asian • Group Friendly",
                 "https://www.ubereats.com/store/asaka-sushi-%26-grill/CyXU")],
            name=ASAKA, address=ASAKA_ADDRESS)

        assert found.offers == ()

    def test_a_labelled_menu_list_still_gets_through(self):
        # The other half of the rule above: the label is what makes an
        # interpunct run a menu, and a labelled one needs no head-count.
        found = state(
            [Row("83838c9f", "Featured items. Dosirak (Set Meal) · Set Meal.",
                 "https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf")],
            name=YELLOW_COW, address=YELLOW_COW_ADDRESS)

        assert "Dosirak (Set Meal)" in {o.name for o in found.offers}

    def test_an_offer_can_be_looked_up_by_a_move_that_names_it(self):
        # What the Analyst rule needs: "launch a fixed-price lunch set" has to
        # be answerable against the inventory without a model call.
        found = state(
            [Row("83838c9f", "Featured items. Dosirak (Set Meal) · Set Meal · "
                             "Grilled Pork.",
                 "https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf")],
            name=YELLOW_COW, address=YELLOW_COW_ADDRESS)

        assert found.offer_named("Launch a weekday Dosirak lunch set").name == (
            "Dosirak (Set Meal)")
        assert found.offer_named("Add valet parking") is None


class TestTheBlockTheAnalystReads:
    def test_says_nothing_when_nothing_is_known(self):
        assert describe_business_state(state([])) == ""

    def test_names_the_declared_domain_and_the_undeclared_ones(self):
        found = state(
            [Row("a", "Hours of Operation (Takeout). Asaka.",
                 "https://www.asakacatogo.com")],
            name=ASAKA, address=ASAKA_ADDRESS,
            facts=["The business website is https://www.asakakaiyo.com/."])
        block = describe_business_state(found)

        assert "asakakaiyo.com" in block
        assert "asakacatogo.com" in block
        assert "did not declare" in block

    def test_lists_the_offers_with_their_evidence(self):
        found = state(
            [Row("83838c9f", "Featured items. Dosirak (Set Meal) · Set Meal.",
                 "https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf")],
            name=YELLOW_COW, address=YELLOW_COW_ADDRESS,
            facts=["What we sell is Korean BBQ."])
        block = describe_business_state(found)

        assert "Dosirak (Set Meal)" in block
        assert "83838c9f" in block
        assert "Korean BBQ" in block

    def test_reports_a_weekday_disagreement_as_a_disagreement(self):
        found = state([
            Row("a", "Hours Monday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
            Row("b", "⏰Open : Mon(11:30am-10pm)",
                "https://www.facebook.com/palsaik.california"),
        ])
        block = describe_business_state(found)

        assert "Monday" in block
        assert "disagree" in block

    def test_does_not_call_day_disjoint_sources_a_disagreement(self):
        found = state([
            Row("a", "Hours Monday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
            Row("b", "Open. Thursday : 11:30-21:30.",
                "https://www.fantuanorder.com/store/palsaik"),
        ])
        block = describe_business_state(found)

        assert "disagree" not in block
        assert "Thursday" in block

    def test_says_a_dayless_window_cannot_contradict_a_weekday(self):
        found = state([
            Row("a", "Hours Monday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
            Row("b", "Hours Today Pickup: 11:45am–8:45pm",
                "https://www.grubhub.com/restaurant/palsaik/2169422"),
        ])
        block = describe_business_state(found)

        assert "no weekday" in block

    def test_sources_that_agree_are_printed_as_one_reconciled_window(self):
        # The whole point of the word "reconciled". Four sources saying the same
        # thing is agreement, and listing it four times invites the model to
        # count it as four signals.
        found = state([
            Row("a", "Monday: 11:30 AM – 9:30 PM",
                "https://palsaikkoreanbbqca.com/location"),
            Row("b", "Monday: 11:30 AM – 9:30 PM",
                "https://m.yelp.com/biz/palsaik-korean-bbq-torrance"),
            Row("c", "Palsaik Korean BBQ. Monday: 11:30 AM – 9:30 PM",
                "https://maps.apple.com/place?place-id=ICBB126"),
        ])
        [line] = [row for row in describe_business_state(found).splitlines()
                  if row.startswith("- Monday")]

        assert line.count("11:30–21:30") == 1
        assert "3 sources" in line

    def test_one_row_is_not_one_rows(self):
        found = state([Row("a", "Palsaik Korean BBQ, 22757 Hawthorne Blvd.",
                           "https://palsaikkoreanbbqca.com/location")])

        assert "1 rows" not in describe_business_state(found)

    def test_stays_bounded_on_a_menu_the_size_of_a_real_one(self):
        # Asaka's Postmates storefront alone carries eighteen menu sections. The
        # block sits in every nightly prompt and cannot grow with the menu.
        rows = [Row(f"o{n}", f"#### Dish Number {n}\n\n $9.{n:02d}",
                    "https://www.asakacatogo.com/order-online")
                for n in range(60)]
        block = describe_business_state(state(rows, name=ASAKA, address=ASAKA_ADDRESS))

        assert len(block.splitlines()) < 40


class TestItReadsWhatTheRepositoryStores:
    """The state is built from stored rows, not from a retrieval slice."""

    @pytest.fixture()
    def seeded(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        business = repo.create_business(
            name=YELLOW_COW, category="restaurant", city=YELLOW_COW_ADDRESS)
        repo.insert_observation(
            business,
            content="Featured items. Dosirak (Set Meal) · Set Meal · Grilled Pork.",
            kind="trend", embedding=[1.0] + [0.0] * 1023,
            source_name="web",
            source_url="https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf",
            observed_at=CAPTURED)
        return repo, business

    def test_the_whole_corpus_is_readable_without_a_query_vector(self, seeded):
        repo, business = seeded

        rows = repo.all_observations(business, limit=500)

        assert [r.content for r in rows] == [
            "Featured items. Dosirak (Set Meal) · Set Meal · Grilled Pork."]
        assert rows[0].source_url.startswith("https://postmates.com/")

    def test_it_is_scoped_to_the_business(self, seeded):
        repo, business = seeded
        other = repo.create_business(name="Someone Else", category="restaurant",
                                     city="Torrance")

        assert repo.all_observations(other, limit=500) == []

    def test_the_state_built_from_it_knows_about_the_set_meal(self, seeded):
        repo, business = seeded

        found = build_business_state(
            business=repo.get_business(business),
            facts=repo.get_business_facts(business),
            observations=repo.all_observations(business, limit=500),
        )

        assert "Dosirak (Set Meal)" in {o.name for o in found.offers}
