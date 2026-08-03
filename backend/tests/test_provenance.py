"""Where an observation came from, as something two rows can share.

Written because find 7c4a9124 opened with "Three separate captures of your
delivery storefront (a9728078, 5193b576, f5457ff3) all show..." â€” three
overlapping fragments of ONE fetch of ONE Grubhub page, presented to the model
as three independent signals. A fourth cited row was the same store on
seamless.com, which is Grubhub under another name, store id 2033337 in both
URLs. Four of the five cited rows were one storefront.

The Analyst could not have known: nothing downstream of the fetch carried the
URL. This module is the missing identity, kept pure and out of SQL so the rule
that merges a mirror can be argued with in a test rather than in a query plan.
"""

from __future__ import annotations

from brasstacks.provenance import source_host, source_identity


class TestSourceHost:
    def test_the_host_is_what_an_owner_would_call_the_site(self):
        assert source_host("https://www.grubhub.com/restaurant/rosas/2033337") == "grubhub.com"

    def test_scheme_case_and_port_do_not_change_the_host(self):
        assert source_host("HTTPS://Yelp.com:443/biz/rosas") == "yelp.com"

    def test_a_row_with_no_url_has_no_host(self):
        assert source_host(None) is None
        assert source_host("   ") is None

    def test_something_that_is_not_a_web_page_has_no_host(self):
        # Radar only ever stores http(s), but an owner upload or a hand-seeded
        # row can carry anything, and a mailto: is not a source to group by.
        assert source_host("mailto:owner@example.com") is None


class TestSourceIdentity:
    def test_two_captures_of_one_page_are_one_source(self):
        # The demonstrated defect: three rows, one URL, one fetch.
        url = "https://www.grubhub.com/restaurant/rosas-trattoria/2033337"
        assert source_identity(url) == source_identity(url)
        assert source_identity(url) is not None

    def test_a_tracking_parameter_does_not_split_one_page(self):
        plain = "https://grubhub.com/restaurant/rosas/2033337"
        tagged = "https://grubhub.com/restaurant/rosas/2033337?utm_source=email#menu"
        assert source_identity(tagged) == source_identity(plain)

    def test_case_and_a_trailing_slash_do_not_split_one_page(self):
        assert (source_identity("https://Example.com/Blog/Columbus-Dining/")
                == source_identity("http://www.example.com/blog/columbus-dining"))

    def test_a_mirror_is_the_same_storefront(self):
        # Seamless IS Grubhub. Same store id, same menu, same prices â€” and the
        # find treated them as two independent confirmations of one problem.
        grubhub = source_identity("https://www.grubhub.com/restaurant/rosas-trattoria/2033337")
        seamless = source_identity("https://www.seamless.com/menu/rosas-trattoria/2033337")
        assert grubhub == seamless

    def test_two_stores_on_one_platform_stay_apart(self):
        # The cap this feeds must not collapse a rival's storefront into ours.
        ours = source_identity("https://www.grubhub.com/restaurant/rosas/2033337")
        theirs = source_identity("https://www.grubhub.com/restaurant/luccas/9911223")
        assert ours != theirs

    def test_a_store_number_reused_by_an_unrelated_host_stays_apart(self):
        # A shared trailing number is only evidence of one storefront within a
        # platform that is known to mirror itself. Across unrelated hosts it is
        # a coincidence, and merging on it would hide a real second source.
        grubhub = source_identity("https://www.grubhub.com/restaurant/rosas/2033337")
        doordash = source_identity("https://www.doordash.com/store/rosas/2033337")
        assert grubhub != doordash

    def test_a_page_number_is_not_a_store_id(self):
        # /page/2 and /page/3 are two pages of one listing, but a two-digit
        # number is far more likely pagination than a store, and reading it as a
        # store id would merge unrelated pages across a whole site.
        assert (source_identity("https://example.com/reviews/page/2")
                != source_identity("https://example.com/reviews/page/3"))

    def test_a_row_with_no_url_has_no_identity(self):
        # None means "ungrouped", not "grouped with every other URL-less row".
        # The seeded corpus and every owner upload have no URL; giving them a
        # shared identity would hand the per-source cap most of memory.
        assert source_identity(None) is None
        assert source_identity("") is None


def test_a_shared_number_does_not_merge_two_pages_on_an_ordinary_site():
    """The store-id rule is a claim about mirror platforms, and the docstring
    says so â€” but it was applied to every host. A four-digit YEAR is the most
    common trailing segment on editorial URLs, which is exactly what Radar's
    trends query returns, so two unrelated articles on one magazine silently
    became one source and lost a cap slot to each other."""
    guides = source_identity("https://eater.com/guides/2026")
    news = source_identity("https://eater.com/news/2026")

    assert guides != news


def test_the_store_id_rule_still_merges_a_known_mirror():
    """The case it exists for must survive the narrowing."""
    assert (source_identity("https://www.grubhub.com/restaurant/x/2033337")
            == source_identity("https://www.seamless.com/menu/x/2033337"))

