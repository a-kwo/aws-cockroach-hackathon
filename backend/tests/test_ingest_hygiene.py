"""Ingest hygiene and statement typing — what Radar refuses to remember.

Every rule here was written against the 124 web observations that were actually
in the cluster on 2026-08-03, not against an imagined page. The three failures
they exist to stop:

* **Letter-shredding.** Grubhub renders its fee banner one character per line, so
  "No fees when your items total $50+" reached the embedder as thirty tokens of
  one character each.
* **Page furniture.** Roughly 40% of each tenant's corpus was navigation, cookie
  text, footers and "people also viewed" carousels. Grubhub's carousel put Pho
  So 1, Chick-fil-A and L&L Hawaiian *inside Yellow Cow's own rows*, where the
  Analyst could read a competitor's rating as the tenant's.
* **One page, three rows.** The three Grubhub captures behind find 7c4a9124
  differed only in which slice of the carousel they caught. Strip the carousel
  and they are one page, which is what the Analyst should have been told.

Typing is separate from `kind` on purpose. `kind` says where a row came from;
`statement_type` says what it asserts, and only the second can tell a staleness
rule that "This menu isn't available right now" expires in minutes while
"Monday 11:30 am - 8:30 pm" is still true next month.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brasstacks.agents.radar import run_radar
from brasstacks.providers import FakeEmbedder
from brasstacks.repository import InMemoryRepository
from brasstacks.signals import (
    STATEMENT_TYPES,
    RawSignal,
    classify_statement,
    clean_observation_text,
    repair_shredded_text,
)

# The real storefront, as Radar stored it three times over.
GRUBHUB_URL = ("https://www.grubhub.com/restaurant/"
               "yellow-cow-korean-bbq-1835-w-redondo-beach-blvd-gardena/2033337")
YELLOW_COW = "Yellow Cow Korean BBQ"
YELLOW_COW_ADDRESS = "1835 W Redondo Beach Blvd, Gardena, CA 90247, United States"


def clean(content, *, name=YELLOW_COW, address=YELLOW_COW_ADDRESS, url=GRUBHUB_URL):
    return clean_observation_text(content, business_name=name, address=address,
                                  source_url=url)


class TestRepairShredding:
    def test_joins_a_long_run_of_single_characters(self):
        # Verbatim from the Grubhub storefront: "\n\n" between every letter.
        shredded = "\n\n".join("Nofeeswhenyouritemstotal$50+")
        assert repair_shredded_text(shredded) == "Nofeeswhenyouritemstotal$50+"

    def test_leaves_the_rest_of_the_line_alone(self):
        text = "service fee (max $7.50)\n\n" + "\n\n".join("Nofees") + "\n\nPreorder"
        assert repair_shredded_text(text) == (
            "service fee (max $7.50)\n\nNofees\n\nPreorder")

    def test_does_not_eat_an_initialism(self):
        # "L & L Hawaiian" and "1835 W Redondo" both put single characters next
        # to each other. Eating those would invent words nobody wrote.
        assert repair_shredded_text("L & L Hawaiian") == "L & L Hawaiian"
        assert repair_shredded_text("1835 W Redondo Beach Blvd") == (
            "1835 W Redondo Beach Blvd")
        assert repair_shredded_text("a b c d e") == "a b c d e"

    def test_repairs_before_anything_else_sees_the_text(self):
        # Dedup depends on this: a repaired and an unrepaired copy of one page
        # must not both be storable.
        shredded = "\n\n".join("Preorder")
        assert clean(shredded) == clean("Preorder")


class TestPageChrome:
    def test_strips_navigation_and_sign_in(self):
        body = ("Enter an address\n\nSearch restaurants or dishes\n\nSign in\n\n"
                "Skip to NavigationSkip to AboutSkip to FooterSkip to Cart\n\n"
                "# Yellow Cow Korean BBQ")
        assert clean(body) == "# Yellow Cow Korean BBQ"

    def test_strips_footer_and_consent_text(self):
        body = ("# Yellow Cow Korean BBQ\n\nTerms of use\n\nPrivacy policy\n\n"
                "Do not sell\n\nWe use cookies to personalise content\n\n"
                "© 2026 MapQuestLegal")
        assert clean(body) == "# Yellow Cow Korean BBQ"

    def test_strips_the_people_also_viewed_carousel(self):
        # Grubhub's carousel, which is where Pho So 1 and Chick-fil-A got into
        # Yellow Cow's rows.
        body = ("# Yellow Cow Korean BBQ\n\n4.9\n\n257 ratings\n\n"
                "### Picked for you\n\n##### Chick-fil-A\n\n5.0 (754)\n\n"
                "American\n\n• 12 min • 1.0 mi\n\n##### Pho So 1\n\n4.6 (470)")
        cleaned = clean(body)
        assert "Chick-fil-A" not in cleaned
        assert "Pho So 1" not in cleaned
        assert "257 ratings" in cleaned

    def test_a_carousel_section_ends_at_the_next_heading_of_its_level(self):
        # singleplatform.com puts "## Browse Nearby" immediately before
        # "## General Info", which carries the address and the opening hours.
        # Skipping to the end of the fragment would throw those away.
        body = ("## Browse Nearby\n\nSome other place\n\n## General Info\n\n"
                "31208 Palos Verdes Dr W\n\nMonday: 9:00am - 9:00pm")
        cleaned = clean(body, name="Asaka",
                        address="31208 Palos Verdes Dr W, Rancho Palos Verdes, CA 90275",
                        url="http://places.singleplatform.com/asaka-4/menu")
        assert "Some other place" not in cleaned
        assert "Monday: 9:00am - 9:00pm" in cleaned

    def test_the_three_grubhub_captures_collapse_to_one(self):
        # The whole point. Two captures of one storefront that differ only in
        # which slice of the carousel they caught must hash to one row.
        head = ("Preorder for 6:30pm.\n\n# Yellow Cow Korean BBQ\n\n4.9\n\n"
                "257 ratings\n\n1835 W Redondo Beach Blvd")
        first = head + " [...] 4.8 (632)\n\nPizza\n\n• 14 min • 0.6 mi\n\n4.4 (631)"
        second = head + " [...] 5.0 (385)\n\nPizza\n\n• 20 min • 1.2 mi\n\n4.9 (550)"
        assert clean(first) == clean(second) != ""


class TestOffTopicFragments:
    def test_keeps_a_fragment_that_names_the_business(self):
        assert "Yellow Cow" in clean("Yellow Cow Korean BBQ has some of the best "
                                     "Korean in Gardena.")

    def test_keeps_a_fragment_that_gives_the_street_address(self):
        body = "1835 W Redondo Beach Blvd, Gardena, CA, 90247. The menu boasts barbeque."
        assert clean(body) == body

    def test_keeps_a_review_that_names_nobody(self):
        # Tripadvisor excerpts routinely omit the restaurant. Dropping these
        # would throw away the most useful rows in the corpus.
        body = "Very nice quality of various meats. Very friendly staffs."
        assert clean(body) == body

    def test_drops_a_fragment_about_a_different_business(self):
        # realtor.com came back for Yellow Cow because it shares the street
        # number 1835. It is a condo four blocks away, so the street *name* has
        # to match too.
        body = ("See 1835 W 145th St Apt 4, Gardena, CA 90249, a condo located in "
                "the South Bay neighborhood.")
        assert clean(body, url="https://www.realtor.com/realestateandhomes-detail/"
                               "1835-W-145th-St-Apt-4") == ""

    def test_drops_a_directory_listing_of_rivals(self):
        body = ("### Sushi Sonagi\n\nGardena, CA, USA\n\n$$$$ · Japanese\n\n"
                "### Sweet Rice\n\nGardena, CA, USA\n\n$ · Thai")
        assert clean(body, name="Asaka",
                     address="31208 Palos Verdes Dr W, Rancho Palos Verdes, CA 90275",
                     url="https://guide.michelin.com/us/en/california/"
                         "rancho-palos-verdes/restaurants") == ""

    def test_a_tenant_page_keeps_its_own_uncredited_copy(self):
        # The tenant's own Postmates storefront lists menu sections and names
        # nobody — but the URL says whose page it is, so it stays.
        body = ("Featured items. Dosirak (Set Meal) · Appetizers · Specialties · "
                "Fried Rice & Bibimbap · Soups & Noodles")
        url = "https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf"
        assert clean(body, url=url) == body

    def test_but_not_the_carousel_on_that_same_page(self):
        # The URL naming the tenant must not launder a block of rival cards.
        body = ("4.8 (632)\n\nPizza\n\n• 14 min • 0.6 mi\n\n4.4 (631)\n\nHawaiian\n\n"
                "• 23 min • 2.7 mi\n\n5.0 (754)\n\nAmerican\n\n• 12 min • 1.0 mi")
        assert clean(body) == ""

    def test_a_row_that_is_all_furniture_becomes_empty(self):
        assert clean("Sign in\n\nAdvertisement\n\nDownload on the App Store") == ""

    def test_hygiene_needs_no_profile_to_be_safe(self):
        # A tenant with no address on file still gets chrome stripped, and a
        # fragment is never dropped for failing an address test we cannot run.
        body = "Sign in\n\nSome text about nothing in particular."
        assert clean(body, name="", address=None, url=None) == (
            "Some text about nothing in particular.")


class TestStatementTyping:
    @pytest.mark.parametrize("text,expected", [
        # page_state first: it is the label the outage claim needed.
        ("### This menu isn't available right now", "page_state"),
        ("Preorder for 6:30pm.", "page_state"),
        ("x Delivery unavailable\n\nToo far to deliver", "page_state"),
        ("Closed now", "page_state"),
        # Durable facts that a staleness rule must NOT treat as perishable.
        ("Hours of Operation (Takeout)\n\nMonday - Thursday\n\n11:30 am - 08:30 pm",
         "hours"),
        ("Sunday 11:30 am - 8:30 pm\nMonday 11:30 am - 8:00 pm", "hours"),
        ("### Spring roll\n\n4pcs of fried vegetable egg rolls with sweet & sour "
         "sauce\n\n$10.00\n\n### Tempura\n\n2pcs battered shrimp\n\n$12.00",
         "menu_item"),
        # Price level, not a dish.
        ("Dining style. Casual Dining ; Price. $30 and under ; Cuisines. Japanese",
         "price"),
        ("Entrees $10-$22.", "price"),
        ("Very nice quality of various meats. Very friendly staffs.", "review"),
        ("I paid $51 for 3 rolls and they were so small. Won't order again.",
         "review"),
        ("Discover the taste of Japan at Asaka! Indulge in our exquisite sushi, "
         "crafted with the freshest ingredients. Place your online order now!",
         "marketing"),
        # Platform text, true of every listing on the site and of no business.
        ("Yellow Cow Korean BBQ in Gardena, Prices may be lower in-store and may "
         "vary between pickup and delivery.", "boilerplate"),
        ("qMenu Free online ordering website. Browse local restaurants, delivery "
         "menus, coupons and reviews.", "boilerplate"),
        ("Javascript is needed to run Uber Eats.", "boilerplate"),
        # The directory card. Not in the suggested vocabulary, but 18 of the
        # 109 rows that survive hygiene are exactly this and nothing else:
        # name, address, phone, and what the platform lets you do about it.
        # Durable, like hours — the label exists so it is not filed as unknown.
        ("More info about Asaka Sushi & Grill. 31208 Palos Verdes Dr W. Rancho "
         "Palos Verdes, CA 90275. Directions. (310) 377-5999. Call Now · Order "
         "Takeout or Delivery.", "listing"),
        ("Asaka Sushi & Grill, Rancho Palos Verdes. 31208 Palos Verdes Dr W, "
         "Rancho Palos Verdes, CA, United States, 90275. In-store pickup",
         "listing"),
        ("Asaka stands out as a culinary gem in Rancho Palos Verdes, offering an "
         "extraordinary dining experience that captivates sushi lovers.",
         "marketing"),
    ])
    def test_labels(self, text, expected):
        assert classify_statement(text) == expected

    def test_unrecognised_text_is_unknown_not_guessed(self):
        # NULL and 'unknown' both mean "we did not decide". Neither may be read
        # as a claim about the row.
        assert classify_statement("Rectangle") == "unknown"
        assert classify_statement("") == "unknown"

    def test_a_transient_line_makes_the_whole_row_perishable(self):
        # A storefront capture carries fees, a rating and the transient banner
        # at once. It has to be labelled by the perishable part: the label
        # exists so a staleness rule can refuse to reason from that banner.
        body = ("# Yellow Cow Korean BBQ\n\n4.9\n\n257 ratings\n\n"
                "$0 delivery fee • 15% service fee (max $7.50)\n\n"
                "### This menu isn't available right now")
        assert classify_statement(body) == "page_state"

    def test_hours_are_not_perishable_just_because_a_day_is_closed(self):
        # "Tuesday: Closed" is an opening-hours fact. Reading it as page_state
        # would blocklist the hours table, which is the failure typing exists
        # to prevent.
        assert classify_statement(
            "Mon 11:00 am - 9:30 pm\nTuesday: Closed\nWed 11:00 am - 9:30 pm"
        ) == "hours"

    def test_every_label_is_in_the_published_vocabulary(self):
        assert "unknown" in STATEMENT_TYPES
        for text in ("Closed now", "Monday 11:30 am - 8:30 pm", "Entrees $10-$22.",
                     "Very friendly staffs.", "In-store pickup", "Rectangle"):
            assert classify_statement(text) in STATEMENT_TYPES

    def test_a_diner_naming_a_dish_is_still_a_review(self):
        # A tie between "somebody is describing their visit" and "a dish word
        # appeared" goes to the person. Verbatim from Tripadvisor: two review
        # words, two menu words, and it is plainly a review.
        assert classify_statement(
            "Solid Korean BBQ in the South Bay. Great value for the two meat "
            "combo that includes a choice of soup & either big beer, bottle of "
            "soju or two sodas.") == "review"

    def test_a_phone_number_does_not_outrank_an_hours_table(self):
        # Every directory card carries a phone number, and most opening-hours
        # pages carry one too. Reading the phone as the point of the row would
        # file the hours as a listing and lose the durable fact.
        assert classify_statement(
            "⏰ Mon, Wed, Sun - 11AM~9PM Tue - Closed Thur, Sat - 11AM~9:30PM "
            "(310) 329-7343 1835 W Redondo Beach Blvd") == "hours"

    def test_classification_does_not_depend_on_surrounding_whitespace(self):
        """Replaces a test that called classify_statement twice on one string
        and asserted the answers matched. A pure function of a string with no
        clock and no randomness cannot fail that, including an implementation
        that returns "unknown" for everything — it was a test that could not
        go red. This asserts something a real change could break."""
        body = "Sunday 11:30 AM – 8:30 PM"

        assert classify_statement(body) == "hours"
        assert classify_statement(f"  \n{body}\t ") == "hours"
        assert classify_statement(body.replace(" ", " ")) == "hours"


# ---------------------------------------------------------------------------
# Radar wiring — hygiene has to happen before the hash and before the spend
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)


class StubSource:
    name = "web"
    retention_hours = None

    def __init__(self, signals):
        self._signals = signals

    def fetch(self, *, business_name, city, limit):
        return list(self._signals)


def web_signal(content, url=GRUBHUB_URL):
    return RawSignal(content=content, kind="trend", source_name="web",
                     source_url=url, observed_at=NOW)


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def business(repo):
    return repo.create_business(name=YELLOW_COW, category="restaurant",
                                city=YELLOW_COW_ADDRESS)


def sweep(repo, business, signals, embedder=None):
    return run_radar(repo=repo, embedder=embedder or FakeEmbedder(),
                     business_id=business, sources=[StubSource(signals)], now=NOW,
                     business_name=YELLOW_COW, city=YELLOW_COW_ADDRESS)


class TestRadarHygiene:
    def test_two_captures_of_one_page_store_one_row(self, repo, business):
        head = ("Sign in\n\n# Yellow Cow Korean BBQ\n\n257 ratings\n\n"
                "1835 W Redondo Beach Blvd")
        result = sweep(repo, business, [
            web_signal(head + " [...] 4.8 (632)\n\nPizza\n\n• 14 min • 0.6 mi"
                              "\n\n4.4 (631)"),
            web_signal(head + " [...] 5.0 (385)\n\nPizza\n\n• 20 min • 1.2 mi"
                              "\n\n4.9 (550)"),
        ])
        assert result.stored == 1
        assert repo.count_observations(business) == 1

    def test_the_stored_text_is_the_repaired_text(self, repo, business):
        sweep(repo, business, [web_signal(
            "Sign in\n\n# Yellow Cow Korean BBQ\n\n" + "\n\n".join("Nofees"))])
        [stored] = repo.search_observations(business, FakeEmbedder().embed(["q"])[0],
                                            limit=5)
        assert "Sign in" not in stored.content
        assert "Nofees" in stored.content

    def test_a_row_hygiene_empties_is_never_embedded(self, repo, business):
        # The spend matters: ~50 embeddings a night per tenant, and furniture
        # was 40% of the corpus.
        embedder = FakeEmbedder()
        result = sweep(repo, business, [
            web_signal("Sign in\n\nAdvertisement\n\nDownload on the App Store"),
            web_signal("Very nice quality of various meats. Very friendly staffs."),
        ], embedder=embedder)
        assert embedder.embedded == [
            "Very nice quality of various meats. Very friendly staffs."]
        assert result.stored == 1
        assert result.dropped == 1
        assert "1 dropped as page furniture" in result.note

    def test_an_owner_upload_is_never_second_guessed(self, repo, business):
        # Hygiene is for text scraped off somebody else's page. A row the owner
        # typed has no URL, no chrome, and no business being dropped for
        # failing to name itself.
        result = run_radar(
            repo=repo, embedder=FakeEmbedder(), business_id=business, now=NOW,
            business_name=YELLOW_COW, city=YELLOW_COW_ADDRESS,
            sources=[StubSource([RawSignal(content="Sign in", kind="owner_upload",
                                           observed_at=NOW)])],
        )
        assert result.stored == 1

class TypedRepository(InMemoryRepository):
    """Stands in for the repository once it accepts the new column."""

    def __init__(self):
        super().__init__()
        self.statement_types = []

    def insert_observation(self, business_id, *, statement_type=None, **kwargs):
        self.statement_types.append(statement_type)
        return super().insert_observation(business_id, **kwargs)


class TestRadarTyping:
    def test_radar_labels_what_it_stores(self):
        repo = TypedRepository()
        business = repo.create_business(name=YELLOW_COW, category="restaurant",
                                        city=YELLOW_COW_ADDRESS)
        sweep(repo, business, [web_signal(
            "Sunday 11:30 AM - 8:30 PM\nMonday 11:30 AM - 8:00 PM",
            url="https://www.toasttab.com/local/order/yellow-cow-korean-bbq")])
        assert repo.statement_types == ["hours"]

    def test_a_repository_without_the_column_still_runs_the_night(self, repo,
                                                                  business):
        # db/schema.sql and the write path do not deploy in lockstep. A night
        # against a repository that has not learned the column must store the
        # observation unlabelled, not fail — the same hazard that cost us the
        # ledger once already.
        result = sweep(repo, business, [
            web_signal("Very nice quality of various meats. Very friendly staffs.")])
        assert result.stored == 1


class TestStatementTypeColumn:
    """The column has to exist in both places, or a deploy outruns the night.

    `db/schema.sql` is the canonical migration and `DECISION_SCHEMA_STATEMENTS`
    is what a rolling Lambda applies at request time. Adding a column to one
    and not the other is the mistake that cost us the ledger in August.
    """

    def _schema_sql(self):
        from pathlib import Path

        from brasstacks.config import REPO_ROOT

        return (Path(REPO_ROOT) / "db" / "schema.sql").read_text(encoding="utf-8")

    def test_schema_sql_declares_it(self):
        assert "statement_type" in self._schema_sql()

    def test_schema_sql_adds_it_to_a_cluster_that_already_exists(self):
        # CREATE TABLE IF NOT EXISTS does nothing to a live table, and all three
        # tenants' observation tables predate this column.
        assert ("ALTER TABLE observation ADD COLUMN IF NOT EXISTS statement_type"
                in self._schema_sql())

    def test_the_runtime_bootstrap_adds_it_too(self):
        from brasstacks.decision_schema import DECISION_SCHEMA_STATEMENTS

        assert any("statement_type" in statement
                   for statement in DECISION_SCHEMA_STATEMENTS)

    def test_the_migration_is_additive(self):
        # Nothing here may break a cluster that has not been migrated, and
        # nothing may claim a label for the 147 rows written before typing
        # existed. NULL means "we never looked" and has to stay available.
        from brasstacks.decision_schema import DECISION_SCHEMA_STATEMENTS

        [added] = [s for s in DECISION_SCHEMA_STATEMENTS
                   if "ADD COLUMN IF NOT EXISTS statement_type" in s]
        assert "NOT NULL" not in added
        assert "DEFAULT" not in added


class TestTheLabelSurvivesTheWrite:
    """Radar classified every row and the repository threw the answer away.

    The column landed, `classify_statement` landed and was tested against 124
    real rows, and Radar computed a label for every observation — and then
    `insert_observation` did not accept the keyword, so an `inspect.signature`
    guard in Radar quietly took the branch that drops it. Every row would have
    been written NULL. The feature reported as shipped and was inert, and the
    test covering it asserted against a subclass defined in the test file whose
    only purpose was to have the signature the real repositories lacked.
    """

    def test_the_repository_stores_what_radar_classified(self, repo):
        business = repo.create_business(name="Yellow Cow", category="restaurant")

        observation_id = repo.insert_observation(
            business,
            content="Monday 11:30 am - 8:30 pm",
            kind="trend",
            embedding=FakeEmbedder().embed(["x"])[0],
            observed_at=NOW,
            statement_type="hours",
        )

        [stored] = repo.all_observations(business, limit=50)
        assert stored.observation_id == observation_id
        assert stored.statement_type == "hours"

    def test_an_unclassified_row_is_null_not_guessed(self, repo):
        """NULL means nobody looked. It must never be a default label."""
        business = repo.create_business(name="Yellow Cow", category="restaurant")

        repo.insert_observation(
            business, content="something", kind="trend",
            embedding=FakeEmbedder().embed(["x"])[0], observed_at=NOW)

        [stored] = repo.all_observations(business, limit=50)
        assert stored.statement_type is None

    def test_radar_labels_the_rows_it_actually_stores(self, repo):
        """The end-to-end version, against the real repository rather than a
        double built to have the signature the real one was missing."""
        business = repo.create_business(name="Yellow Cow", category="restaurant")
        # Names the business, or the hygiene filter drops it as somebody else's
        # page before typing ever gets a look — which is the correct behaviour
        # and was worth learning here rather than in production.
        source = _TypingSource("web", [
            RawSignal(content="Yellow Cow hours: Monday 11:30 am - 8:30 pm",
                      kind="trend", source_name="web",
                      source_url="https://example.com/hours", observed_at=NOW),
        ])

        run_radar(repo=repo, embedder=FakeEmbedder(), business_id=business,
                  sources=[source], now=NOW, business_name="Yellow Cow")

        assert [o.statement_type for o in repo.all_observations(business, limit=50)] == ["hours"]


class _TypingSource:
    def __init__(self, name, signals):
        self.name = name
        self._signals = signals

    def fetch(self, *, business_name, city, limit):
        return list(self._signals)


