"""The site build is production code, so it gets tests like production code.

Two kinds of test here. The first kind is ordinary: money formats, prose
splitting, chart shape. The second kind guards the honesty invariants — the
things the UI is not allowed to claim. Those exist because every one of them was
violated by the mock this frontend grew out of:

  * a growth chart with five invented future bars and one real one
  * "actual" figures on rows that were only ever modelled
  * a projection that grew whenever the owner answered a chat question

A regression on any of those is not a cosmetic bug. It is the product telling
the owner something untrue about their own money.
"""

from __future__ import annotations

import copy
import json
import os
import re

import pytest

import build_web
from brasstacks.agents import analyst, ask
from brasstacks.agents.analyst import ANALYST_QUERIES


# --------------------------------------------------------------- fixtures


def find(**over):
    base = {
        "id": "11111111-2222-3333-4444-555555555555",
        "emoji": "🍰",
        "title": "Tiramisu → $9",
        "rationale": "Because.",
        "move": "Reprice tiramisu from $7.00 to $9.00.",
        "predicted_daily_cents": 2300,
        "confidence": 0.88,
        "verify_after": "2026-07-01",
        "status": "live",
        "created_at": "2026-06-01T02:00:00+00:00",
        "verdict": "verified",
        "actual_daily_cents": 2500,
        "method": "terminal sales",
        "note": "233 sold.",
        "measured_at": "2026-06-24T06:00:00+00:00",
        "period_start": "2026-06-01",
        "period_end": "2026-06-15",
        "evidence": [{
            "rank": 0, "similarity": 0.702,
            "observation_id": "obs-1", "content": "Best tiramisu in the city.",
            "kind": "review", "source_name": "review_site", "subject": None,
            "observed_at": "2026-06-02T19:00:00+00:00",
        }],
    }
    base.update(over)
    return base


@pytest.fixture
def data():
    return {
        "business": {
            "id": "b", "name": "Rosa's Trattoria", "category": "restaurant",
            "city": "Columbus", "region": "OH",
            "goal_monthly_cents": 800000, "goal_note": "by fall",
        },
        "summary": {"verified": 1, "estimated": 0, "miss": 0,
                    "verified_daily_cents": 2500, "hit_rate": 1.0, "judged": 1},
        "corpus": {"observations": 127, "earliest": None, "latest": None},
        "finds": [find()],
        "runs": [],
        "monthly": [{"month": "2026-06-01", "verified_daily_cents": 2500,
                     "verified": 1, "miss": 0}],
        "kinds": [{"kind": "review", "count": 79}],
        "ratings": [{"week": "2026-06-01", "avg_rating": 4.14, "reviews": 7}],
    }


# --------------------------------------------------------------- formatting


@pytest.mark.parametrize("cents,expected", [
    (0, "$0"), (2300, "$23"), (2350, "$23.50"), (126_50, "$126.50"),
    (800_000, "$8,000"),
])
def test_money_is_formatted_from_integer_cents(cents, expected):
    assert build_web.money(cents) == expected


@pytest.mark.parametrize("cents,expected", [
    (99900, "$999"), (100000, "$1.0k"), (186000, "$1.9k"),
])
def test_short_money_for_tight_labels(cents, expected):
    assert build_web.short_money(cents) == expected


def test_clamp_never_cuts_mid_word():
    """Road labels used to shear titles mid-word — 'he Saturday' with no T."""
    out = build_web.clamp("The Saturday waitlist problem", 14)
    assert out.endswith("…")
    assert "Saturda…" not in out          # no partial word before the ellipsis
    assert out.replace("…", "").strip() in "The Saturday waitlist problem"


# --------------------------------------------------------------- agent prose


def test_bullets_splits_an_enumerated_move():
    """Trailing list punctuation is dropped — a bullet that ends in a semicolon
    reads as a fragment of a sentence that is not there."""
    out = build_web.bullets("Do this: (1) reprint the card; (2) brief the staff.")
    assert out == ["Do this:", "reprint the card", "brief the staff."]


def test_bullets_splits_sentences_when_not_enumerated():
    out = build_web.bullets("Reprice the tiramisu. Reprint the dessert card.")
    assert out == ["Reprice the tiramisu.", "Reprint the dessert card."]


def test_bullets_keeps_a_single_sentence_whole():
    """The previous build truncated at the first clause and dropped the rest."""
    text = "Raise pasta mains by $2.00 across the board and reprint the menu."
    assert build_web.bullets(text) == [text]


def test_bullets_of_nothing_is_empty():
    assert build_web.bullets("") == []
    assert build_web.bullets(None) == []


# --------------------------------------------------------------- view model


def test_open_finds_are_ordered_by_value_not_similarity(data):
    """Leading with the best-retrieved find would imply a closer match is a
    better bet. In the real corpus it is not — see the honesty tests below."""
    small = find(id="a" * 36, status="proposed", verdict=None,
                 actual_daily_cents=None, measured_at=None,
                 predicted_daily_cents=1000)
    small["evidence"][0]["similarity"] = 0.9
    big = find(id="b" * 36, status="proposed", verdict=None,
               actual_daily_cents=None, measured_at=None,
               predicted_daily_cents=6200)
    big["evidence"][0]["similarity"] = 0.1
    data["finds"] = [small, big]

    model = build_web.build_model(data)
    assert model["proposed"] == ["b" * 8, "a" * 8]


def test_finds_are_split_by_what_the_owner_must_do(data):
    data["finds"] = [
        find(id="1" * 36, status="proposed", verdict=None, actual_daily_cents=None),
        find(id="2" * 36, status="later", verdict=None, actual_daily_cents=None),
        find(id="3" * 36, status="live", verdict=None, actual_daily_cents=None),
        find(id="4" * 36, status="live", verdict="verified"),
    ]
    model = build_web.build_model(data)
    assert model["proposed"] == ["1" * 8]
    assert model["saved"] == ["2" * 8]
    assert model["measuring"] == ["3" * 8]
    assert model["judged"] == ["4" * 8]


def test_status_line_replaces_three_contradicting_claims(data):
    data["finds"] = [
        find(id="1" * 36, status="proposed", verdict=None, actual_daily_cents=None),
        find(id="3" * 36, status="live", verdict=None, actual_daily_cents=None),
    ]
    model = build_web.build_model(data)
    assert model["statusLine"] == "$25/day earning now · 1 waiting on you · 1 still measuring"


def test_evidence_keeps_its_similarity_and_rank_order(data):
    model = build_web.build_model(data)
    [f] = model["finds"]
    assert f["evidenceCount"] == 1
    assert f["topSimilarity"] == 0.702
    assert f["evidence"][0]["similarity"] == 0.702


# --------------------------------------------------- the honesty invariants


def test_real_months_are_never_marked_projected(data):
    model = build_web.build_model(data)
    real = [m for m in model["months"] if not m["projected"]]
    assert len(real) == 1
    assert real[0]["cents"] == 2500 * build_web.PER_MONTH


def test_at_most_one_projected_month(data):
    """The mock drew five invented futures beside one real bar."""
    data["finds"].append(find(id="9" * 36, status="live", verdict=None,
                              actual_daily_cents=None, measured_at=None,
                              predicted_daily_cents=9700))
    data["monthly"].append({"month": "2026-07-01", "verified_daily_cents": 3000,
                            "verified": 2, "miss": 1})
    model = build_web.build_model(data)
    assert sum(1 for m in model["months"] if m["projected"]) == 1


def test_no_projection_without_something_actually_being_measured(data):
    """A forecast has to be a forecast *of* something. With nothing pending
    there is nothing to project, so the chart shows only the record."""
    model = build_web.build_model(data)
    assert model["measuring"] == []
    assert [m for m in model["months"] if m["projected"]] == []


def test_the_projection_names_what_it_is_waiting_on(data):
    data["finds"].append(find(id="9" * 36, status="live", verdict=None,
                              actual_daily_cents=None, measured_at=None))
    model = build_web.build_model(data)
    [proj] = [m for m in model["months"] if m["projected"]]
    assert "still being measured" in proj["note"]


def test_only_verified_money_counts_toward_the_daily_rate(data):
    """An estimate must never be laundered into the headline figure. The
    summary comes from SQL that filters on verdict = 'verified'."""
    data["summary"]["estimated"] = 3
    model = build_web.build_model(data)
    assert model["summary"]["dailyCents"] == 2500


def test_a_find_with_no_outcome_reports_no_actual(data):
    data["finds"] = [find(status="live", verdict=None, actual_daily_cents=None,
                          measured_at=None)]
    model = build_web.build_model(data)
    [f] = model["finds"]
    assert f["actualDaily"] is None
    assert f["actualDailyTxt"] is None


def test_a_miss_survives_into_the_model(data):
    """The one thing the UI is never allowed to quietly drop."""
    data["finds"] = [find(verdict="miss", actual_daily_cents=0,
                          note="No measurable lift.")]
    data["summary"] = {"verified": 0, "estimated": 0, "miss": 1,
                       "verified_daily_cents": 0, "hit_rate": 0.0, "judged": 1}
    model = build_web.build_model(data)
    [f] = model["finds"]
    assert f["verdict"] == "miss"
    assert f["actualDailyTxt"] == "$0"
    assert model["summary"]["miss"] == 1


def test_an_estimated_verdict_carries_no_actual_anywhere(data):
    """An estimate is a verdict with no measurement behind it, so the Meter
    stores nothing in `actual_daily_cents`. Every surface built from that row —
    the card and the admin timeline — has to say nothing rather than $0, which
    the owner would read as "this move earned nothing"."""
    data["finds"] = [find(verdict="estimated", actual_daily_cents=None,
                          note="No sales data connected.")]
    data["summary"] = {"verified": 0, "estimated": 1, "miss": 0,
                       "verified_daily_cents": 0, "hit_rate": None, "judged": 0}
    model = build_web.build_model(data)
    [f] = model["finds"]
    assert f["verdict"] == "estimated"
    assert f["actualDaily"] is None
    assert f["actualDailyTxt"] is None
    measured = [e for e in model["timeline"] if e["kind"] == "measured"]
    assert [e["amount"] for e in measured] == [None]


def test_money_never_becomes_a_float_in_the_model(data):
    """Cents in, formatted strings out — the page does no arithmetic on money."""
    model = build_web.build_model(data)
    [f] = model["finds"]
    for key in ("predictedDaily", "actualDaily"):
        assert isinstance(f[key], int), key
    assert isinstance(model["summary"]["dailyCents"], int)


# ------------------------------------------------------------ cross-checks


def test_the_run_receipt_shows_the_queries_the_analyst_actually_runs():
    """The receipt claims "6 questions asked" and lists them. If the Analyst's
    queries change and this copy does not, the UI is describing a search that
    never happened."""
    assert list(build_web.ANALYST_QUERIES) == list(ANALYST_QUERIES)


def test_templates_carry_the_data_placeholder():
    """Both pages are rendered by substituting one JSON block. A template
    missing it would silently ship with no data."""
    for name in ("app.html", "landing.html", "signup.html", "login.html",
                 "register.html"):
        html = (build_web.SITE / name).read_text(encoding="utf-8")
        assert build_web.DATA_TAG.search(html), name


def test_landing_routes_directly_to_the_demo():
    """The nav offers signup; the page itself opens the interactive demo.

    The demo now begins on the signup page in tour mode: a walkthrough of
    onboarding with sample answers that types itself in, then hands off to
    the board. A judge still reaches the working product without handing
    over a name — tour mode pre-fills every field, holds no session, and
    never POSTs — so the original principle stands even though the demo
    now shows what onboarding looks like first.
    """
    html = (build_web.SITE / "landing.html").read_text(encoding="utf-8")
    assert 'class="nav-cta" href="register/">Sign up</a>' in html
    assert html.count('href="signup/?tour=owner"><span>Try the interactive demo</span>') == 2
    # The unguided drop-in is retired, not just renamed.
    assert 'href="app/">' not in html


def test_signup_tour_mode_shows_onboarding_without_writing_anything():
    """?tour= walks the signup page as the demo's opening chapter. Three
    things make that safe to show a stranger: no session is required (the
    register redirect is tour-guarded), nothing the walkthrough types is
    drafted into localStorage, and submit never reaches the onboarding API —
    the success step renders locally and hands off to the app tour.
    """
    html = (build_web.SITE / "signup.html").read_text(encoding="utf-8")
    assert "const TOUR" in html
    assert "if (!currentSession() && !TOUR) window.location.replace" in html
    # The draft writer and the submit path both bail out in tour mode.
    assert html.count("if (TOUR) return;") >= 2
    assert "Interactive demo · nothing was saved." in html
    assert "../app/?tour=owner&from=onboarding" in html


def test_signup_collects_the_agent_scope():
    """This asserted `'type="password"' not in html` and "No password yet".

    That was correct while the product had one seeded tenant and no accounts:
    a signup that took a password would have been collecting a credential it
    had nowhere to store and nothing to protect. Accounts landed on 2026-08-02,
    so the absence it pinned is now the bug — a workspace no one can sign into.
    The scope fields it also guarded are unaffected and stay here; the password
    assertions moved to test_signup_collects_a_username_and_password.
    """
    html = (build_web.SITE / "signup.html").read_text(encoding="utf-8")
    for field_id in (
        "ownerName", "email", "businessName", "category", "location",
        "website", "offers",
    ):
        assert f'id="{field_id}"' in html
    for dimension in (
        'data-multi="segments"', 'data-multi="channels"', 'data-goal=',
    ):
        assert dimension in html
    assert "brass-tacks-onboarding-profile-v1" in html


def test_signup_and_app_share_the_same_profile_memory_key():
    signup = (build_web.SITE / "signup.html").read_text(encoding="utf-8")
    app = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    key = "brass-tacks-onboarding-profile-v1"
    assert key in signup and key in app


def test_onboarding_endpoint_reaches_the_built_view_model(monkeypatch, data):
    monkeypatch.setenv("ONBOARDING_API_ENDPOINT", "https://api.example/v1/onboarding/")
    model = build_web.build_model(data)
    assert model["api"]["onboardingEndpoint"] == "https://api.example/v1/onboarding"


def test_menu_scan_endpoint_is_derived_from_onboarding(monkeypatch, data):
    """One env var fewer to set. The scan lives on the onboarding Lambda, so
    its URL is always the onboarding URL plus a segment."""
    monkeypatch.delenv("MENU_SCAN_API_ENDPOINT", raising=False)
    monkeypatch.setenv("ONBOARDING_API_ENDPOINT", "https://api.example/v1/onboarding/")
    model = build_web.build_model(data)
    assert model["api"]["menuScanEndpoint"] == "https://api.example/v1/onboarding/menu-scan"


def test_menu_scan_endpoint_can_be_set_explicitly(monkeypatch, data):
    monkeypatch.setenv("MENU_SCAN_API_ENDPOINT", "https://api.example/v1/scan/")
    model = build_web.build_model(data)
    assert model["api"]["menuScanEndpoint"] == "https://api.example/v1/scan"


def test_the_signup_page_offers_to_scan_a_menu(monkeypatch, data):
    """The feature has to be reachable from the page a real owner lands on."""
    html = (build_web.SITE / "signup.html").read_text(encoding="utf-8")
    assert 'id="menuPhotos"' in html
    assert "menuScanEndpoint" in html
    # `capture` is what makes the file picker open the camera on a phone,
    # which is the whole interaction: the owner is stood in front of the menu.
    assert "capture" in html


def test_menu_prices_are_edited_in_dollars_and_sent_as_cents(monkeypatch, data):
    """The review step is where a misread price gets corrected.

    Showing raw cents in the input would make the owner do the conversion, and
    posting a float back would put a rounding error into the money column.
    """
    html = (build_web.SITE / "signup.html").read_text(encoding="utf-8")
    assert "centsToInput" in html
    assert "inputToCents" in html
    # Math.round, not parseInt or a bare multiply: 14.10 * 100 is
    # 1409.9999999999998 in IEEE 754 and truncating that loses a cent.
    assert "Math.round" in html


def test_new_owner_workspace_is_honest_until_radar_runs():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    # Formatting-independent: an onboarding workspace shows no posts. The
    # assertion used to pin the whole line on one row, which broke when the
    # invented-card fallback was removed and the expression wrapped.
    assert "let posts = onboardingMode" in html
    assert "? []" in html
    assert "renderOnboardingWorkspace" in html
    assert "first market sweep" in html
    assert "Your workspace is ready" in html
    assert "who Radar watches, where it searches" in html
    assert "Profile ready" in html


def test_radar_statistics_use_operator_language():
    """Radar counts evidence, not recommendations. The compact operator view
    must say that directly instead of exposing database jargon."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    assert "market signals stored" in html
    assert "How to read Radar" in html
    assert "memories ready for retrieval" not in html
    assert 'label: "signal types"' not in html


def test_render_escapes_a_closing_script_tag(data):
    """A find whose text contains </script> would otherwise end the JSON block
    early and break the page."""
    data["finds"] = [find(note="</script><b>x</b>")]
    model = build_web.build_model(data)
    out = build_web.render(build_web.SITE / "app.html", model)
    assert "</script><b>x</b>" not in out
    assert "<\\/script>" in out


def test_build_model_does_not_mutate_its_input(data):
    before = copy.deepcopy(data)
    build_web.build_model(data)
    assert data == before


# ------------------------------------------------- agent runs (admin view)


@pytest.mark.parametrize("started, finished, expected", [
    ("2026-07-28T10:09:58.200643+00:00", "2026-07-28T10:11:16.319624+00:00", 78),
    ("2026-07-28T10:09:58+00:00", "2026-07-28T10:09:58+00:00", 0),
    ("2026-07-28T10:09:58+00:00", None, None),
    ("2026-07-28T10:09:58+00:00", "", None),
])
def test_run_seconds_measures_a_finished_run_only(started, finished, expected):
    """A run still in flight has no duration — it must not report 0 seconds,
    which would read on the admin screen as "finished instantly"."""
    assert build_web.run_seconds(
        {"started_at": started, "finished_at": finished}) == expected


def test_runs_reach_the_model_for_the_admin_view(data):
    model = build_web.build_model(data)
    assert [r["agent"] for r in model["runs"]] == [r["agent"] for r in data["runs"]]
    assert all(len(r["id"]) == 8 for r in model["runs"]), "ids are shortened for display"


def test_admin_run_list_is_not_padded(data):
    """The seeded corpus has exactly one run. The view says so rather than
    inventing a busier night than the cluster actually recorded."""
    model = build_web.build_model(data)
    assert len(model["runs"]) == len(data["runs"])


# ------------------------------------------------- the admin view's evidence


def test_a_find_carries_the_dates_the_timeline_is_built_from(data):
    """The fixture holds one agent_run row, so the run list alone makes seven
    weeks of work look like a single night. The finds carry the real history —
    proposed on one date, due on another, judged on a third — and the model used
    to drop all three."""
    model = build_web.build_model(data)
    got = model["finds"][0]
    assert got["createdAt"] == "2026-06-01T02:00:00+00:00"
    assert got["periodStart"] == "2026-06-01"
    assert got["periodEnd"] == "2026-06-15"


def test_the_timeline_is_only_events_the_cluster_timestamped(data):
    """Every entry names a column a run wrote: `created_at` when the Analyst
    proposed the find, `measured_at` when the Meter judged it. Nothing is
    interpolated between them — a gap is a stretch where nothing was recorded,
    and the page must be free to say so."""
    model = build_web.build_model(data)
    kinds = [e["kind"] for e in model["timeline"]]
    assert kinds.count("proposed") == 1
    assert kinds.count("measured") == 1
    stamps = {e["kind"]: e["at"] for e in model["timeline"]}
    assert stamps["proposed"] == "2026-06-01T02:00:00+00:00"
    assert stamps["measured"] == "2026-06-24T06:00:00+00:00"
    assert all(e["findId"] == model["finds"][0]["id"] for e in model["timeline"])


def test_a_due_date_is_not_an_event(data):
    """`verify_after` is a commitment, not something that happened. Most of the
    seeded finds are due after the export was taken, so admitting it as an event
    put dates in the future at the top of a log of the past. It belongs on the
    find, where the page reads it as one end of a span."""
    data["finds"] = [find(verify_after="2099-01-01")]
    model = build_web.build_model(data)
    assert all(e["kind"] in ("proposed", "measured") for e in model["timeline"])
    assert model["finds"][0]["verifyAfter"] == "2099-01-01"


def test_the_timeline_is_newest_first(data):
    older = find(id="a" * 36, created_at="2026-05-01T02:00:00+00:00",
                 verify_after="2026-05-20", measured_at=None, verdict=None,
                 actual_daily_cents=None, status="live")
    data["finds"] = [find(), older]
    model = build_web.build_model(data)
    stamps = [e["at"] for e in model["timeline"]]
    assert stamps == sorted(stamps, reverse=True)


def test_an_unjudged_find_contributes_no_measured_event(data):
    """A find still being measured has no verdict yet. Emitting a `measured`
    event for it would put a judgement on the page that no run ever made."""
    data["finds"] = [find(measured_at=None, verdict=None, actual_daily_cents=None,
                          status="live")]
    model = build_web.build_model(data)
    assert [e["kind"] for e in model["timeline"]] == ["proposed"]


def test_maker_artifacts_reach_the_model(data):
    data["finds"] = [find(artifacts=[{
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "kind": "review_reply", "title": "Reply to Dana",
        "preview": "Thank you for the kind words.",
        "created_at": "2026-06-02T06:00:00+00:00",
        "s3_bucket": "bt-artifacts", "s3_key": "finds/x/review_reply.md",
    }])]
    model = build_web.build_model(data)
    art = model["finds"][0]["artifacts"][0]
    assert art["kind"] == "review_reply"
    assert art["stored"] is True
    assert art["location"] == "s3://bt-artifacts/finds/x/review_reply.md"
    assert model["artifactCount"] == 1


def test_a_draft_that_never_reached_s3_still_counts_as_a_draft(data):
    """The Maker treats a failed upload as "draft saved, not uploaded" rather
    than losing the work, so the page must not report the find as undrafted."""
    data["finds"] = [find(artifacts=[{
        "id": "a" * 36, "kind": "review_reply", "title": "Reply to Dana",
        "preview": "…", "created_at": "2026-06-02T06:00:00+00:00",
        "s3_bucket": None, "s3_key": None,
    }])]
    model = build_web.build_model(data)
    art = model["finds"][0]["artifacts"][0]
    assert art["stored"] is False and art["location"] is None
    assert model["artifactCount"] == 1


def test_a_fixture_predating_artifacts_still_builds(data):
    """The committed fixture was exported before `export_fixture.py` learned to
    attach artifacts, so the key is absent rather than empty."""
    assert "artifacts" not in data["finds"][0]
    model = build_web.build_model(data)
    assert model["finds"][0]["artifacts"] == []
    assert model["artifactCount"] == 0


def test_tokens_are_reported_unrecorded_rather_than_zero(data):
    """Historical runs may predate usage accounting.

    Zero would read as "this run was free"; null plus a stated source reads as
    what it is, even though new Analyst and Maker runs now write token usage.
    """
    data["runs"] = [{"id": "r" * 36, "agent": "radar", "status": "ok",
                     "started_at": "2026-07-28T10:00:00+00:00",
                     "finished_at": "2026-07-28T10:01:18+00:00",
                     "note": "127 observed", "model_id": None}]
    run = build_web.build_model(data)["runs"][0]
    assert run["inputTokens"] is None and run["outputTokens"] is None
    assert run["tokensSource"] == "unrecorded"
    assert run["modelId"] is None and run["error"] is None


def test_static_export_keeps_receipts_for_open_finds_without_growing_forever():
    source = (build_web.REPO / "scripts" / "export_fixture.py").read_text(encoding="utf-8")
    assert "WITH recent AS" in source
    assert "JOIN find f ON f.run_id = ar.id" in source
    assert "f.status IN ('proposed', 'later', 'accepted', 'live')" in source
    assert "FROM ledger_entry le" in source


# ------------------------------------------- placeholders that cannot pass
#
# The admin view ships stand-in data for things the cluster does not record yet
# — the Ask agent's SQL trail, token counts, dedup figures. That is legitimate
# for a page whose job is to show how the machinery works. What is not
# legitimate is placeholder data that a reader cannot tell apart from measured
# data. These tests make the difference structural rather than a matter of
# remembering to add a badge.


def placeholders(**blocks):
    return {"_comment": "test", **blocks}


def test_build_model_invents_nothing_of_its_own(data):
    """`build_model` is a pure function of the cluster export. Every block it
    emits is sourced from the data or from a constant that mirrors the backend,
    never from a stand-in — those arrive later, by a route that is guarded."""
    model = build_web.build_model(data)
    for key, block in model.items():
        if isinstance(block, dict) and "source" in block:
            assert block["source"] != "placeholder", key
    assert len(model["runs"]) == len(data["runs"])
    assert len(model["finds"]) == len(data["finds"])


def test_a_merged_block_must_say_where_it_came_from(data):
    model = build_web.build_model(data)
    with pytest.raises(build_web.PlaceholderError, match="source"):
        build_web.merge_placeholders(model, placeholders(ask={"sessions": []}))


def test_a_placeholder_can_never_touch_a_guarded_key(data):
    """`runs`, `finds`, `summary`, `months` and `ratings` are what the honesty
    tests guard and what the page labels as cluster rows. A future maintainer
    must not be able to launder an invented month into the growth chart by
    dropping it into the placeholder file."""
    model = build_web.build_model(data)
    for guarded in ("runs", "finds", "summary", "months", "ratings"):
        with pytest.raises(build_web.PlaceholderError, match=guarded):
            build_web.merge_placeholders(
                model, placeholders(**{guarded: {"source": "placeholder"}}))


def test_a_placeholder_may_never_carry_money(data):
    """Placeholders may fake operational figures — SQL text, durations, dedup
    counts. They may never fake financial ones. Every honesty rule in CLAUDE.md
    is a claim about the owner's money, so the line is drawn at money itself and
    holds even if a badge is missed at render time."""
    model = build_web.build_model(data)
    for key in ("dailyCents", "predictedDaily", "actualDailyTxt"):
        with pytest.raises(build_web.PlaceholderError, match="money"):
            build_web.merge_placeholders(model, placeholders(
                ask={"source": "placeholder", "sessions": [{key: 1200}]}))


def test_a_placeholder_may_not_quote_a_figure_in_prose(data):
    """The money rule has to survive prose, not just field names. A stand-in
    answer reading "the patio night brought in $19/day" states an earning as
    plainly as any column would, and nothing about it being inside a sentence
    makes it less of a claim about the owner's money."""
    model = build_web.build_model(data)
    with pytest.raises(build_web.PlaceholderError, match="money"):
        build_web.merge_placeholders(model, placeholders(ask={
            "source": "placeholder",
            "sessions": [{"answer": "The patio night brought in $19/day."}],
        }))


def test_merging_keeps_the_real_fields_of_the_block_it_lands_on(data):
    """`ask` is partly real already — the question limit and the table list come
    from the handler. A stand-in supplies the sessions the cluster has never
    recorded and must not take the real fields down with it."""
    model = build_web.build_model(data)
    merged = build_web.merge_placeholders(model, placeholders(ask={
        "source": "placeholder", "sessions": [{"runId": "abc"}]}))
    assert merged["ask"]["maxQuestionChars"] == build_web.MAX_QUESTION_CHARS
    assert merged["ask"]["tables"] == build_web.ASK_TABLES
    assert merged["ask"]["sessions"] == [{"runId": "abc"}]
    # The panel is badged by its weakest part, not its strongest.
    assert merged["ask"]["source"] == "placeholder"


def test_the_shipped_placeholder_only_names_finds_that_exist():
    """A stand-in answer is still checkable. The Ask sessions quote find ids in
    their SQL, and an id that matches nothing in the fixture is an invitation
    for a judge to run the query and find the page describing a row the cluster
    does not hold. Caught one: an answer claimed nine observations at 0.58 for a
    find carrying five at 0.129."""
    import re as _re
    shipped = json.loads(build_web.PLACEHOLDERS.read_text(encoding="utf-8"))
    model = build_web.build_model(json.loads(
        build_web.FIXTURE.read_text(encoding="utf-8")))
    known = {f["id"] for f in model["finds"]}
    for session in shipped.get("ask", {}).get("sessions", []):
        for line in session["trail"]:
            for quoted in _re.findall(r"find_id = '([0-9a-f]{8})'", line):
                assert quoted in known, f"{quoted} is not a find in the fixture"


def test_a_merged_block_reaches_the_model_and_is_declared(data):
    model = build_web.build_model(data)
    merged = build_web.merge_placeholders(model, placeholders(
        ask={"source": "placeholder", "sessions": [{"runId": "abc"}]}))
    assert merged["ask"]["sessions"][0]["runId"] == "abc"
    assert merged["provenance"]["blocks"]["ask"] == "placeholder"


def test_the_provenance_manifest_covers_every_declared_block(data):
    """The manifest is what the page reads to decide which panels wear a badge.
    A block that declares a source and goes unlisted would render unmarked."""
    model = build_web.merge_placeholders(build_web.build_model(data),
                                         placeholders())
    declared = {k for k, v in model.items()
                if isinstance(v, dict) and "source" in v}
    assert declared <= set(model["provenance"]["blocks"])


def test_merging_nothing_leaves_the_model_alone(data):
    """An empty placeholder file is the shipped state until the panels are
    filled, and it must not change a single figure."""
    model = build_web.build_model(data)
    before = copy.deepcopy(model)
    merged = build_web.merge_placeholders(model, {"_comment": "none yet"})
    assert {k: v for k, v in merged.items() if k != "provenance"} == before


# ------------------------------------------ what the detail panels claim
#
# Clicking an agent opens a panel that quotes the backend at a judge: model
# ids, endpoints, thresholds, index names. Quoting is the whole value of it,
# so each of these pins a string on screen to the code it came from. A panel
# that drifts is worse than one that says nothing.


def detail_panels():
    """The DETAIL map's source text, which is where the claims live."""
    start = APP.index("const DETAIL = {")
    return APP[start:APP.index("\n    };", start)]


@pytest.mark.parametrize("named", [
    "VECTOR(1024)",
    "observation_embed_idx",
    "observation_dedup_idx",
    "ledger_period_idx",
    "vector_cosine_ops",
])
def test_the_panels_name_the_schema_as_it_is(named):
    assert named in detail_panels(), f"{named} is not on the page"
    assert named in SCHEMA, f"{named} is not in db/schema.sql"


def test_the_panels_name_the_embedding_model_as_configured():
    """The embedding model is the load-bearing AWS dependency the submission
    discloses, so the id on screen has to be the id the build ships."""
    env = (build_web.REPO / ".env.example").read_text(encoding="utf-8")
    model_id = "amazon.titan-embed-text-v2:0"
    assert model_id in detail_panels()
    assert model_id in env


def test_the_panels_quote_the_backend_constants():
    from brasstacks import meter as meter_mod
    from brasstacks import providers
    from brasstacks.agents import maker
    from brasstacks.handlers import ask as ask_handler

    panels = detail_panels()
    assert providers.DEFAULT_MCP_URL in panels
    assert providers.MCP_BETA_FLAG in panels
    assert providers.DEFAULT_MCP_SERVER_NAME in panels
    assert maker.ARTIFACT_KIND in panels
    assert str(maker.PREVIEW_CHARS) in panels
    assert str(ask_handler.MAX_QUESTION_CHARS) in panels
    # 0.25 is shown to the owner as a percentage, which is the same number.
    assert f"{int(meter_mod.DEFAULT_MISS_THRESHOLD * 100)}%" in panels


def test_the_yelp_retention_claim_matches_the_licence_term():
    """The panel tells a judge Yelp review text expires after 24 hours. That
    figure is a licence term rather than a setting, so it is the one number on
    the page that must not be tuned to look better."""
    from brasstacks import signals
    assert signals.YELP_RETENTION_HOURS == 24
    assert "twenty-four hours" in detail_panels()


def test_the_panels_do_not_claim_bedrock_reasoning():
    """Reasoning runs on the Anthropic API because AWS would not grant this
    account a current Claude model on Bedrock. The panel says so; a page that
    implied otherwise would misstate the one disclosure most worth getting
    right."""
    panels = detail_panels()
    assert "Anthropic API" in panels and "not on\n                   Bedrock" in panels
    assert "claude-opus-5" in panels


def test_every_agent_and_the_core_open_into_a_panel():
    panels = detail_panels()
    for key in ("radar:", "analyst:", "maker:", "meter:", "ask:", "core:"):
        assert f"\n      {key}" in panels, key


# --------------------------------------------------- the console backdrop
#
# The admin view's scene is hand-drawn vector by default. A painted backdrop
# reaches a depth that geometry cannot, so the build picks one up if it is
# there — and must carry on without one, because a fresh clone has none.


def test_a_backdrop_is_found_when_one_is_dropped_in(tmp_path):
    (tmp_path / "hud-backdrop.png").write_bytes(b"x")
    assert build_web.find_backdrop(tmp_path) == "../assets/hud-backdrop.png"


def test_the_smaller_format_wins_when_several_are_present(tmp_path):
    for name in ("hud-backdrop.png", "hud-backdrop.jpg", "hud-backdrop.webp"):
        (tmp_path / name).write_bytes(b"x")
    assert build_web.find_backdrop(tmp_path) == "../assets/hud-backdrop.webp"


def test_no_backdrop_is_not_an_error(tmp_path):
    """A clone has no assets folder at all, and the drawn scene stands in."""
    assert build_web.find_backdrop(tmp_path) is None
    assert build_web.find_backdrop(tmp_path / "nope") is None


def test_the_model_says_whether_a_backdrop_was_found(data):
    """The page switches its whole scene on this one value, so it is stated
    rather than probed for at runtime."""
    assert build_web.build_model(data)["backdrop"] in (None, *[
        f"../assets/hud-backdrop{ext}" for ext in build_web.BACKDROP_FORMATS])


def test_the_page_drops_its_drawn_scene_when_a_backdrop_is_present():
    """Both must not paint at once: the image already carries a map, a floor
    and its own light, and the vector scene over it reads as a second world."""
    assert '.view.console.has-backdrop .hud-scene { display: none' in APP
    assert '.view.console.has-backdrop::after { display: none' in APP


# --------------------------------------------- what the admin view argues


def test_the_funnel_separates_a_ceiling_from_a_measurement(data):
    """Six queries at six rows each is thirty-six *at most* — `search_observations`
    is LIMIT 6 per query, so the number is a product of two constants and not
    something anyone counted. The stage says so, and the stage that really was
    counted says that instead."""
    model = build_web.build_model(data)
    stages = {s["key"]: s for s in model["retrieval"]["byFind"][model["finds"][0]["id"]]}
    assert stages["queries"]["n"] == 6 and stages["queries"]["source"] == "code"
    assert stages["raw"]["n"] == 36 and stages["raw"]["source"] == "code"
    assert "ceiling" in stages["raw"]["label"]
    assert stages["cited"]["n"] == 1 and stages["cited"]["source"] == "cluster"


def test_the_dedup_stage_admits_it_was_never_recorded(data):
    """The Analyst writes its retrieved count into its own run note, so this
    becomes real the moment an analyst run exists. Until then it is null rather
    than a guess."""
    model = build_web.build_model(data)
    stages = {s["key"]: s for s in model["retrieval"]["byFind"][model["finds"][0]["id"]]}
    assert stages["deduped"]["n"] is None
    assert stages["deduped"]["source"] == "unrecorded"


def test_the_funnel_matches_the_analyst_the_page_describes():
    """The same cross-check the query list already gets: if the Analyst changes
    how many rows it pulls per query, the funnel on screen is describing a
    search that never happened."""
    assert build_web.PER_QUERY_LIMIT == analyst.DEFAULT_PER_QUERY_LIMIT
    assert build_web.RAW_HIT_CEILING == len(ANALYST_QUERIES) * analyst.DEFAULT_PER_QUERY_LIMIT


def test_an_ask_run_becomes_a_session_with_its_sql(data):
    """The trail is already stored, in `agent_run.note`, by every Ask run. The
    page needs no new column to show what SQL hit the cluster."""
    data["runs"] = [{
        "id": "3f2a91c4-0000-0000-0000-000000000000", "agent": "ask",
        "status": "ok", "started_at": "2026-07-28T10:14:02+00:00",
        "finished_at": "2026-07-28T10:14:08+00:00",
        "note": "2 tool call(s)\nsql> select_query SELECT title FROM find\n"
                "sql> select_query [FAILED] SELECT * FROM nope",
    }]
    session = build_web.build_model(data)["ask"]["sessions"][0]
    assert session["runId"] == "3f2a91c4"
    assert session["queriedTheCluster"] is True
    assert session["trail"] == ["select_query SELECT title FROM find",
                               "select_query [FAILED] SELECT * FROM nope"]
    assert session["seconds"] == 6


def test_an_answer_that_read_nothing_says_so(data):
    """A model answering from its own knowledge rather than the cluster is the
    exact thing the note exists to expose, so it must not render as a session
    that queried anything."""
    data["runs"] = [{
        "id": "a" * 36, "agent": "ask", "status": "ok",
        "started_at": "2026-07-28T10:14:02+00:00",
        "finished_at": "2026-07-28T10:14:03+00:00",
        "note": "answered with no tool calls — nothing was read from the cluster",
    }]
    session = build_web.build_model(data)["ask"]["sessions"][0]
    assert session["queriedTheCluster"] is False
    assert session["trail"] == []


def test_the_ask_panel_agrees_with_the_api_it_will_call():
    """The input's limit and the listed tables are disclosures about what a
    question can do. If either drifts from the handler, the page is describing
    a different API than the one behind it."""
    from brasstacks.handlers import ask as ask_handler
    assert build_web.MAX_QUESTION_CHARS == ask_handler.MAX_QUESTION_CHARS
    for table in build_web.ASK_TABLES:
        assert f"{table}(" in ask.SCHEMA_HINT


def test_the_trail_parser_is_the_inverse_of_the_one_that_wrote_it():
    """Pinned to the agent's own writer rather than to a copy of its format."""
    line = "select_query SELECT 1"
    assert build_web.parse_trail(f"1 tool call(s)\n{ask.TRAIL_PREFIX}{line}") == [line]
    assert build_web.parse_trail("no trail here") == []


def test_no_ask_runs_is_an_empty_session_list_not_a_missing_block(data):
    """The seeded cluster has never run Ask. The panel ships an honest empty
    state, and the same key fills itself the moment a real run lands."""
    model = build_web.build_model(data)
    assert model["ask"]["sessions"] == []
    assert model["ask"]["source"] == "cluster"


# ------------------------------------------------------ the card can't crush

APP = (build_web.SITE / "app.html").read_text(encoding="utf-8")
SCHEMA = (build_web.REPO / "db" / "schema.sql").read_text(encoding="utf-8")

# Comments in this repo quote the copy they replaced, so "this sentence is gone
# from the page" cannot be asserted against the file — the comment recording its
# removal contains it. Same problem, and the same fix, as the scoped assertion in
# test_card_copy_spacing_is_fixed_and_never_a_leftover.
APP_MARKUP = re.sub(r"<!--.*?-->", "", APP, flags=re.S)


def test_card_copy_spacing_is_fixed_and_never_a_leftover():
    """The deck is a fixed height, so a short card has room left over. When that
    room lived in the gap above the Try-this panel — an auto margin, which takes
    all of it — the gap ranged from 0px on a card whose title wrapped to 66px on
    a one-line card, cramped at one end and a hole in the middle of the card at
    the other. The gaps between the copy elements are therefore fixed, in a
    `gap` on the column that is charged before any flexible space is handed out,
    and the spare room goes to a flexible spacer at the bottom instead."""
    # Several stylesheets touch .post-copy; the one that matters is the block
    # that makes it the flex column.
    blocks = [
        body for body in
        (part.split("}")[0] for part in APP.split("body.autopilot-mode .post-copy {")[1:])
        if "flex-direction: column" in body
    ]
    assert len(blocks) == 1, "the copy column is declared in more than one place"
    assert "gap:" in blocks[0], "the spacing floor is gone; a long title will crush the card"
    # Scoped to the rule, not the file: the comment above it explains the auto
    # margin this replaced, and that prose must not fail its own test.
    panel = APP.split("body.autopilot-mode .post-copy .recommendation {")[1].split("}")[0]
    assert "margin-top: auto" not in panel, \
        "an auto margin here pools the card's spare room into one gap again"
    spacer = APP.split("body.autopilot-mode .post-copy::after {")[1].split("}")[0]
    assert "flex: 1" in spacer, "nothing is absorbing the spare room at the bottom"
    assert "margin-top: calc(-1 *" in spacer, \
        "the spacer charges the card a gap for holding nothing"
    for child in (".post-meta-row { margin-bottom: 0", ".signal-text { margin-top: 0"):
        assert child in APP, f"{child} would add spacing the gap already provides"


def test_every_fit_step_the_script_climbs_has_a_style():
    """`fitFeedCards()` steps a card's title down until its copy fits. Step 0 is
    the design and needs no rule; every rung above it does, or the loop measures
    the same overflow forever and gives up on an unchanged card."""
    steps = int(APP.split("const FIT_STEPS = ")[1].split(";")[0])
    for step in range(1, steps):
        # Specifically the title: it is the tall thing, and a rung that does not
        # shrink it leaves the loop measuring the same overflow it started with.
        selector = f'.feed-card[data-fit="{step}"] .post-copy h2'
        assert selector in APP, f"step {step} never shrinks the title"
        assert "font-size" in APP.split(selector)[1].split("}")[0], \
            f"step {step} styles the title without resizing it"


def test_the_fit_pass_is_not_scheduled_on_a_frame():
    """Frames stop being served to a tab the browser is not painting, and the
    resize that matters most is the one that happened while the window was
    elsewhere. requestAnimationFrame was tried here and the fit never ran."""
    listener = APP.split('window.addEventListener("resize"')[1].split("});")[0]
    assert "requestAnimationFrame" not in listener
    assert "setTimeout" in listener

# --------------------------------------- operator-first Memory Engine layout


def test_business_identity_reaches_the_operator_model(data):
    """The operator view must distinguish business owners; a display name alone
    is not a stable tenant key and cannot support a multi-owner pipeline."""
    model = build_web.build_model(data)
    assert model["business"]["id"] == "b"
    assert model["business"]["category"] == "restaurant"
    assert model["business"]["region"] == "OH"


def test_memory_engine_is_an_owner_pipeline_not_a_radial_network():
    """Operators need rows by owner and columns by workflow stage. The old
    radial node map hid both the tenant and the sequence of work."""
    assert 'id="ownerPipelines"' in APP
    assert 'id="memoryOwnerFilter"' in APP
    assert 'id="memorySummary"' in APP
    for stage in ("radar", "analyst", "decision", "maker", "meter"):
        assert f'data-stage="{stage}"' in APP
    assert "Decision gate" in APP


def test_memory_engine_details_are_progressively_disclosed():
    """The overview stays scannable; owner, agent, and infrastructure details
    must be available without rendering every technical receipt at once."""
    assert 'class="owner-pipeline-details"' in APP
    assert 'class="agent-accordion"' in APP
    assert 'id="memorySystemDetails"' in APP
    assert "toggleOwnerPipeline" in APP
    assert "toggleAllOwnerPipelines" in APP


def test_memory_engine_can_normalize_multiple_owner_workspaces():
    """The live demo has one tenant, but the renderer must consume an array when
    the operator API begins returning several business owners."""
    assert "normalizeOwnerWorkspaces" in APP
    assert "btData.ownerWorkspaces" in APP
    assert "buildCurrentOwnerWorkspace" in APP


def test_memory_engine_has_operations_and_live_graph_modes():
    """Operators can switch between the dense audit matrix and a visual live map
    without losing the same owner-scoped source of truth."""
    assert 'data-memory-view-mode="workflow"' in APP
    assert 'data-memory-view-mode="visual"' in APP
    assert 'id="memoryWorkflowView"' in APP
    assert 'id="memoryVisualView"' in APP
    assert "function setMemoryEngineMode" in APP
    assert "function renderMemoryVisual" in APP


def test_memory_visual_mode_is_owner_scoped_and_data_driven():
    """The graph must select a real owner workspace and derive every node/chart
    from that owner's live model rather than from presentation-only constants."""
    for marker in (
        'id="memoryVisualOwnerList"',
        'id="memoryVisualPipeline"',
        'id="visualStageInspector"',
        'id="visualPortfolioLoad"',
        'id="visualMemoryChart"',
        'id="visualDecisionChart"',
    ):
        assert marker in APP
    assert "function memoryVisualOwnerButton" in APP
    assert "function memoryPortfolioStageCounts" in APP
    assert "function memorySignalGroups" in APP
    assert "deriveMemoryStages(model)" in APP
    assert "normalizeOwnerWorkspaces()" in APP


def test_memory_engine_removes_the_redundant_attention_kpi_and_subtitle():
    """Attention already appears at the exact pipeline handoff; a global card
    and explanatory subtitle duplicated that signal and crowded the header."""
    header = APP[APP.index('<header class="engine-header"'):APP.index('</header>', APP.index('<header class="engine-header"'))]
    assert "Scan every business owner" not in header
    source = APP[APP.index("function renderPortfolioKpis"):APP.index("function renderMemoryPortfolioSummary")]
    assert 'label: "Need attention"' not in source
    assert "repeat(3, minmax(0, 1fr))" in APP


def test_memory_visual_animation_has_an_accessible_reduced_motion_path():
    """Animated flow is useful for live status, but operators who request less
    motion must receive a stable production interface."""
    assert "visual-flow-particle" in APP
    assert "visual-node-enter" in APP
    reduced = APP[APP.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".memory-visual-panel" in reduced
    assert "animation: none" in reduced


def test_technical_detail_sections_render_as_collapsible_disclosures():
    """Deep schema and model receipts remain checkable without dominating the
    first view of an agent's current work."""
    source = APP[APP.index("function renderDetailSection"):]
    source = source[:source.index("function renderNode")]
    assert "<details" in source
    assert "<summary" in source


def test_memory_engine_projects_feed_decisions_into_the_operator_pipeline():
    """A Do it / Pass action must change the operator pipeline immediately.

    The exported model is a build-time snapshot, while the decision is a live
    browser/API event.  Without a decision overlay, For You can remove a card
    while Memory Engine continues to report the old waiting count.
    """
    assert "function withDecisionOverlay" in APP
    assert "return source.map(workspace => withDecisionOverlay" in APP
    assert 'decision.status === "approved" ? "accepted" : "rejected"' in APP
    assert "proposed: orderedIds" in APP


def test_owner_decision_metrics_distinguish_pass_from_saved_for_later():
    """Pass closes a proposal; it is not the product's separate Later state."""
    source = APP[APP.index('key: "owner", index: "03"'):]
    source = source[:source.index('key: "maker", index: "04"')]
    assert 'label: "Pass"' in source
    assert 'label: "Do it"' in source
    assert 'label: "saved"' not in source
    assert "Pass → close and remember" in APP


def test_owner_decision_stage_exposes_a_traceable_decision_log():
    """Operators must see the recommendation, choice, actor, and recorded time.

    A count such as "1 passed" is not an audit trail.  The owner stage needs a
    compact log that can be expanded to the CockroachDB find identifier and
    routing result without making the default pipeline noisy.
    """
    assert "function memoryOwnerDecisionLogHtml" in APP
    assert "Decision history · newest first" in APP
    assert "Business owner" in APP
    assert "Recorded" in APP
    assert "CockroachDB record" in APP
    assert 'class="owner-decision-record' in APP
    assert "Time not recorded" in APP


def test_build_model_keeps_the_owner_decision_timestamp(data):
    data["finds"][0]["decided_at"] = "2026-08-01T18:03:04+00:00"
    model = build_web.build_model(data)
    assert model["finds"][0]["decidedAt"] == "2026-08-01T18:03:04+00:00"


def test_browser_prefers_the_server_decision_timestamp():
    """The receipt shown in Memory Engine should use CockroachDB's write time,
    not the owner's possibly-skewed browser clock, when the API returned one."""
    assert "receipt.decided_at" in APP
    assert "const recordedAt" in APP


def test_connected_decisions_survive_a_page_reload():
    """A CockroachDB-backed decision cannot reappear merely because the page reloaded."""
    assert "const decisionApiConfigured" in APP
    assert "decisionApiConfigured ? loadState()" in APP
    assert "if (!decisionApiConfigured)" in APP

# ---------------------------------------- live Memory Engine workflow endpoint


def test_build_model_exposes_the_live_workflow_endpoint(data, monkeypatch):
    monkeypatch.setenv("DECISION_API_ENDPOINT", "https://api.example/v1")
    monkeypatch.delenv("WORKFLOW_API_ENDPOINT", raising=False)
    model = build_web.build_model(data)
    assert model["api"]["workflowEndpoint"] == "https://api.example/v1/workflow"

    monkeypatch.setenv("WORKFLOW_API_ENDPOINT", "https://ops.example/live")
    model = build_web.build_model(data)
    assert model["api"]["workflowEndpoint"] == "https://ops.example/live"


def test_memory_engine_revalidates_live_state_without_reloading_the_export():
    assert "workflowApiConfigured" in APP
    assert "refreshWorkflowSnapshot" in APP
    assert '"If-None-Match"' in APP
    assert 'response.status === 304' in APP
    assert "liveWorkflowWorkspaces" in APP
    assert "mergeWorkflowWorkspace" in APP
    assert "visibilitychange" in APP
    assert 'data-memory-action="refresh"' in APP


def test_live_workflow_refresh_is_polled_only_while_the_operator_view_is_visible():
    source = APP[APP.index("function scheduleWorkflowRefresh"):]
    source = source[:source.index("function stopWorkflowRefresh")]
    assert 'activeView !== "admin"' in source
    assert "document.hidden" in source
    assert "setTimeout" in source


def test_live_workflow_refresh_preserves_the_open_maker_task():
    """A polling render replaces the ledger markup. Nested task disclosures
    need their own stable owner/task key or the task being read snaps shut."""
    capture = APP[APP.index("function captureMemoryOpenState"):]
    capture = capture[:capture.index("function restoreMemoryOpenState")]
    restore = APP[APP.index("function restoreMemoryOpenState"):]
    restore = restore[:restore.index("function renderLiveWorkflowViews")]

    assert '.maker-task-item[open]' in capture
    assert "ownerId" in capture
    assert "taskId" in capture
    assert "openState.makerTasks" in restore
    assert '.maker-task-item[data-task-id=' in restore
    assert "detail.open = true" in restore


def test_memory_kpi_tooltips_are_not_clipped_by_the_decorative_card_mask():
    """The context and token cards used overflow:hidden for a corner circle;
    that same mask cut off the help tooltip. Decoration may not own clipping."""
    block = APP.split('<style id="requested-ui-cleanup-v39">', 1)[1].split(
        "</style>", 1
    )[0]

    assert ".engine-kpi.is-efficiency" in block
    assert "overflow: visible" in block
    assert ".engine-kpi.is-efficiency::after" in block
    assert "display: none" in block
    assert ".engine-kpi .metric-help::after" in block
    assert "top: calc(100% + 12px)" in block


def test_deploy_template_exposes_the_workflow_read_route():
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")
    assert "WorkflowFunction:" in template
    assert 'Path: /workflow' in template
    assert 'Method: GET' in template
    assert "WorkflowEndpoint:" in template


def test_deploy_template_supports_a_custom_site_domain():
    """The demo URL judges type must be a real domain, not d2xxxx.cloudfront.net.

    Both parameters default to empty so a fresh clone still deploys with the
    CloudFront domain and no certificate — the alias only attaches when both
    values are supplied, and `sam deploy` reuses previous parameter values, so
    CI deploys that never mention the domain cannot detach it.
    """
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")

    # The two optional parameters, defaulting to off.
    assert "SiteDomainName:" in template
    assert "SiteCertificateArn:" in template
    for parameter in ("SiteDomainName:", "SiteCertificateArn:"):
        block = template.split(parameter, 1)[1]
        assert 'Default: ""' in block.split("Description:", 1)[0]

    # One condition guards both: an alias without a certificate (or the
    # reverse) is a CloudFront validation error at deploy time.
    assert "HasSiteDomain:" in template

    distribution = template.split("SiteDistribution:", 1)[1].split("Outputs:", 1)[0]
    assert "Aliases:" in distribution
    assert "!Ref SiteDomainName" in distribution
    assert "ViewerCertificate:" in distribution
    assert "AcmCertificateArn: !Ref SiteCertificateArn" in distribution
    assert "sni-only" in distribution
    assert "MinimumProtocolVersion:" in distribution

    # The SiteUrl output — what deploy scripts print and verify against —
    # must name the custom domain when one is configured.
    site_url = template.split("SiteUrl:", 1)[1].split("SiteBucketName:", 1)[0]
    assert "!If" in site_url and "HasSiteDomain" in site_url


def test_the_board_ships_security_headers():
    """CloudFront must attach security headers to every page it serves.

    The dashboard keeps the session token in localStorage and renders text that
    came from scrapes and models, so the headers are the backstop: the CSP
    stops an injected handler exfiltrating tokens to an arbitrary host (only
    the API origins are connectable), frame-ancestors stops the board being
    framed for clickjacking, nosniff and HSTS close the classics. Inline
    script/style stay allowed — the page is one self-contained file by design.
    """
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")

    assert "AWS::CloudFront::ResponseHeadersPolicy" in template

    policy = template.split("AWS::CloudFront::ResponseHeadersPolicy", 1)[1]
    assert "StrictTransportSecurity:" in policy
    assert "ContentTypeOptions:" in policy
    assert "FrameOptions:" in policy
    assert "ReferrerPolicy:" in policy

    csp = policy.split("ContentSecurityPolicy:", 1)[1]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "connect-src" in csp
    assert "execute-api" in csp, "the page must still reach its own API"

    # Declared but never attached is the quiet failure mode.
    distribution = template.split("SiteDistribution:", 1)[1].split("Outputs:", 1)[0]
    assert "ResponseHeadersPolicyId:" in distribution


def test_the_sweep_wakes_while_the_businesses_it_watches_are_open():
    """Every observation in the corpus was captured before its tenant opened.

    The three sweeps that ever ran fired at 06:28, 08:07 and 08:40 local,
    because the schedule was `cron(0 6 …)` — so Radar only ever saw shut
    restaurants, and a delivery page reading "not available right now" reached
    the Analyst as a broken storefront. A bot that only looks at closed
    businesses will keep finding outages.
    """
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")

    hour = int(re.search(r"Default: cron\(0 (\d+) \* \* \? \*\)", template).group(1))
    # Trading hours, with room for a 900s night that must not cross midnight UTC
    # from America/New_York: 18:00 EST is 23:00 UTC.
    assert 11 <= hour <= 18, f"cron hour {hour} is outside local trading hours"
    assert "ScheduleExpressionTimezone: !Ref ScheduleTimezone" in template

    schedule = template.split("ScheduleExpression:", 1)[1].split(
        "ScheduleState:", 1)[0]
    assert "closed" in schedule, "the template must say why this hour was chosen"

    # The owner turned the autopilot off. Re-timing it is not re-enabling it.
    state = template.split("ScheduleState:", 1)[1].split("MakerSweepExpression:", 1)[0]
    assert "Default: DISABLED" in state


def test_live_overlay_rederives_all_lifecycle_buckets_from_rows():
    """The live row status is authoritative for Later, Maker, and Meter too.

    Filtering only the build-time proposed list hid Later and left accepted
    decisions out of downstream stages until a fresh export.
    """
    source = APP[APP.index("function withDecisionOverlay"):]
    source = source[:source.index("function workflowWorkspaceId")]
    for bucket in ("proposed", "saved", "measuring", "earning", "judged"):
        assert f"{bucket}: orderedIds" in source
    assert 'find.status === "later"' in source
    assert '["accepted", "live"].includes(find.status)' in source


def test_successful_live_read_can_clear_a_stale_browser_decision():
    """CockroachDB must outrank localStorage after a tenant reset or reopen."""
    source = APP[APP.index("function syncPostsAndDecisionsFromWorkflow"):]
    source = source[:source.index("function workflowAgeLabel")]
    assert 'if (status === "proposed")' in source
    assert "delete state.decisions" in source
    assert "stateChanged = true" in source


def test_successful_live_response_is_authoritative_for_owner_accounts():
    """The build is a network-failure fallback, not a source of phantom tenants."""
    source = APP[APP.index("function mergeLiveWorkflowWorkspaces"):]
    source = source[:source.index("function syncPostsAndDecisionsFromWorkflow")]
    assert "bases.forEach" not in source
    assert "successful response is authoritative" in source


def test_live_merge_never_resurrects_an_absent_active_proposal():
    """A deleted/reset proposal cannot come back from the first-paint snapshot."""
    source = APP[APP.index("function mergeWorkflowWorkspace"):]
    source = source[:source.index("function mergeLiveWorkflowWorkspaces")]
    assert 'const historical = Boolean(find.verdict) || find.status === "retired"' in source
    assert "if (historical && !liveIds.has(id))" in source


def test_static_model_carries_analyst_trace_and_find_run_link(data):
    from brasstacks.analyst_trace import encode_analyst_trace

    run_id = "20000000-0000-0000-0000-000000000002"
    find_id = data["finds"][0]["id"]
    data["finds"][0]["run_id"] = run_id
    data["runs"].insert(0, {
        "id": run_id,
        "agent": "analyst",
        "status": "ok",
        "started_at": "2026-07-29T10:00:00+00:00",
        "finished_at": "2026-07-29T10:00:12+00:00",
        "note": "17 retrieved; proposed a move\n" + encode_analyst_trace(
            query_hits=[6, 4, 3, 2, 1, 1], raw_hits=17,
            unique_hits=12, cited_hits=5, find_id=find_id,
        ),
        "input_tokens": 1900,
        "output_tokens": 240,
        "model_id": "claude-test",
        "error": None,
    })

    model = build_web.build_model(data)
    trace = model["analyst"]["latestRun"]
    first = next(find for find in model["finds"] if find["databaseId"] == find_id)

    assert trace["queryHits"] == [6, 4, 3, 2, 1, 1]
    assert trace["uniqueHits"] == 12
    assert trace["inputTokens"] == 1900
    assert first["runDatabaseId"] == run_id


def test_static_model_carries_evidence_provenance_for_the_analyst_trace(data):
    model = build_web.build_model(data)
    evidence = next(
        row
        for find in model["finds"]
        for row in find["evidence"]
    )

    assert evidence["observationId"]
    assert evidence["id"] == evidence["observationId"][:8]
    assert isinstance(evidence["rank"], int)
    assert "sourceName" in evidence
    assert "subject" in evidence


def test_memory_engine_explains_the_analyst_in_plain_language():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "6 market questions" in html
    assert "How the Analyst arrived here" in html
    assert "These are CockroachDB memory searches, not six internet searches" in html
    assert "No Analyst run receipt in this snapshot" in html
    assert "See the 6 market questions" in html
    assert "See cited evidence" in html
    assert "memoryAnalystReceiptForFind" in html
    assert "This is not zero tokens" in html
    assert "signals searchable" in html


def test_shared_memory_sidebar_uses_the_same_find_scoped_analyst_receipt():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    source = html[html.index("function memoryRetrievalReceipt"):]
    source = source[:source.index("function renderOwnerMemoryDisclosure")]

    assert "memoryAnalystReceiptForFind(model, selectedFind)" in source
    assert "No matching Analyst run receipt" in source


def test_memory_engine_can_expand_each_analyst_output_to_its_evidence():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "Why this recommendation exists" in html
    assert "Recommended move" in html
    assert "Cited market evidence" in html
    assert 'class="analyst-output-item' in html

# ---------------------------------------- visual Memory Engine mode


def test_memory_engine_removes_the_ambiguous_attention_summary_card():
    """The operator header should not reduce several different handoffs to one
    unexplained 'Need attention' KPI. Attention remains visible on the owner
    and agent statuses where the operator can act on it."""
    source = APP[APP.index("function renderPortfolioKpis") :]
    source = source[: source.index("function renderMemoryPortfolioSummary")]
    assert 'label: "Need attention"' not in source
    assert "Scan every business owner across the same five handoffs" not in APP


def test_memory_engine_has_a_workflow_visual_mode_toggle():
    """Operators can switch between the detailed workflow and a visual status
    map without leaving Memory Engine or losing the selected owner."""
    assert 'id="memoryViewToggle"' in APP
    assert 'data-memory-view-mode="workflow"' in APP
    assert 'data-memory-view-mode="visual"' in APP
    assert 'id="memoryWorkflowView"' in APP
    assert 'id="memoryVisualView"' in APP
    assert "function setMemoryViewMode" in APP


def test_visual_memory_mode_tracks_each_owner_from_the_same_live_model():
    """The graph may be more visual, but it must derive every owner and stage
    from the same workspaces and stage function as the traceable table view."""
    assert 'id="memoryVisualOwnerList"' in APP
    assert 'id="memoryVisualPipeline"' in APP
    assert 'id="memoryVisualCharts"' in APP
    source = APP[APP.index("function renderMemoryVisual") :]
    source = source[: source.index("function filterOwnerPipelines")]
    assert "workspaces.map" in source
    assert "deriveMemoryStages" in source
    assert "memoryFocus" in source
    assert "memoryEngineSelectedId" in source


def test_visual_memory_mode_contains_real_data_charts_not_decorative_metrics():
    """The visual workspace should expose the three operator questions: what is
    waiting, what memory informed it, and what prior work is being measured."""
    for chart_id in ("visualDecisionChart", "visualMemoryChart", "visualOutcomeChart"):
        assert f'id="{chart_id}"' in APP
    for label in ("Owner decisions", "Market memory", "Outcome ledger"):
        assert label in APP
    assert "model.kinds" in APP
    assert "model.summary" in APP


def test_visual_memory_animation_has_an_accessible_reduced_motion_path():
    assert ".visual-flow-particle" in APP
    reduced = APP[APP.rindex("@media (prefers-reduced-motion: reduce)") :]
    assert ".visual-flow-particle" in reduced
    assert "animation: none" in reduced

# ---------------------------------------- CockroachDB memory / token proof


def test_memory_engine_prominently_shows_the_memory_to_context_funnel():
    """The competition proof belongs above the fold, not behind a technical dialog."""
    assert 'id="memoryEfficiencyHero"' in APP
    assert "CockroachDB memory advantage" in APP
    assert "Retrieve first. Reason second." in APP
    assert "persistent memories" in APP
    assert "memory rows in context" in APP
    assert "evidence rows saved" in APP
    assert "function memoryEfficiencySnapshot" in APP


def test_memory_engine_keeps_row_reduction_separate_from_actual_model_tokens():
    """A retrieval-row reduction is evidence of context selection, not a fabricated
    provider bill. Actual input/output usage must come from the linked agent_run."""
    assert "Actual Analyst token receipt" in APP
    assert "Context reduction is row-based" in APP
    assert "Exact provider token usage is shown separately" in APP
    assert "This historical snapshot has no linked Analyst token receipt. It is not zero" in APP
    source = APP[APP.index("function memoryEfficiencySnapshot") :]
    source = source[: source.index("function renderMemoryEfficiencyHero")]
    assert "run.inputTokens" in source
    assert "run.outputTokens" in source
    assert 'receiptLabel: hasTokenReceipt' in source


def test_portfolio_kpis_aggregate_memory_efficiency_across_all_owners():
    """The top KPI strip is a portfolio summary, never the currently selected owner.

    Per-owner retrieval details stay in the selected-owner hero and graph, while
    the headline context-reduction and token figures combine all owner accounts.
    """
    assert "function memoryPortfolioEfficiencySnapshot" in APP
    source = APP[APP.index("function memoryPortfolioEfficiencySnapshot") :]
    source = source[: source.index("function setMemoryViewMode")]
    assert "map(memoryEfficiencySnapshot)" in source
    assert "contextBound / searchable" in source
    assert 'note: "All owners"' in source
    assert "tokenReceiptCount" in source
    assert "Selected owner ·" in APP


def test_visual_memory_view_has_an_owner_scoped_token_efficiency_chart():
    assert 'id="visualTokenChart"' in APP
    assert "function renderVisualTokenEfficiency" in APP
    source = APP[APP.index("function renderMemoryVisual") :]
    source = source[: source.index("function openMemoryOperationsStage")]
    assert "renderVisualTokenEfficiency(model)" in source
    assert "renderMemoryEfficiencyHero(workspaces, model)" in source


def test_memory_engine_identifies_sql_workflow_refresh_as_zero_llm_tokens():
    # The dedicated KPI card was removed at the owner's request, but the claim
    # it made still has to be stated plainly — it lives in the footer note.
    assert "0 LLM tokens" in APP
    assert "/workflow" in APP
    assert "SQL-only CockroachDB read" in APP


def test_login_offers_owner_and_operator_roles_with_separate_sessions():
    """An operator holds an owner session and an admin session at once, one per
    window. The login page is where the role is chosen, and each role lands in
    its own localStorage slot so neither login evicts the other."""
    html = (build_web.SITE / "login.html").read_text(encoding="utf-8")

    assert 'data-role="owner"' in html
    assert 'data-role="operator"' in html
    assert 'ADMIN_SESSION_KEY = "brass-tacks-admin-session-v1"' in html
    # Operator lands in the admin slot and opens the admin window; owner keeps
    # the original slot and the plain board.
    assert "operator ? ADMIN_SESSION_KEY : SESSION_KEY" in html
    assert '"../app/?workspace=admin"' in html


def test_app_routes_each_window_to_its_own_session_slot():
    """?workspace=admin points a window at the admin slot; without it the window
    is the owner's board. Everything downstream keys off SESSION_KEY, so a
    single flag routes the whole window and signing out of one leaves the
    other's token in place."""
    assert 'ADMIN_SESSION_KEY = "brass-tacks-admin-session-v1"' in APP
    assert 'appQuery.get("workspace") === "admin"' in APP
    assert "SESSION_KEY = ADMIN_VIEW ? ADMIN_SESSION_KEY : OWNER_SESSION_KEY" in APP


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_the_credentials_page_creates_the_account():
    """Signup is two pages, and this is why.

    Both fields lived on the profile form until 2026-08-02. Splitting the flow
    the obvious way — credentials on page one, profile on page two — would have
    meant carrying the password across a navigation in sessionStorage, i.e. a
    plaintext credential in browser storage for as long as the owner takes to
    fill in a form. Creating the account on page one avoids the question: the
    password is posted once and the owner arrives at the profile holding a
    session token instead.
    """
    html = (build_web.SITE / "register.html").read_text(encoding="utf-8")
    for field_id in ("username", "password", "inviteCode"):
        assert f'id="{field_id}"' in html, field_id
    assert 'autocomplete="new-password"' in html
    assert "registerEndpoint" in html
    assert 'window.location.href = "../signup/"' in html


def test_the_profile_page_no_longer_touches_the_password():
    html = (build_web.SITE / "signup.html").read_text(encoding="utf-8")

    assert 'id="password"' not in html
    assert 'type="password"' not in html
    assert 'id="inviteCode"' not in html


def test_the_profile_page_requires_a_session():
    """Without one the API answers 401, so filling in three steps first would
    lose the lot. The one exception is the guided demo's walkthrough (?tour=),
    which needs no session precisely because it never calls the API."""
    html = (build_web.SITE / "signup.html").read_text(encoding="utf-8")

    assert 'if (!currentSession() && !TOUR) window.location.replace("../register/");' in html
    assert "Bearer ${session.token}" in html


def test_no_page_writes_a_password_to_local_storage():
    """The profile page spreads the whole profile into localStorage. That was a
    plaintext-password hazard while the form collected one; now it cannot be,
    because the field is gone. Pinned so re-adding it is a test failure."""
    for name in ("signup.html", "register.html", "login.html"):
        html = (build_web.SITE / name).read_text(encoding="utf-8")
        body = html.split("localStorage.setItem", 1)
        if len(body) < 2:
            continue
        assert 'id="password"' not in html or "password:" not in body[1][:300], name


def test_the_login_page_posts_to_the_login_endpoint():
    html = (build_web.SITE / "login.html").read_text(encoding="utf-8")

    assert "loginEndpoint" in html
    assert 'autocomplete="current-password"' in html
    # One message for both failures, matching the API. Naming which half was
    # wrong would tell anyone probing it which usernames exist.
    assert "payload.error" in html


def test_the_login_page_stores_only_the_session():
    html = (build_web.SITE / "login.html").read_text(encoding="utf-8")
    stored = html.split(
        "localStorage.setItem(operator ? ADMIN_SESSION_KEY : SESSION_KEY,", 1
    )[1][:220]

    assert "token:" in stored
    assert "businessId:" in stored
    assert "password" not in stored


# ---------------------------------------------------------------------------
# Sign in with Google
# ---------------------------------------------------------------------------
#: Every variable that can put a Google endpoint into the model. Cleared
#: together, because `build_web` derives the two Google URLs from the decision
#: endpoint when they are not given explicitly — so unsetting only the obvious
#: two proves nothing.
_GOOGLE_BUILD_VARS = (
    "GOOGLE_START_API_ENDPOINT",
    "GOOGLE_COMPLETE_API_ENDPOINT",
    "GOOGLE_OAUTH_ENABLED",
    "DECISION_API_ENDPOINT",
)


def _model_without_google(payload):
    """The model a build with no OAuth client produces.

    Built in-process from a cleaned environment. Reading `web/` instead makes
    the assertion depend on how that directory last happened to be built, which
    is a different thing from what the code does.
    """
    saved = {name: os.environ.pop(name, None) for name in _GOOGLE_BUILD_VARS}
    try:
        return build_web.build_model(copy.deepcopy(payload))
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value



def test_the_google_button_is_not_drawn_unless_it_was_switched_on(data):
    """A build with no OAuth client must produce no button.

    Nothing at build time can tell whether a client actually exists in the
    Google console, so `GOOGLE_OAUTH_ENABLED` is the single switch. Without it
    the endpoint is null and `.alt-auth` never gets `.available` — a button that
    404s is worse than no button.
    """
    # Built here with the switch explicitly off, rather than read out of web/.
    # That directory is whatever the last build left behind — and since deploys
    # now run from this machine, the last build is usually a real one with live
    # endpoints spliced in, which made this assertion fail for a reason that had
    # nothing to do with the code under test.
    model = _model_without_google(data)
    assert model["api"]["googleStartEndpoint"] is None
    assert model["api"]["googleCompleteEndpoint"] is None

    # The markup ships either way; only the class that reveals it is withheld.
    source = (build_web.SITE / "register.html").read_text(encoding="utf-8")
    assert 'class="alt-auth"' in source
    assert 'getElementById("altAuth").classList.add("available")' in source


def test_the_google_button_still_demands_the_invite_code():
    """The gate is a spend control, not a formality — a workspace created
    through Google runs the same nightly models as one created with a password.
    An OAuth path around it would be a hole in the budget."""
    html = (build_web.SITE / "register.html").read_text(encoding="utf-8")
    handler = html.split('googleButton.addEventListener', 1)[1]

    assert "if (!inviteCode)" in handler
    assert "invite=${encodeURIComponent(inviteCode)}" in handler
    # The check has to come before the navigation, not alongside it.
    assert handler.index("if (!inviteCode)") < handler.index("window.location.href")


def test_every_workflow_that_publishes_the_site_passes_the_google_switch():
    """Two workflows build and upload `web/`, and both must build it the same.

    On 2026-08-07 they did not. `deploy-frontend.yml` resolved
    `GOOGLE_OAUTH_ENABLED=1` and produced a page with the button; `deploy.yml`
    passed only the decision endpoint, and its later upload replaced that page
    with one where the button was switched off. Nothing failed — every other
    endpoint survived because `build_web.py` derives them from the decision
    endpoint, and the switch is the one value with no fallback.

    So the rule is about the pair, not about either file: whatever runs
    `build_web.py` and then syncs to S3 has to carry the switch.
    """
    for name in ("deploy.yml", "deploy-frontend.yml"):
        workflow = (build_web.REPO / ".github" / "workflows" / name).read_text(
            encoding="utf-8")
        if "aws s3 sync web/" not in workflow:
            continue

        assert "GOOGLE_OAUTH_ENABLED:" in workflow, (
            f"{name} uploads web/ but never passes GOOGLE_OAUTH_ENABLED, so "
            f"the site it publishes has no Sign in with Google button")
        # Resolved from the OAuth client actually existing, not hard-coded on.
        assert "GOOGLE_OAUTH_CLIENT_ID" in workflow


def test_the_sign_in_page_offers_google_under_the_same_switch(data):
    """The button belongs on /login/ too — an owner who created a workspace
    with Google has no password to come back with.

    Gated identically to /register/: the markup always ships, and only the
    class that reveals it depends on the build having been told a client
    exists.
    """
    source = (build_web.SITE / "login.html").read_text(encoding="utf-8")

    assert 'class="alt-auth"' in source
    assert 'getElementById("altAuth").classList.add("available")' in source
    # Same reason as the register test above: built from a controlled
    # environment rather than read out of whatever web/ currently holds.
    assert _model_without_google(data)["api"]["googleStartEndpoint"] is None


def test_the_sign_in_page_asks_google_for_a_sign_in_not_a_sign_up():
    """`intent=login` is what tells the backend to skip the invite check — and
    therefore what tells the callback it may not create an account. The sign-in
    page has no invite field, so it must never claim to have one."""
    html = (build_web.SITE / "login.html").read_text(encoding="utf-8")
    handler = html.split("googleButton.addEventListener", 1)[1].split("});", 1)[0]

    assert "?intent=login" in handler
    # Not anywhere on the page: there is no field to read one from, and a page
    # that sent one would be claiming a check nobody ran.
    assert "invite=" not in html


def test_the_sign_in_page_says_so_when_the_google_account_is_unknown():
    """The callback sends them back here rather than creating a workspace off
    an uninvited sign-in. A silent bounce would read as a broken button."""
    html = (build_web.SITE / "login.html").read_text(encoding="utf-8")

    assert '"no-account"' in html
    assert '"cancelled"' in html


def test_the_landing_page_never_reads_a_token_from_the_url():
    """The callback hands over a one-time code, never a session token.

    A token in a redirect is a token in browser history and in the next
    request's Referer, which is the capability-URL pattern CLAUDE.md rejected.
    The page must trade the code over POST and store only what comes back.
    """
    html = (build_web.SITE / "auth-complete.html").read_text(encoding="utf-8")

    assert 'window.location.hash' in html
    assert '.get("code")' in html
    # Nothing reads a token out of the URL, in either half of it.
    assert "get(\"token\")" not in html
    assert "hash).get(\"token\")" not in html
    assert 'method: "POST"' in html


def test_the_landing_page_strips_the_code_before_it_awaits_anything():
    """Single-use or not, the code should not sit in the address bar or in
    history while the exchange is in flight."""
    html = (build_web.SITE / "auth-complete.html").read_text(encoding="utf-8")
    body = html.split("async function finish()", 1)[1]
    stripped = body.index("history.replaceState")

    assert stripped < body.index("await fetch")


def test_the_landing_page_stores_only_the_session():
    html = (build_web.SITE / "auth-complete.html").read_text(encoding="utf-8")
    stored = html.split("localStorage.setItem(SESSION_KEY", 1)[1][:220]

    assert "token:" in stored
    assert "businessId:" in stored
    assert "password" not in stored


def test_the_landing_page_sends_owners_without_a_business_to_onboarding():
    """Same fork as /register: an account exists, a business may not."""
    html = (build_web.SITE / "auth-complete.html").read_text(encoding="utf-8")

    assert 'payload.business_id ? "../../app/" : "../../signup/"' in html


def test_the_landing_page_offers_a_way_back_that_signs_you_in():
    """By the time anyone reaches this page the callback has already found or
    created their account — so a failure here is a spent or expired handoff
    code, not a missing workspace. Sending them to sign-up would offer to make
    a second account for someone who already has one, and since the button now
    also sits on /login/, the page they left from is as likely to be that."""
    html = (build_web.SITE / "auth-complete.html").read_text(encoding="utf-8")
    link = html.split('class="retry"', 1)[1].split("</a>", 1)[0]

    assert 'href="../../login/"' in link
    assert "sign-up" not in link


def test_the_landing_page_is_built_even_when_the_button_is_off():
    """Google matches the redirect URI exactly. A live OAuth client whose
    callback lands on a 404 is a worse failure than an unlinked page."""
    assert (build_web.OUT_DIR / "auth" / "complete" / "index.html").exists()


def test_the_board_sends_the_session_token():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "authHeaders(headers)" in html          # the live read
    assert 'authHeaders({ "Content-Type": "application/json" })' in html  # the write
    assert "Bearer ${session.token}" in html


def test_the_board_requires_a_session():
    """This asserted the opposite until 2026-08-02, and it is worth saying why.

    Rendering the committed snapshot to anyone was a defensible public demo
    while there was one seeded tenant: the README leads with "see it in 30
    seconds with no credentials". Once businesses sign themselves up, that
    snapshot is one specific business, and showing it to every visitor as their
    own board is the exact failure multi-tenancy exists to prevent — a new owner
    finished onboarding and was shown another restaurant's recommendations.

    The no-credentials demo now belongs on the landing page, which needs no
    session and claims to be nobody's data.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "if (!readSession()) {" in html
    # An owner window goes to the plain login; an admin window is sent back
    # preselected on Operator. Either way, no session means the login page.
    assert 'window.location.replace(ADMIN_VIEW ? "../login/?role=operator" : "../login/")' in html


def test_a_business_with_no_night_is_not_told_it_is_caught_up():
    """Two different empty states wore the same words.

    "All caught up" tells the owner the agents ran and found nothing worth
    saying. For a business that has never had a night, nothing has ever looked
    at it — and the only trigger was a button at the end of onboarding they had
    already walked past, so the board was a dead end.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "The agents haven't looked yet" in html
    assert "data-start-night" in html
    # From the data, not a flag someone can forget to set: no finds and no runs
    # means nothing has ever looked.
    assert "const neverRan = !myFinds.length && !myRuns.length;" in html


def test_the_empty_state_asks_about_the_signed_in_tenant():
    """btData is the committed snapshot and belongs to whichever tenant was
    exported last. Reading it here told a brand-new business it was "All caught
    up" — on somebody else's three finds — seconds after its owner pressed the
    button. The live workspace is the only honest source."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "const mine = liveMine || btData;" in html
    assert "liveWorkflowWorkspaces[0]" in html
    # The old form must not come back.
    assert "const neverRan = !(btData.finds || []).length" not in html


def test_a_night_in_progress_says_so():
    """A night takes 60-90 seconds and the board polls every 15. Without this
    the owner pressed the button, saw a terminal-sounding message, and
    reasonably concluded nothing had happened."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "The agents are working" in html
    # Bounded: see test_the_board_will_not_claim_work_from_an_abandoned_run for
    # why an open run row alone stopped being good enough evidence.
    assert 'const openRuns = myRuns.filter(run => String(run.status) === "running");' in html
    # Same animated ellipsis the trigger button uses.
    assert 'class="working-dots"' in html


def test_the_board_can_start_a_night_for_the_signed_in_business():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "runEndpoint" in html
    assert "startNight" in html
    # Same guard as everywhere else: the tenant comes from the session, so the
    # request carries no business id to spend someone else's money against.
    start = html.split("async function startNight", 1)[1][:900]
    assert "authHeaders(" in start
    assert "business_id" not in start


# ---------------------------------------------------------------------------
# The card is a fixed shape; agent prose is not
# ---------------------------------------------------------------------------

def test_the_card_bounds_every_axis_that_can_overflow():
    """A find overflowed the card twice, and the second time the price and the
    Do it / Pass buttons ended up underneath the move list.

    CLAUDE.md records the first occurrence as the reason main was reverted
    rather than the card retuned. The model is asked for one short sentence and
    card_summary cuts at 180 characters, but neither is enforcement — a find
    written before summaries existed falls back to its rationale, which runs to
    a paragraph. This is the enforcement.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    for selector in (".signal-text", ".post-title", ".recommendation-steps li"):
        block = html.split(selector + " {", 1)
        assert len(block) == 2, f"{selector} rule is missing"
        rule = block[1].split("}", 1)[0]
        assert "-webkit-line-clamp" in rule, selector
        assert "overflow: hidden" in rule, selector


def test_the_live_read_carries_the_summary():
    """The card falls back to the full rationale without it, which is exactly
    how the deck overflowed after the board started reading live data."""
    source = (build_web.REPO / "backend" / "src" / "brasstacks"
              / "workflow_snapshot.py").read_text(encoding="utf-8")

    assert "emoji, title, summary," in source, "the SQL must select it"
    assert '"summary": card_summary(raw_find)' in source, "and the mapping must expose it"


def test_every_path_to_the_card_uses_the_same_summary_limit():
    """Three files compute this. They have to agree, or the same find renders
    at different lengths depending on whether the board is reading the
    committed snapshot or the live cluster."""
    from brasstacks import finds as finds_module
    from brasstacks import workflow_snapshot

    assert (build_web.SUMMARY_MAX_CHARS
            == finds_module.SUMMARY_MAX_CHARS
            == workflow_snapshot.SUMMARY_MAX_CHARS)


def test_inline_icons_cannot_paint_at_their_unsized_default():
    """A tab switch rebuilds the panel with innerHTML. An <svg> with a viewBox
    and no width/height has no intrinsic size and falls back to the
    replaced-element default of 300x150 for the frame before its container's
    sizing rule matches — a giant zig-zag arrow (the `demand` icon: a zig-zag
    path plus an arrow head) flashing across the view on every switch.

    `svg { max-width: 100% }` was not enough on its own: the container is wide
    mid-transition, so 100% of it is still enormous. An intrinsic size is what
    actually bounds it, and a presentation attribute is the weakest possible
    source, so every `.thing svg { width: 17px }` rule still wins.
    """
    import re

    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "svg { max-width: 100%; }" in html
    unsized = re.findall(r"<svg (?!width=)[^>]*viewBox=", html)
    assert not unsized, f"{len(unsized)} inline svg(s) have no intrinsic size"


def test_the_board_offers_a_way_to_sign_out():
    """Without one the session lives fourteen days and the machine is stuck on
    one tenant — which makes onboarding a second business impossible to test,
    and leaves a shared computer signed in to somebody's revenue figures."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'id="signOut"' in html
    assert "endSession()" in html
    # Landing, not login: signing out is often the start of onboarding a
    # different business, and a form asking for the credentials just abandoned
    # is a dead end. The board's own guard still sends an unauthenticated
    # visitor to /login/ — see test_the_board_requires_a_session.
    after = html.split("function endSession()", 1)[1][:900]
    assert 'window.location.href = "../";' in after
    # Best effort at the server too, but signing out must not depend on it.
    assert "/logout" in html
    assert "keepalive: true" in html


def test_the_welcome_screen_shows_once_not_forever():
    """Onboarding is an event, not a state.

    This keyed off the onboarding profile in localStorage, which signup writes
    and nothing ever removes — so every later visit showed the welcome screen
    and its "Preview sample recommendations" link, even for a business with real
    finds behind it. Signing back in a week later put the owner on day one.

    The signup redirect already carried ?onboarded=1 and nothing read it. A
    query parameter survives exactly one navigation, which is the length of the
    moment being described.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'const justOnboarded = appQuery.get("onboarded") === "1";' in html
    assert "onboardingProfile && justOnboarded && !sampleWorkspaceMode" in html
    # The old form must not come back.
    assert "Boolean(onboardingProfile && !sampleWorkspaceMode)" not in html


def test_each_audience_sees_only_its_own_views():
    """An owner and an operator are two products sharing one page.

    The operator owns no business, so For You, Growth, Supplies and Chat
    would be empty for them — their session carries no tenant and the workflow
    endpoint answers 401 by design. An owner must never see the Memory Engine,
    which reads across tenants.

    Supplies is one of those owner views in its own right. It shows one
    tenant's standing orders, spend limits and receipts, so it belongs
    wherever For You and Growth belong and nowhere else.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "const OPERATOR_SESSION = Boolean(readSession()?.isAdmin);" in html
    assert ('OPERATOR_SESSION\n        ? ["autopilot", "growth", "orders", "chat"]'
            in html)
    assert ': ["admin"];' in html
    # Several call sites ask for "autopilot" by name; one guard inside
    # switchView beats four at the call sites, and cannot be forgotten at a fifth.
    assert 'if (OPERATOR_SESSION) viewName = "admin";' in html


def test_supplies_is_folded_into_the_agent_canvas():
    """Supplies is a tab inside the agent's canvas, not a separate page.

    It was a primary view for a while, on the reasoning that ordering is a
    place you go rather than a mode a conversation is in. The owner reversed
    that: they want one agent that holds both the conversation and the board,
    toggled like This move | Supplies, and the assistant renamed "My agent".
    The earlier decision is preserved in git history; this is the current one.

    So there is no Supplies view-switcher tab, the canvas offers a supplies
    panel, and the board is relocated into it at startup.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    # No Supplies tab in the primary switcher; the assistant is "My agent".
    switcher = html.split('<nav class="view-switcher"', 1)[1].split("</nav>", 1)[0]
    assert 'data-view="orders"' not in switcher
    assert "<span>Supplies</span>" not in switcher
    assert "<span>My agent</span>" in switcher

    # The canvas offers a Supplies tab and a panel to host the board.
    assert 'data-canvas="supplies"' in html
    assert 'id="chatCanvasSupplies"' in html
    # And the board is folded into that panel at startup.
    assert "host.appendChild(board)" in html
    assert 'getElementById("chatCanvasSupplies")' in html


def test_the_supplies_stat_bar_sits_on_the_supplies_screen():
    """The four numbers head the screen they describe.

    Spent this week and Low stock are facts about the pantry, so they belong
    above the pantry, not above a conversation. They are also read-only
    headers now rather than buttons: on their own screen there is nowhere
    left for them to navigate to, and a control that does nothing is worse
    than a label.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    orders = html[html.index('id="view-orders"'):html.index('id="view-admin"')]
    assert 'id="ordersStats"' in orders
    # Top of the screen, under the honesty banner — the disclosure qualifies
    # the numbers, so it must not be pushed below them.
    assert orders.index("orders-mock-banner") < orders.index('id="ordersStats"')
    assert orders.index('id="ordersStats"') < orders.index('id="ordersAskForm"')

    # The chat no longer carries them, and no longer owns the renderer: the
    # cross-module hook ran before the chat defined it, so a preview build
    # rendered the board with no bar above it.
    assert 'id="chatStats"' not in html
    assert "chat-stat" not in html
    assert "__btChatOverview" not in html

    stats = html.split("function renderOrdersStats(figures)", 1)[1].split(
        "\n      }", 1)[0]
    assert "Spent this week" in stats
    assert "Low stock" in stats
    assert "ordersStats" in stats
    # Read-only: tiles, not buttons wired to a canvas that no longer exists.
    assert "<button" not in stats
    assert "data-canvas" not in stats

    # Both modes fill it. The preview's numbers are sample data like the rest
    # of that screen, and its banner already says so.
    live = html.split("function ordersLive()", 1)[1].split(
        "function ordersPreview()", 1)[0]
    preview = html.split("function ordersPreview()", 1)[1]
    assert "renderOrdersStats(" in live
    assert "renderOrdersStats(" in preview


def test_the_agent_canvas_rests_on_the_supplies_board():
    """With Supplies folded in, the board is the canvas's resting state.

    The canvas shows a move when one is open; otherwise it shows the Supplies
    board, which is the useful default now that ordering and stock live here.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    # setCanvas knows the supplies panel.
    canvas = html.split("function setCanvas(mode)", 1)[1].split("\n      }", 1)[0]
    assert 'suppliesPanel.hidden = mode !== "supplies"' in canvas
    # Resting default is the board, not the retired empty state.
    assert 'setCanvas("supplies")' in html
    assert 'setCanvas(chatContext ? "move" : "supplies")' in html


def test_an_order_placed_in_chat_reaches_the_supplies_canvas():
    """The Quartermaster answers in chat; its receipt is on the Supplies tab.

    A placed order offers the trip to Supplies, which now opens the board in
    the agent's own canvas (switchView redirects the old "orders" target)
    rather than a separate screen.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    # The board still gets the fresh state even while another canvas shows.
    assert "window.__btOrdersApplyState(answer.state)" in html
    # The message still carries a way there; switchView redirects it to the
    # agent's Supplies canvas.
    assert 'action: { label: "Open Supplies", view: "orders" }' in html
    assert "window.__btShowSuppliesCanvas" in html
    assert 'data-goto-view="${message.action.view}"' in html
    assert 'const goto = event.target.closest("[data-goto-view]")' in html


def test_chat_and_supplies_share_the_midnight_shell_without_sharing_the_board():
    """Chat must not drop back to the light canvas.

    Every owner screen wears one midnight surface. Chat used to get its by
    borrowing body.orders-mode wholesale, which also dragged in the whole
    .orders-* board treatment — fine while the board was literally inside the
    chat canvas, wrong once it moved out. Splitting them left chat on the
    light default, which read as a beige page among dark ones.

    So the shell — tokens, background, topbar chrome — is written for both
    modes, and the board rules stay on orders-mode alone.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    # The token block and page background.
    assert "body.orders-mode,\n    body.chat-mode {" in html
    assert "body.orders-mode::before,\n    body.chat-mode::before {" in html

    # The chrome that sits above every view, not inside one.
    for selector in (".topbar", ".view-switcher", ".view-tab"):
        assert (f"body.orders-mode {selector},\n    body.chat-mode {selector} "
                in html), selector

    # The board's own styling stays where the board is.
    assert "body.chat-mode .orders-zone" not in html
    assert "body.chat-mode .orders-row" not in html
    assert "body.chat-mode .view-inner.orders-inner" not in html


def test_the_live_supplies_board_can_speak_on_its_own_screen():
    """A refusal must appear where the owner is looking.

    The board's note() routed every message to the chat thread, which was
    right while the board sat inside the chat canvas — the two were one
    screen. Since the board moved to its own tab, an approval that fails on
    limits, a price that moved, or an expired session all announced
    themselves on a tab the owner is not on, while the button quietly
    re-enabled. Nothing visible happened, so the owner clicks again.

    The inline note has to survive live mode for that reason, which means
    hiding the ask *input* rather than the whole command zone that contains
    it.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    speak = html.split("function note(text, warn)", 1)[1].split("\n        }", 1)[0]
    # The inline note is written first and unconditionally — no early return
    # that depends on the chat module being present.
    assert speak.index('el("ordersAskNote")') < speak.index("__btChatNotify")
    assert "return;\n          }\n          const target" not in speak

    # Live mode must not hide the element that carries it.
    chrome = html.split("function applyChrome()", 1)[1].split("\n        }", 1)[0]
    assert 'querySelector(".orders-zone-command")' not in chrome
    assert 'el("ordersAskForm")' in chrome


def test_the_projection_caption_uses_the_count_the_build_computed():
    """The chart's caption is a claim about which finds the forecast rests on.

    build_model already writes that sentence into the projected month's
    `note`, naming one find or N. The page ignored it and hardcoded "One move
    is still being measured", so a projection resting on three pending finds
    told the owner it rested on one — a false statement about their money on
    the one chart that forecasts it.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "One move is still being measured" not in html
    # Two blocks read #chartNote: the empty-history one, then the real chart.
    # The second is the one that draws a projection.
    caption = html.split('const note = document.getElementById("chartNote");')[2]
    caption = caption.split("note.hidden = true;", 1)[0]
    assert "projected.note" in caption
    assert "escapeHtml(rests)" in caption


def test_the_doordash_screen_admits_it_is_a_preview():
    """The static build's DoorDash screen is sample data, and must say so.

    The storage layer has landed: deployed with an /orders endpoint, the screen
    renders CockroachDB rows and swaps this banner at runtime for the live one
    (see test_the_doordash_screen_is_live_when_the_deployment_gives_it_an_api).
    But a fresh clone builds with no endpoint and renders the preview, so the
    static banner stays, and stays specific — a vaguer "demo data" note would
    let a reader assume the connection is real and only the numbers are
    illustrative.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    banner = html[html.index('id="view-orders"'):html.index('id="view-admin"')]

    assert "orders-mock-banner" in banner
    assert "Preview, not live." in banner
    # The three specific claims. A vaguer "demo data" note would let a reader
    # assume the connection is real and only the numbers are illustrative.
    assert "nothing here is saved" in banner
    assert "no DoorDash account is" in banner
    assert "no order can be placed" in banner


def test_removing_a_tab_happens_before_handlers_are_bound():
    """viewTabs is captured into a static array. Removing a tab afterwards
    would leave its click handler bound to a detached node."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert html.index("gateViewsToTheAudience") < html.index("const viewTabs =")


def test_gating_removes_the_tab_but_never_the_view():
    """Removing the hidden views broke the Memory Engine outright.

    Module-scope lookups reach inside them — `queueContent` lives in
    #view-autopilot, and growthChart / growthChartSub / growthSwipeTrack in
    #view-growth. For an operator those resolved to null, and the first
    `.textContent` on one threw, aborting the script and taking the console's
    own render down with it.

    A view with no tab is already unreachable, and carries no `.active` class
    so it is display:none regardless. Removing the tab was all this needed.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    gate = html.split("function gateViewsToTheAudience", 1)[1][:1200]

    assert "tab.remove()" in gate
    assert "view.remove()" not in gate
    # And the elements the rest of the file depends on are still declared.
    for element_id in ("view-autopilot", "view-growth", "view-admin"):
        assert f'id="{element_id}"' in html, element_id


def test_an_operator_lands_on_the_console():
    """The starting view is decided by the markup — #view-autopilot ships with
    `.active` — and switchView's guard only fires once something calls it.
    Without setting it here an operator landed on a recommendation card for a
    business they do not own, and had to click Memory Engine themselves."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    gate = html.split("function gateViewsToTheAudience", 1)[1][:2200]

    assert 'view.id === "view-admin"' in gate
    assert 'tab.dataset.view === "admin"' in gate
    assert 'document.body.classList.add("console-mode")' in gate
    # And the module's own record of where it is agrees from the start.
    assert 'const INITIAL_VIEW = OPERATOR_SESSION ? "admin" : "autopilot";' in html
    assert "let activeView = INITIAL_VIEW;" in html
    assert 'let activeView = "autopilot";' not in html


def test_the_topbar_names_the_signed_in_tenant():
    """setBrandBusiness defaulted to btData — the committed snapshot — so the
    topbar read whichever tenant was exported last. Signed in as one business
    it showed another's name under the logo."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "function setBrandBusiness(model = null)" in html
    assert "function setBrandBusiness(model = btData)" not in html
    # And it is re-set once the tenant's own data arrives, not only at first paint.
    live = html.split("syncPostsAndDecisionsFromWorkflow(liveWorkflowWorkspaces);", 1)[1][:400]
    assert "setBrandBusiness();" in live


def test_no_fit_level_reintroduces_the_descender_clipping():
    """fitFeedCards() shrinks type by setting data-fit on the feed-card, and
    those rules carry an attribute selector — so they outrank a plain
    `body.autopilot-mode .post-copy h2` rule on specificity. One of them sets
    line-height:1.02, which is where the sheared descenders came back from: the
    earlier fix only ever applied to a card that had not been fitted.

    Checks the built page, because this is a question about the cascade rather
    than about any one rule.
    """
    import re

    html = (build_web.OUT_DIR / "app" / "index.html").read_text(encoding="utf-8")
    rules = re.finditer(r"([^{}]*(?:post-copy h2|post-title)[^{}]*)\{([^}]*)\}", html)
    winner = None
    for match in rules:
        found = re.search(r"line-height:\s*([\d.]+)", match.group(2))
        if found:
            winner = float(found.group(1))

    assert winner is not None, "no line-height rule found for the title"
    assert winner >= 1.15, (
        f"the last line-height that can match the title is {winner}; "
        "descenders will clip on a fitted card"
    )


def test_the_card_title_leaves_room_for_descenders():
    """A -webkit-box's height is lines x line-height and overflow is hidden, so
    anything a glyph extends below its line box is cut. At 1.06 the descender
    on "birthdays" was sheared off."""
    import re

    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    rule = html.split("body.autopilot-mode .post-copy h2.post-title {", 1)[1].split("}", 1)[0]
    [line_height] = re.findall(r"line-height:\s*([\d.]+)", rule)

    assert float(line_height) >= 1.15, "descenders will clip"


# ---------------------------------------------------------------------------
# The committed snapshot is a first paint, never an identity
# ---------------------------------------------------------------------------

def test_the_snapshot_is_scrubbed_when_it_is_not_the_signed_in_tenants():
    """One guard instead of one per reader.

    db/fixtures/demo.json belongs to whichever tenant was exported last. Three
    separate bugs came from reading it as though it were the signed-in
    business: the card showed a full rationale, the empty state told a brand-new
    business it was "All caught up" on someone else's three finds, and the
    topbar named the wrong company. Others were still latent — btData.finds
    seeds the deck and btData.months draws the growth chart, so a new tenant saw
    another business's cards and revenue until the live read landed.

    Guarding each reader is what produced that sequence. The snapshot is now
    emptied of everything tenant-shaped at the point it is parsed.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split("const btData = (() => {", 1)[1].split("})();", 1)[0]

    # The read is slot-aware now (an admin window scrubs against its own token),
    # but it still reads a session to decide whose snapshot this is.
    assert '"brass-tacks-session-v1"' in block
    assert 'get("workspace") === "admin"' in block
    assert "signedInAs === snapshotOf" in block
    assert 'const KEEP = new Set(["api", "backdrop"]);' in block


def test_endpoints_and_assets_survive_the_scrub():
    """`api` is endpoint configuration and `backdrop` an asset path. Neither is
    anyone's data, and dropping them would leave a signed-in owner unable to
    reach the API at all."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split("const btData = (() => {", 1)[1].split("})();", 1)[0]

    assert '"api"' in block and '"backdrop"' in block
    assert "if (KEEP.has(key)) scrubbed[key] = value;" in block


def test_an_anonymous_visitor_still_sees_the_snapshot():
    """No session means nobody is being shown the wrong tenant — the landing
    demo and a first paint before login both depend on it rendering."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split("const btData = (() => {", 1)[1].split("})();", 1)[0]

    assert "if (!signedInAs || !snapshotOf || signedInAs === snapshotOf) return snapshot;" in block


def test_clamped_text_has_descender_room_independent_of_line_height():
    """line-height is a guess about a typeface's descender depth, and it took
    four attempts to get right — the fit-level rules kept overriding it.

    A -webkit-box with overflow:hidden clips at the padding box, not the content
    box, so padding-bottom gives the last line room for its tails without
    revealing any part of the next one: the line count still caps the height.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split("Belt and braces on the clipping", 1)[1].split("}", 1)[0]

    assert "padding-bottom" in block
    for selector in ("post-copy h2", "signal-text", "recommendation-steps li"):
        assert selector in block, selector


def test_invented_cards_are_never_a_fallback_for_an_empty_deck():
    """The deck fell back to hardcoded demoPosts — "Oak & Pine launched a $6.50
    drink-and-snack combo" — whenever it had no real finds to show.

    That was survivable while the page always had a seeded tenant behind it. It
    became a lie once the snapshot could legitimately be empty: an owner who
    signed up ten minutes ago was shown a competitor's invented promotion as
    though the agents had found it for them.

    CLAUDE.md is unambiguous — nothing appears on the deck that is not a row in
    CockroachDB. Samples require ?demo=1, which is a deliberate request for them.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "(sampleWorkspaceMode ? demoPosts : [])" in html
    # The unguarded fallback must not come back.
    assert "memoryPosts.length ? memoryPosts : demoPosts" not in html


def test_an_operator_reads_the_cross_tenant_endpoint():
    """/workflow resolves its tenant from the caller's session, and an operator
    owns none — so it answered 401 and the Memory Engine fell back to the
    committed snapshot, showing exactly one business. The admin endpoint was
    deployed and returning every tenant correctly; nothing was pointed at it."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "const workflowApiEndpoint = OPERATOR_SESSION" in html
    assert 'btData.api?.adminEndpoint' in html
    assert 'btData.api?.workflowEndpoint' in html


def test_the_admin_endpoint_is_built_into_the_page():
    """Without it the operator's board has nowhere to read from."""
    source = (build_web.REPO / "scripts" / "build_web.py").read_text(encoding="utf-8")

    assert '"adminEndpoint": admin_endpoint or None' in source
    assert 'ADMIN_API_ENDPOINT' in source

# ---------------------------------------------------------------------------
# Native swipe and complete mobile reading
# ---------------------------------------------------------------------------


def test_for_you_swipe_waits_for_horizontal_intent_and_uses_velocity():
    """A recommendation deck must not steal a vertical reading gesture.

    The old handler captured the pointer on pointerdown and moved the card on
    every pointermove. On a phone that made a slightly diagonal scroll feel
    like a broken swipe. The replacement locks an axis first, renders on the
    animation frame, and uses distance plus velocity when deciding whether to
    advance.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split("function bindFeedGestures()", 1)[1].split(
        "/**\n     * Arrow keys browse", 1
    )[0]

    assert 'gesture.axis = "vertical"' in block
    assert 'gesture.axis = "horizontal"' in block
    assert "requestAnimationFrame(renderDrag)" in block
    assert "gesture.velocityX" in block
    assert "const predicted = gesture.deltaX + gesture.velocityX" in block
    assert "event.cancelable" in block and "event.preventDefault()" in block


def test_mobile_for_you_keeps_every_word_readable():
    """On a phone, long evidence and recommendations scroll; they never clamp.

    The decision footer stays available while the body owns vertical scrolling.
    This is more important than preserving a fixed-card screenshot: an owner
    cannot approve a move responsibly if the card hides part of the evidence.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    css = html.split('<style id="native-swipe-mobile-v19">', 1)[1].split(
        "</style>", 1
    )[0]
    mobile = css.split("@media (max-width: 640px)", 1)[1]

    assert "touch-action: pan-y pinch-zoom" in css
    assert "overflow-y: auto" in mobile
    assert "-webkit-overflow-scrolling: touch" in mobile
    assert "grid-template-rows: minmax(0, 1fr) auto" in mobile
    assert "-webkit-line-clamp: unset" in mobile
    assert "overflow: visible" in mobile
    for selector in (
        "body.autopilot-mode .post-copy h2",
        "body.autopilot-mode .signal-text",
        "body.autopilot-mode .recommendation-steps li",
    ):
        assert selector in mobile, selector


def test_growth_swipe_uses_the_same_native_gesture_contract():
    """Growth should not feel like a second, rougher carousel."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split("function bindGrowthSwipe()", 1)[1].split(
        "/* ----------------------------------------------------------\n       Memory Engine", 1
    )[0]

    assert 'gesture.axis = "vertical"' in block
    assert 'gesture.axis = "horizontal"' in block
    assert "requestAnimationFrame(renderDrag)" in block
    assert "gesture.velocityX" in block
    assert "const predicted = gesture.deltaX + gesture.velocityX" in block


def test_growth_momentum_heading_is_not_visible():
    """The owner asked to remove the redundant 'Your momentum' card heading."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert ">Your momentum<" not in html
    assert 'id="growthChartHeading" class="sr-only">Growth history</h2>' in html


def test_every_viewport_renders_the_same_owner_summary_and_action_list():
    """Device width changes reading mechanics, never recommendation content."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    parity_css = html.split('<style id="consistent-feed-chat-growth-v23">', 1)[1].split(
        "</style>", 1
    )[0]
    card = html.split('class="signal-text"', 1)[1][:300]
    steps = html.split("function renderSteps(post)", 1)[1].split(
        "function getDecision", 1
    )[0]

    assert "signal: conciseFindSummary(find)" in html
    assert "fullSignal: cleanOwnerCopy(find.rationale" in html
    assert 'escapeHtml(post.signal || "")' in card
    assert "post.fullSignal" not in card
    assert 'class="signal-summary"' not in card
    assert 'class="signal-full"' not in card
    assert ".slice(" not in steps
    assert "step-overflow" not in steps
    assert "step-more" not in steps
    assert "overflow-y: auto !important" in parity_css
    assert "-webkit-line-clamp: unset !important" in parity_css
    assert "body.autopilot-mode .recommendation-steps li" in parity_css




def test_owner_feed_summary_is_concise_and_hides_trace_identifiers():
    """The card is owner communication; evidence ids stay in the trace."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "function cleanOwnerCopy(value)" in html
    assert "function completeOwnerSummary(value, limit = 180)" in html
    assert "function conciseFindSummary(find)" in html
    assert "signal: conciseFindSummary(find)" in html
    assert '<p class="signal-text">${escapeHtml(post.signal || "")}</p>' in html
    assert "concise-owner-feed-v25" in html
    assert 'return stem && !/[.!?]$/.test(stem) ? `${stem}.` : stem' in html


def test_analyst_prompt_defines_a_short_nonredundant_owner_summary():
    source = (build_web.REPO / "backend" / "src" / "brasstacks" /
              "agents" / "analyst.py").read_text(encoding="utf-8")

    assert "ideally 110–160 characters" in source
    assert '"memory", "rows", "records" or "observations"' in source
    assert "include any evidence id" in source
    assert "Do not repeat the title" in source


def test_mobile_landing_story_uses_portrait_compositions_without_clamps():
    html = (build_web.SITE / "landing.html").read_text(encoding="utf-8")
    css = html.split("MOBILE STORY COMPOSITION · V25", 1)[1].split("</style>", 1)[0]

    assert "--story-stage-width" in css
    assert "--story-stage-height" in css
    assert "aspect-ratio: auto" in css
    assert "-webkit-line-clamp: unset" in css
    assert ".node-a" in css and ".node-b" in css and ".node-c" in css
    assert ".move-card h4" in css
    assert ".move-reason p" in css
    assert ".proof-title" in css
    assert "storyFind.title || storyFind.shortTitle" in html
    assert "storyFind.summary || storyFind.rationale" in html


def test_empty_growth_history_shows_an_honest_process_projection():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split("function renderGrowthProjectionPlaceholder", 1)[1].split(
        "function renderRevenueChart", 1
    )[0]

    assert "No measured growth yet" in block
    assert "Approve" in block and "Launch" in block and "Verify" in block
    assert "No revenue is projected or claimed here" not in block
    assert "note.hidden = true" in block
    assert "growth-projection" in block
    assert "Your momentum" not in html


def test_drawer_chat_calls_authenticated_ask_and_has_no_fake_answer():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    chat = html.split("async function loadDrawerChat", 1)[1].split(
        "function escapeHtml", 1
    )[0]

    assert "fetch(`${ASK_ENDPOINT}?find_id=" in chat
    assert "fetch(ASK_ENDPOINT" in chat
    assert "authHeaders" in chat
    assert "question: submittedText" in chat
    assert "find_id: post.databaseId || post.id" in chat
    assert "CockroachDB memory receipt" in html
    assert "The production app would answer" not in html


def test_build_infers_the_ask_endpoint_from_the_shared_api(monkeypatch, data):
    monkeypatch.setenv("DECISION_API_ENDPOINT", "https://example.execute-api.us-east-1.amazonaws.com/v1")
    monkeypatch.delenv("ASK_API_ENDPOINT", raising=False)

    model = build_web.build_model(data)

    assert model["api"]["askEndpoint"] == (
        "https://example.execute-api.us-east-1.amazonaws.com/v1/ask"
    )


def test_build_infers_the_orders_endpoint_from_the_shared_api(monkeypatch, data):
    """The Quartermaster shares the one HTTP API, so its URL derives from the
    decision endpoint like login and profile do. A deployment that forgets the
    explicit variable still gets a live DoorDash screen rather than the
    sample-data preview."""
    monkeypatch.setenv("DECISION_API_ENDPOINT", "https://example.execute-api.us-east-1.amazonaws.com/v1")
    monkeypatch.delenv("ORDERS_API_ENDPOINT", raising=False)

    model = build_web.build_model(data)

    assert model["api"]["ordersEndpoint"] == (
        "https://example.execute-api.us-east-1.amazonaws.com/v1/orders"
    )


def test_the_doordash_screen_is_live_when_the_deployment_gives_it_an_api():
    """Two modes, one honest each way: with an injected endpoint and a session
    the screen renders only rows the /orders API returned from CockroachDB;
    without one it stays the labelled sample-data preview. The dispatcher is
    what keeps the preview banner truthful in both worlds."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    assert "btData.api?.ordersEndpoint" in html
    assert "ordersLive();" in html
    assert "ordersPreview();" in html
    # The live mode announces what is real and what is not, in both payment
    # configurations.
    assert "Live, with one honest exception." in html
    assert "Live, with two honest exceptions." in html
    # The chat can act: a decision spoken in the thread goes through the
    # same decide() every button uses, and ambiguity asks instead of
    # guessing which move gets the owner's yes.
    assert "function parseDecisionIntent" in html
    assert "async function performDecision" in html
    assert "if (await handleDecision(text)) return;" in html


def test_mobile_for_you_uses_native_scroll_snap_instead_of_pointer_drag():
    """Phones should let the browser compositor own the horizontal gesture.

    The desktop stack keeps its pointer-driven card physics. On mobile, native
    scroll snap avoids pointercancel and nested vertical-scroll races that made
    diagonal finger movement jump back or stutter.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    css = html.split('<style id="mobile-feed-production-v22">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "scroll-snap-type: x mandatory" in css
    assert "-webkit-overflow-scrolling: touch" in css
    assert "scroll-snap-align: start" in css
    assert "touch-action: pan-x pan-y pinch-zoom" in css
    assert "function bindNativeMobileFeed(stage)" in html
    assert "if (usesNativeMobileFeed())" in html
    assert 'stage.dataset.gestureBound = "native"' in html


def test_mobile_snap_never_inherits_the_desktop_preview_transform():
    """The card that was `.is-next` stayed scaled and faded after it snapped in.

    The midnight desktop theme is later than the original mobile scroll-snap
    rules, so its 3D preview won the cascade: 97.2% scale plus 42% opacity made
    every second post look blurred. The last mobile block must restore a 1:1
    pixel surface for every deck-position class.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    assert html.index('id="owner-midnight-experience-v34"') < html.index(
        'id="mobile-feed-crisp-snap-v36"'
    )
    css = html.split('<style id="mobile-feed-crisp-snap-v36">', 1)[1].split(
        "</style>", 1
    )[0]

    for state in ("is-current", "is-next", "is-back", "is-hidden", "is-prev"):
        assert f".feed-card.{state}" in css
    assert "opacity: 1 !important" in css
    assert "transform: none !important" in css
    assert "filter: none !important" in css
    assert "will-change: auto !important" in css
    assert "transform-style: flat !important" in css


def test_native_mobile_swipe_updates_the_semantic_current_card():
    """Visual state and accessible state follow the card that actually snapped."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    source = html[html.index("function syncNativeMobileCardStates") :]
    source = source[: source.index("function bindFeedGestures")]

    assert 'card.classList.remove(...CARD_POSITIONS' in source
    assert 'index === activeIndex ? "is-current" : "is-hidden"' in source
    assert 'card.setAttribute("aria-hidden", index === activeIndex ? "false" : "true")' in source
    assert source.count("syncNativeMobileCardStates(stage,") >= 3


def test_decision_buttons_wait_for_the_authenticated_live_workflow():
    """A build-time UUID must never be written before tenant state is verified."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "signedInBusinessId !== snapshotBusinessId" in html
    assert "Checking latest decision status" in html
    assert "Never write a UUID from an unverified build snapshot" in html
    assert "await refreshWorkflowSnapshot({ force: true, silent: true })" in html
    assert "This move was already decided or is no longer available. Feed refreshed." in html


def test_memory_engine_makes_ask_memory_and_analyst_collaboration_explicit():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "Ask · owner memory" in html
    assert "conversation messages stored" in html
    assert "memory references retrieved" in html
    assert "Goals and constraints learned here become available to future Analyst runs" in html
    assert "Turn market evidence and owner memory into the highest-value, measurable, executable growth moves" in html
    assert "owner-memory rows linked to this run" in html
    assert "Market + owner memory → growth moves" in html


def test_ask_lambda_can_embed_questions_and_return_durable_history():
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")
    ask_block = template.split("AskFunction:", 1)[1].split("DecisionFunction:", 1)[0]

    assert "ConversationMemoryEmbedding" in ask_block
    assert "bedrock:InvokeModel" in ask_block
    assert "AskHistory:" in ask_block
    assert "Path: /ask" in ask_block
    assert "Method: GET" in ask_block
    assert "Method: POST" in ask_block


def test_passed_growth_drawer_exposes_a_durable_undo_pass_action():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "Changed your mind?" in html
    assert "Undo Pass changes this recommendation to Do it and starts Maker." in html
    assert 'data-undo-pass data-post-id="${post.id}"' in html
    assert 'action: "undo_pass"' in html
    assert "async function undoPass(postId, button)" in html
    assert "function applyUndoPassReceipt(post, payload)" in html
    assert "previous_decided_at" in html
    assert "Pass undone. This is now Do it." in html


def test_maker_uses_a_durable_fifo_and_standard_workflow_control_plane():
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")
    decision = template.split("DecisionFunction:", 1)[1].split(
        "WorkflowFunction:", 1
    )[0]
    ask = template.split("AskFunction:", 1)[1].split("DecisionFunction:", 1)[0]
    maker = template.split("MakerFunction:", 1)[1].split("MakerEmailFunction:", 1)[0]
    starter = template.split("TaskStarterFunction:", 1)[1].split("MakerFunction:", 1)[0]
    reconciler = template.split("MakerReconcilerFunction:", 1)[1].split("AskFunction:", 1)[0]

    assert "MakerTaskQueue:" in template
    assert "FifoQueue: true" in template
    assert "MakerTaskDeadLetterQueue:" in template
    assert "MakerWorkflow:" in template
    assert "Type: STANDARD" in template
    assert 'Command: ["brasstacks.handlers.task_starter.handler"]' in starter
    assert 'Command: ["brasstacks.handlers.maker.handler"]' in maker
    assert 'Command: ["brasstacks.handlers.task_reconciler.handler"]' in reconciler
    assert "FunctionResponseTypes:" in starter
    assert "ReportBatchItemFailures" in starter
    assert "states:StartExecution" in starter
    assert "BRASSTACKS_MAKER_QUEUE_URL: !Ref MakerTaskQueue" in decision
    assert "BRASSTACKS_MAKER_QUEUE_URL: !Ref MakerTaskQueue" in ask
    assert "sqs:SendMessage" in decision
    assert "sqs:SendMessage" in ask
    assert "rate(5 minutes)" in template
    assert "MakerSweepState" in reconciler
    assert "ReservedConcurrentExecutions" not in maker


def test_maker_task_control_plane_is_explained_to_operators():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "Live task ledger · exact work per owner" in html
    assert "Do it and Undo Pass create one idempotent CockroachDB task." in html
    assert "SQS FIFO buffers bursts" in html
    assert "Step Functions Standard orchestrates the task" in html
    assert "Maker must atomically claim it before any model call" in html


def test_maker_email_deep_link_opens_the_exact_task_and_full_draft():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'const requestedTaskId = String(appQuery.get("task") || "").trim()' in html
    assert "function maybeOpenRequestedTask()" in html
    assert "function makerReviewPanel(post)" in html
    assert 'data-maker-task-review="${escapeHtml(task.id || "")}"' in html
    assert "data-maker-draft-body" in html
    assert "Copy full draft" in html
    assert "Use the prepared content manually, or ask Maker in chat to revise it before you act." in html
    assert "maybeOpenRequestedTask();" in html


def test_static_artifact_model_preserves_task_identity_and_full_body():
    row = {
        "artifacts": [{
            "id": "50000000-0000-0000-0000-000000000005",
            "kind": "review_reply",
            "title": "Owner draft",
            "preview": "preview",
            "body": "complete body",
            "task_id": "60000000-0000-0000-0000-000000000006",
            "idempotency_key": "task:6:artifact:review_reply:v1",
            "s3_bucket": "bucket",
            "s3_key": "draft.md",
            "created_at": "2026-08-02T20:00:00+00:00",
        }]
    }

    [artifact] = build_web.artifacts(row)

    assert artifact["databaseId"].endswith("000000000005")
    assert artifact["taskId"].endswith("000000000006")
    assert artifact["body"] == "complete body"
    assert artifact["idempotencyKey"].startswith("task:")


def test_build_infers_the_profile_endpoint_from_the_shared_api(monkeypatch, data):
    monkeypatch.setenv(
        "DECISION_API_ENDPOINT",
        "https://example.execute-api.us-east-1.amazonaws.com/v1",
    )
    monkeypatch.delenv("PROFILE_API_ENDPOINT", raising=False)

    model = build_web.build_model(data)

    assert model["api"]["profileEndpoint"] == (
        "https://example.execute-api.us-east-1.amazonaws.com/v1/profile"
    )


def test_app_has_an_accessible_owner_profile_menu_and_editor():
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    assert 'id="profileMenuButton"' in html
    assert 'aria-controls="profilePanel"' in html
    assert 'id="profilePanel"' in html
    assert 'profileField("profileEmail"' in html
    assert 'id="profileSave"' in html
    assert 'method: "PUT"' in html
    assert 'profileApiConfigured' in html
    assert 'owner.email' in html
    assert 'body.autopilot-mode .brand {' in html
    assert 'body.autopilot-mode .brand-copy { display: none; }' in html
    assert 'All active businesses and the email recorded for each owner account.' in html


def test_profile_api_is_deployed_on_the_authenticated_onboarding_lambda():
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(
        encoding="utf-8"
    )

    onboarding = template.split("OnboardingFunction:", 1)[1].split(
        "RunFunction:", 1
    )[0]
    assert "Path: /profile" in onboarding
    assert "Method: GET" in onboarding
    assert "Method: PUT" in onboarding
    assert "ProfileEndpoint:" in template
    assert 'AllowMethods: ["GET", "POST", "PUT", "OPTIONS"]' in template


def test_profile_schema_backfills_only_legacy_business_accounts():
    schema = (build_web.REPO / "db" / "schema.sql").read_text(encoding="utf-8")

    assert "owner_account ADD COLUMN IF NOT EXISTS email" in schema
    assert "peter.flp.2006@gmail.com" in schema
    assert "AND (email IS NULL OR trim(email) = '')" in schema
    assert "profile_managed" in schema



# ------------------------------------------------- the empty decision queue
#
# An empty queue is not an empty business. Every assertion below was written
# against a real screenshot: Yellow Cow Korean BBQ, three accepted moves,
# $100/day predicted, and a board that said "The agents are working…" over a
# night that had died a day and a half earlier.


def test_the_board_will_not_claim_work_from_an_abandoned_run():
    """`status === "running"` is not proof that anything is running.

    A Lambda killed by a timeout cannot close its own agent_run row, so the
    board has to bound how long it believes one. Unbounded, a single dead run
    made the product claim work was in progress indefinitely — the same class
    of untruth as showing money the Meter has not verified.
    """
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    assert "NIGHT_STALE_AFTER_MS" in html
    assert "const openRuns = myRuns.filter(run => String(run.status) === \"running\");" in html
    assert "Date.now() - started) < NIGHT_STALE_AFTER_MS" in html
    # And it must say so rather than silently showing nothing.
    assert "Last night didn't finish" in html


def test_the_stale_bound_matches_the_one_the_run_endpoint_enforces():
    """Two different bounds would let the board offer a button the API refuses."""
    from brasstacks.handlers.run import STALE_RUN_AFTER

    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")
    minutes = int(STALE_RUN_AFTER.total_seconds() // 60)

    assert f"const NIGHT_STALE_AFTER_MS = {minutes} * 60 * 1000;" in html


def test_a_decided_queue_shows_the_moves_in_flight_not_a_full_stop():
    """"All caught up" told an owner with three live moves that there was
    nothing to see, and offered them Restart demo as the way forward."""
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    # The rendered heading, not the word: the comment history above the render
    # explains why that heading was wrong and is worth keeping.
    assert "<h2>All caught up</h2>" not in html
    assert "renderInFlightBoard" in html
    assert "moves are live" in html
    assert "Look for more tonight" in html


def test_the_in_flight_board_keeps_predicted_apart_from_verified():
    """The honesty rule the headline figure has always had, in the one place
    an owner is most likely to read a forecast as a fact: right after saying
    yes to everything."""
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    board = html.split("function renderInFlightBoard", 1)[1].split(
        "function renderAutopilot", 1)[0]
    assert "/day predicted" in board
    assert "/day verified so far" in board
    # The forecast is summed from finds; the verified figure comes from the
    # ledger-backed summary and is never added to it.
    assert "predictedDaily" in board
    assert "summary?.dailyTxt" in board
    assert "predicted + " not in board


def test_restart_demo_is_not_offered_to_a_live_owner():
    """It wipes the owner's decisions out of localStorage. For a demo tenant
    that is a reset; for a real one it destroys their record."""
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    board = html.split("function renderInFlightBoard", 1)[1].split(
        "function renderAutopilot", 1)[0]
    assert "sampleWorkspaceMode" in board
    restart = board.split("data-restart-demo", 1)[0]
    assert restart.rstrip().endswith("? `<button class=\"empty-secondary\" type=\"button\"")


def test_a_refused_night_does_not_animate_as_a_started_one():
    """The endpoint answers 200 with `cooldown` when it declines to spend. The
    button used to read that as success and animate over a night nobody began."""
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    assert 'payload.status === "cooldown"' in html
    assert "Asked already today" in html
    assert "nextNightAt" in html


def test_a_calendar_date_is_not_shifted_into_the_previous_day():
    """`verify_after` is a DATE. JavaScript parses a bare YYYY-MM-DD as UTC
    midnight, so an owner in California was shown "verifies Aug 22" for a find
    the Meter will judge on the 23rd. A date the product commits to has to
    render as the date in the row."""
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    shortdate = html.split("function shortDate(iso) {", 1)[1].split(
        "\n    }", 1)[0]
    assert "^\\d{4}-\\d{2}-\\d{2}$" in shortdate
    assert "T00:00:00" in shortdate


def test_measurement_window_uses_calendar_days_not_timestamp_hours():
    """A DATE minus a late-afternoon TIMESTAMPTZ can appear one day shorter.
    The proof strip and the Analyst brief must describe the same window."""
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")
    block = html.split("function calendarDayNumber", 1)[1].split(
        "function estimatedReadMinutes", 1
    )[0]

    assert "Date.UTC" in block
    assert "verifyDay - createdDay" in block
    assert 'timeZone: "UTC"' in block
    assert "Date.parse(find?.createdAt" not in block


# ------------------------------- the Growth board's headline is a forecast
#
# The three tests below are one rule seen from three sides. `approvedAmount` is
# summed from `predicted_daily_cents` — the Analyst's guess, never a measurement
# — and it goes up the moment the owner presses Do it. It was captioned "Added
# by the moves you approved", set in the same green the product uses for
# verified money, badged "approved impact", and itemised under a heading that
# called each one a win. `ledger_entry` had no rows at all.
#
# That is the mock's third original sin — a projection that grows when the owner
# answers — wearing the vocabulary of the first two.


def test_the_growth_headline_names_the_projection_without_explainer_copy():
    """The Growth view is a glanceable record. It keeps one truthful status
    label and moves the longer explanation into the slide's accessible name."""
    slide = APP.split('class="growth-slide growth-slide-summary"', 1)[1].split(
        "</article>", 1)[0]

    assert "Added by the moves you approved" not in APP_MARKUP
    assert 'id="approvedAmount"' in slide
    assert "Projected · not earned" in slide
    assert "Projected · not measured" not in slide
    assert "Predicted monthly value of the moves you approved" not in slide
    assert "Not earned until verified by the Ledger" in slide


def test_the_forecast_total_is_not_dressed_as_earned_money():
    """Two ways the figure claimed to be earnings without saying so: a leading
    plus sign, and the verified green. It now renders in the chart's projected
    indigo — the same mark language as the one dashed month — and without the
    `+` that `formatMoney` puts in front of money that arrived."""
    growth = APP.split("function renderGrowth() {", 1)[1].split(
        "\n    }", 1)[0]

    assert "projectedMoney(" in growth
    assert "formatMoney(" not in growth
    assert '"+$0"' not in growth
    # The forecast is never added to, or drawn from, the ledger-backed summary.
    assert "dailyTxt" not in growth

    rule = APP.split(".growth-total.forecast strong,", 1)[1].split("}", 1)[0]
    assert "#c9893c" in rule
    assert "var(--approve)" not in rule


def test_the_approved_list_does_not_call_a_forecast_a_win():
    """"Your wins" over rows reading "+$690/mo" is the same untrue claim,
    itemised. The panel keeps the figures — an owner wants to know what they
    committed to — under a heading that says what they are."""
    panel = APP.split('class="list-panel approved-panel"', 1)[1].split(
        "</article>", 1)[0]

    assert "Your wins" not in APP_MARKUP
    assert "<h2>Approved moves</h2>" in panel
    # The row and its accessible label still say projected; the panel subtitle
    # was removed because the amount chip already carries that visual language.
    assert "Projected · not yet measured" not in panel

    row = APP.split("function renderDecisionRow(post, status) {", 1)[1].split(
        "\n    }", 1)[0]
    assert "projectedMoney(post.amount)" in row
    assert "formatMoney(" not in row
    assert "projected, not earned" in row            # the screen-reader label
    assert "${post.recommendation}" not in row       # keep the list minimal


def test_the_growth_process_preview_uses_only_the_three_action_labels():
    source = APP.split("function renderGrowthProjectionPlaceholder", 1)[1].split(
        "function renderRevenueChart", 1
    )[0]

    for label in ("1 · Approve", "2 · Launch", "3 · Verify"):
        assert label in source
    for detail in ("Choose a move", "Maker prepares it", "Meter records impact"):
        assert detail not in source
    assert "note.hidden = true" in source


def test_the_forecast_colour_actually_wins_the_cascade():
    """A rule that loses the cascade is a comment, not a safeguard.

    The forecast block was written last in the head on the reasoning that later
    wins. It does not: four earlier blocks set the verified green with
    `!important` on these exact elements, and `!important` beats source order
    regardless of position. The board kept rendering projected money in the
    colour this product reserves for money the Meter has verified — while the
    test asserting the fix passed, because it only checked that the declaration
    existed in the file.
    """
    import re

    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")
    before, _, rest = html.partition('<style id="forecast-not-earned-v31">')
    block, _, _ = rest.partition("</style>")

    # The competing declarations are real, so the override has to outrank them.
    assert "color: var(--theme-demand) !important" in before
    assert "color: #77827f !important" in before

    colours = re.findall(r"\bcolor:[^;}]+", block)
    assert colours, "the forecast block sets no colour at all"
    for declaration in colours:
        assert "!important" in declaration, declaration

def test_maker_review_workspace_is_concise_traceable_and_revision_ready():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "Maker review workspace" in html
    assert "What Maker prepared" in html
    assert "Your next step" in html
    assert "Open complete working draft" in html
    assert "View the exact message sent" in html
    assert "Email delivery timeline" in html
    assert "Make it shorter" in html
    assert "Warmer tone" in html
    assert "Clearer next steps" in html
    assert 'action: "revise_draft"' in html
    assert "async function reviseMakerDraft" in html


def test_memory_engine_task_ledger_renders_the_full_email_receipt():
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")

    ledger = html.split("function memoryMakerTasksHtml", 1)[1].split(
        "function deriveMemoryStages", 1
    )[0]
    receipt = html.split("function makerEmailReceiptHtml", 1)[1].split(
        "function makerReviewPanel", 1
    )[0]

    assert "latestMakerEmailTool(task)" in ledger
    assert "makerEmailReceiptHtml(email, Boolean(task.supersededAt))" in ledger
    assert "input.plain_body" in receipt
    assert "output.sender || input.sender" in receipt
    assert "output.recipient || input.recipient" in receipt
    assert "output.subject || input.subject" in receipt
    assert 'emailEventFor(email, "delivered")' in receipt
    assert 'emailEventFor(email, "opened")' in receipt
    assert "View the exact message sent" in receipt


def test_ses_delivery_tracking_is_deployed_with_a_configuration_set():
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(
        encoding="utf-8"
    )

    assert "MakerEmailConfigurationSet:" in template
    assert "MakerEmailEventDestination:" in template
    assert "ConfigurationSetName: !Ref MakerEmailConfigurationSet" in template
    assert "EventBridgeDestination:" in template
    assert "MakerEmailEventFunction:" in template
    assert 'Command: ["brasstacks.handlers.ses_event.handler"]' in template
    assert "Email Delivered" in template
    assert "Email Opened" in template
    assert "Email Clicked" in template
    assert "MAKER_EMAIL_CONFIGURATION_SET: !Ref MakerEmailConfigurationSet" in template


def test_maker_email_renderer_does_not_dump_the_complete_working_artifact():
    source = (build_web.REPO / "backend" / "src" / "brasstacks" / "tools" /
              "email.py").read_text(encoding="utf-8")

    assert "Your Maker draft is ready" in source
    assert "YOUR NEXT STEP" in source
    assert "Nothing has been published or sent to customers" in source
    assert '"plain_body": rendered["plain"]' in source
    assert '"html_body": rendered["html"]' in source
    assert "payload.get(\"body\")" not in source




# ------------------------------------------- the night that held everything back
#
# A night that ran and withheld every candidate is a third empty state, distinct
# from "no night has run yet" and from "you have decided on everything". An owner
# who opens the board to nothing, with no reason, concludes the product is
# broken. An owner who reads "41 signals, three candidates, none cleared the bar"
# learns where the bar is.
#
# The trap these guard: an earlier design of the quality gates was measured
# against the real corpus and withheld 9 of 9 finds. So this screen must never
# read as an achievement, and it must never soften into "all caught up".


def test_the_board_explains_a_night_that_held_everything_back():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "function heldBackNotice(" in html
    assert "workspace.heldBack" in html
    # Named as what it is. Not "all caught up", not "nothing new".
    assert "held everything back" in html


def test_the_notice_only_speaks_for_the_most_recent_night():
    """A quiet night in July must not explain every empty morning after it.

    `isLatestNight` is computed in workflow_snapshot against the newest Analyst
    run in CockroachDB. The page may not re-derive or ignore it — believing a
    stale withheld night is the same class of untruth as the board claiming work
    is in progress over a run that died a day and a half ago.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    # Scoped to the guard inside heldBackNotice, not to the file. The string
    # occurs twice — once in the guard and once in the heldNow computation — so
    # a whole-file assertion stayed green when the guard clause was deleted, and
    # a July night would have explained every morning after it. A mutation run
    # proved it: removing "!held.isLatestNight ||" left the suite passing.
    notice = html.split("function heldBackNotice(", 1)[1].split("\n    }", 1)[0]
    assert "held.isLatestNight" in notice
    assert "!held.isLatestNight ||" in notice


def test_the_notice_quotes_the_reason_the_pipeline_stored():
    """The reason is `find.withheld_reason`, shown verbatim.

    Paraphrasing it in the page would put words in the agents' mouths about the
    owner's own business, and the operator view and the owner view would stop
    showing the same string.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "escapeHtml(row.reason" in html
    assert "escapeHtml(row.title" in html


def test_the_notice_does_not_invent_a_signal_count():
    """No trace on the run means we do not know how many rows it read.

    "We checked 0 signals" is a worse lie than saying nothing, so the clause is
    dropped rather than defaulted.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "held.signalsConsidered !== null" in html
    assert "held.signalsConsidered !== undefined" in html


def test_the_notice_puts_no_money_on_a_withheld_find():
    """A withheld find carries a predicted figure. It must not be shown.

    Quoting "$23/day we passed on" would be the product advertising money it
    explicitly declined to stand behind — and the honesty rules already say
    nothing the owner does in the UI may raise the record.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    # The function body, cut at the next JSDoc block rather than at the next
    # `function`, so the neighbouring comment is not read as this one's code.
    body = html.split("function heldBackNotice(", 1)[1].split("\n\n    /**", 1)[0]

    assert "predictedDaily" not in body
    assert "dailyMoney" not in body
    assert "/day" not in body


def test_the_timeline_does_not_say_the_analyst_proposed_a_withheld_find(data):
    """`created_at` on a withheld row is when it was written, not when it was
    put to the owner. It never was.

    The static build folds the operational history out of stamps the cluster
    stored, and "proposed" is the word it uses for `created_at`. A withheld find
    arriving in an export would be logged as a proposal that never happened —
    the same shape of untruth as a modelled figure labelled Actual.
    """
    from brasstacks.repository import WITHHELD_STATUS

    data["finds"] = [find(status=WITHHELD_STATUS, verdict=None,
                          measured_at=None, actual_daily_cents=None,
                          title="Delivery menu is broken")]

    events = build_web.build_model(data)["timeline"]

    assert [event for event in events if event["kind"] == "proposed"] == []


def test_the_live_refresh_rebuilds_the_deck_from_an_allowlist():
    """The second gate, and the one a new status could slip past.

    `applyWorkflowWorkspaces` rebuilds `posts` straight from the live workflow
    rows rather than from the snapshot's `proposed` list. If that filter named
    the statuses to *exclude*, a withheld find would become a decision card the
    moment the board refreshed. It names the ones to include, for the same
    reason OWNER_SEEN_STATUSES is an allowlist.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert ('["proposed", "accepted", "live", "rejected", "later"].includes('
            in html)
    assert '"withheld"' not in html.split("const currentFinds", 1)[1][:400]


def test_a_withheld_find_never_becomes_a_decision_card():
    """`postsFromMemory` filters on the snapshot's `proposed` list.

    That list is built in workflow_snapshot from statuses only, and `withheld`
    is in none of the owner buckets. Pinned here because the deck is the one
    surface where a card means "decide on this".
    """
    from brasstacks.repository import WITHHELD_STATUS
    from brasstacks.workflow_snapshot import build_workspace

    workspace = build_workspace({
        "business": {"id": "b", "name": "Yellow Cow Korean BBQ"},
        "summary": {}, "corpus": {}, "runs": [], "kinds": [],
        "finds": [{
            "id": "11111111-2222-3333-4444-555555555555",
            "title": "Delivery menu is broken", "rationale": "r", "move": "m",
            "emoji": "🚚", "predicted_daily_cents": 2300, "confidence": 0.5,
            "verify_after": "2026-08-18", "status": WITHHELD_STATUS,
            "withheld_reason": "closed-shop page read as an outage",
            "created_at": "2026-08-04T06:02:00+00:00", "evidence": [],
        }],
    })

    assert workspace["proposed"] == []
    assert workspace["withheld"] == ["11111111"]

def test_owner_midnight_experience_is_shared_by_for_you_and_growth():
    """The two owner-facing screens must feel like one premium product."""
    html = (build_web.REPO / "site" / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-midnight-experience-v34">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "--owner-shell: #0c0b09" in block
    assert "--owner-gradient: linear-gradient(135deg" in block
    assert "body.autopilot-mode .feed-card" in block
    assert "body.autopilot-mode .post-body" in block
    assert "body.autopilot-mode .post-art" in block
    assert "body.autopilot-mode .decision-button.approve" in block
    assert "body.growth-mode .growth-swipe" in block
    assert "body.growth-mode .list-panel" in block
    assert "@media (max-width: 900px)" in block
    assert "overflow-y: auto !important" in block
    assert "-webkit-line-clamp: unset !important" in block



# --------------------------------------- production owner polish v35


def test_desktop_feed_uses_the_full_card_and_scrolls_long_recommendations():
    """Desktop must never hide action steps behind the persistent footer.

    The decorative artwork column was hidden by the production mobile contract
    but the desktop grid continued reserving space for it.  The final owner
    layer gives that space to the action plan and makes the reading region the
    one scroll owner when agent copy is unusually long.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-production-polish-v35">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "@media (min-width: 901px)" in block
    assert "overflow-y: auto !important" in block
    assert "scrollbar-gutter: stable !important" in block
    assert '"title action"' in block
    assert '"signal action"' in block
    assert "body.autopilot-mode .post-art { display: none !important; }" in block
    assert "grid-area: action !important" in block
    assert "min-height: 104px !important" in block


def test_growth_process_and_empty_states_share_the_midnight_surface():
    """Growth must not embed a second card inside the dark owner shell.

    The process preview used to carry its own filled, bordered box inside the
    already-dark slide — a box in a box the owner flagged as "too many
    backgrounds". It is flat now: no card gradient, so it inherits the one
    surface. It still must not become a bright white card, which transparency
    over the dark slide guarantees.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-production-polish-v35">', 1)[1].split(
        "</style>", 1
    )[0]

    assert ".growth-chart.is-process-preview" in block
    assert "height: 100% !important" in block
    # No inner card fill any more — the preview shares the slide's background.
    assert "linear-gradient(145deg, rgba(20,25,49,.90), rgba(10,14,30,.80))" not in block
    assert "body.growth-mode .growth-projection" in block
    assert "body.growth-mode .list-empty" in block
    assert "rgba(255,255,255,.018) !important" in block
    assert "color: #e8ebf6 !important" in block
    assert 'panel.classList.add("is-process-preview")' in html
    assert 'panel.classList.remove("is-process-preview")' in html


# --------------------------------------- unified owner surfaces v37


def test_owner_profile_and_chat_share_the_midnight_product_surface():
    """Owner overlays must not switch back to the old white application skin."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-unified-surfaces-v37">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "body.autopilot-mode #detailDrawer" in block
    assert "body.growth-mode #detailDrawer" in block
    assert "background: linear-gradient(180deg, #171410 0%, #100e0a 100%) !important" in block
    assert "body.autopilot-mode .profile-panel" in block
    assert "body.growth-mode .profile-panel" in block
    assert "body.autopilot-mode .profile-field input" in block
    assert "background: var(--owner-surface-input) !important" in block
    assert "body.autopilot-mode #detailDrawer .chat-message.agent" in block
    assert "background: #1b1811 !important" in block
    assert "body.autopilot-mode #detailDrawer .send-button" in block


def test_owner_theme_is_scoped_away_from_memory_engine():
    """The operator console keeps its own dense visual language."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-unified-surfaces-v37">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "body.autopilot-mode" in block and "body.growth-mode" in block
    assert "body.console-mode" not in block
    assert ".memory-engine" not in block


def test_growth_forecast_value_is_a_dark_high_contrast_chip():
    """Projected money must be legible without using a paper-white pill."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-unified-surfaces-v37">', 1)[1].split(
        "</style>", 1
    )[0]
    chip = block.split("body.growth-mode .decision-row-value,", 1)[1].split("}", 1)[0]

    assert "background: linear-gradient(135deg, rgba(126,59,225,.22), rgba(190,55,169,.15)) !important" in chip
    assert "color: #ead7ff !important" in chip
    assert "font-size: 12px !important" in chip
    assert "font-weight: 820 !important" in chip
    assert "font-variant-numeric: tabular-nums !important" in chip
    assert "background: #fff" not in chip
    assert "background: #ffffff" not in chip

# --------------------------------------- owner production system v38


def test_growth_lists_remove_redundant_explanatory_copy():
    """Growth remains traceable without repeating instructions in every panel."""
    html = APP
    approved = html.split('class="list-panel approved-panel"', 1)[1].split(
        "</article>", 1
    )[0]
    passed = html.split('class="list-panel rejected-panel"', 1)[1].split(
        "</article>", 1
    )[0]

    assert "Projected · not yet measured" not in approved
    assert "Ideas you skipped" not in passed
    assert "Nothing approved yet" not in html
    assert "Nothing passed" not in html
    assert "Passed ideas stay here for reference." not in html
    assert "function renderGrowthDecisionList" in html
    assert 'panel.classList.toggle("is-empty", items.length === 0)' in html
    # Forecast meaning remains in the row's accessible label.
    assert "projected, not earned" in html


def test_empty_growth_panels_collapse_to_their_title_and_count():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-production-system-v38">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "body.growth-mode .list-panel.is-empty" in block
    assert "body.growth-mode .list-panel.is-empty .decision-list" in block
    assert "display: none !important" in block
    assert "align-items: start !important" in block


def test_owner_chat_controls_use_dark_high_contrast_statuses_and_disclosures():
    """Maker review may not reintroduce paper-white cards or pastel status pills."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-production-system-v38">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "--owner-surface-input: #12100c" in block
    assert "body.autopilot-mode #detailDrawer .maker-full-draft" in block
    assert "background: #12100d !important" in block
    assert "max-height: min(48vh, 520px) !important" in block
    assert "background: #100e0a !important" in block
    assert "body.autopilot-mode #detailDrawer .maker-review-state.superseded" in block
    assert "background: rgba(148,163,184,.10) !important" in block
    assert "body.autopilot-mode #detailDrawer .maker-email-status.clicked" in block
    assert "background: rgba(45,181,142,.11) !important" in block
    assert "body.autopilot-mode #detailDrawer .maker-email-timeline .is-complete i" in block
    assert "body.autopilot-mode #detailDrawer .maker-review-head .maker-review-state" in block
    assert "font: 800 9px/1 Inter" in block


def test_reconsider_form_and_profile_selects_use_the_dark_control_contract():
    """Selects stay readable both closed and in their native option menu."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-production-system-v38">', 1)[1].split(
        "</style>", 1
    )[0]

    assert 'class="reconsider-field"' in html
    assert "<span>Reason</span>" in html
    assert "<span>Note <small>optional</small></span>" in html
    assert "grid-template-columns: minmax(0, 1fr) !important" in block
    assert "color-scheme: dark !important" in block
    assert "appearance: none !important" in block
    assert "background-color: var(--owner-surface-input) !important" in block
    assert "body.autopilot-mode #detailDrawer .reconsider-fields select option" in block
    assert "body.autopilot-mode .profile-field select option" in block
    assert "background: #0b1121 !important" in block
    assert "color: #f4f6ff !important" in block


def test_owner_brand_and_primary_actions_share_one_accessible_gradient():
    """The owner logo and actions belong to one product family."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-production-system-v38">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "--owner-brand-gradient: linear-gradient(135deg, #6d28d9 0%, #8b2bd8 52%, #a21caf 100%)" in block
    assert "body.autopilot-mode .brand-mark" in block
    assert "body.autopilot-mode .profile-panel-mark" in block
    assert "background: var(--owner-brand-gradient) !important" in block
    assert "body.autopilot-mode #detailDrawer .send-button" in block
    assert "body.autopilot-mode #detailDrawer .reconsider-button" in block
    assert "body.autopilot-mode .profile-save" in block


def test_v38_owner_layer_does_not_restyle_memory_engine():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="owner-production-system-v38">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "body.autopilot-mode" in block
    assert "body.growth-mode" in block
    assert "body.console-mode" not in block
    assert ".memory-engine" not in block


# --------------------------------------- structured Analyst feed v40


def test_for_you_feed_renders_a_structured_analyst_brief():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    card = html.split("function renderFeedCard", 1)[1].split(
        "function feedCardAt", 1
    )[0]

    assert "renderFeedDetailPanel(post)" in card
    # The "For you" chip was removed to declutter the card — the tab already
    # says which surface this is. The structured brief below is what matters.
    assert 'class="feed-tag-list"' in card
    assert 'class="feed-proof-strip"' in card
    assert 'class="feed-detail-panel"' in html
    # The Impact/Effort/Type/Owner-approval grid was removed from the card at
    # the owner's request (too dense; the money is in the footer). The panel now
    # leads with the next step and keeps the modelled detail and at-a-glance grid
    # the honesty rules require, one layer down under "More detail".
    assert "At a glance" in html
    assert "Execution plan" in html
    assert "post.feedBrief" in html
    assert "pricePoint" in html
    assert "successSignal" in html


def test_for_you_feed_uses_smaller_aligned_title_and_responsive_geometry():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    block = html.split('<style id="analyst-feed-brief-v40">', 1)[1].split(
        "</style>", 1
    )[0]

    assert "grid-template-columns: minmax(0, 1.04fr) minmax(390px, .96fr)" in block
    assert "font-size: clamp(31px, 2.45vw, 40px) !important" in block
    assert "max-width: 25ch !important" in block
    assert ".feed-detail-row" in block
    assert ".feed-glance-grid" in block
    assert ".feed-plan" in block
    assert "@media (max-width: 1120px)" in block
    assert "@media (max-width: 900px)" in block
    assert "grid-template-columns: minmax(0, 1fr) !important" in block
    # The legacy mobile grid declared one flexible row. Without explicit
    # max-content auto rows the copy and detail panel occupy the same track and
    # visually overlap even though both are present in the DOM.
    assert "grid-template-rows: none !important" in block
    assert "grid-auto-rows: max-content !important" in block


def test_find_to_post_carries_the_analyst_feed_brief_without_trusting_it_blindly():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    mapper = html.split("function findToPost", 1)[1].split(
        "function postsFromMemory", 1
    )[0]

    assert "normaliseFeedBrief(find)" in mapper
    assert "feedBrief:" in mapper
    assert "measurementWindowLabel" in mapper
    assert "estimatedReadMinutes" in mapper


def test_feed_brief_frontend_preserves_honest_empty_prices_and_precise_tags():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    normaliser = html.split("function normaliseFeedBrief", 1)[1].split(
        "function feedDetailIcon", 1
    )[0]

    assert "hasStructuredBrief" in normaliser
    assert '"Not specified"' in normaliser
    assert "Only legacy rows with no brief at all" in normaliser
    assert "if (tags.length < 2)" in normaliser
    assert "Preserve a precise two- or three-tag Analyst brief" in normaliser
    assert "Analyst confidence" in html
    assert 'String(value ?? "")' in html


def test_feed_evidence_rows_open_an_accessible_inline_sheet():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'data-open-evidence="${escapeHtml(post.id)}"' in html
    assert 'aria-haspopup="dialog"' in html
    assert 'role="dialog" aria-modal="true"' in html
    assert 'id="evidenceSheetTitle"' in html
    assert 'function openEvidenceSheet(postId, opener)' in html
    assert 'function closeEvidenceSheet(restoreFocus = true)' in html
    assert 'if (event.key === "Escape" && document.getElementById("evidenceSheet"))' in html


def test_feed_evidence_sheet_renders_source_metadata_and_mobile_bottom_sheet():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'item.sourceName || item.source || item.kind || "Stored memory"' in html
    assert 'title="Vector similarity"' in html
    assert '.evidence-sheet-panel { width:100%; max-height:88dvh;' in html
    assert 'border-radius:22px 22px 0 0' in html


def test_the_detail_panel_shows_a_cost_only_when_one_was_estimated():
    """An unpriced move renders no cost rows at all — never a row of zeroes.

    Every find written before the Coster existed is unpriced, and so is every
    find from a night whose costing call failed. "$0 setup" on those cards would
    be the most persuasive thing the product could say about a move nobody had
    priced, which is why the renderer branches on `hasEstimate` rather than on
    the figures themselves.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    panel = html.split("function renderCostsAndGoals", 1)[1].split(
        "function renderFeedDetailPanel", 1
    )[0]

    assert "cost.hasEstimate" in panel
    assert "costRows = cost && cost.hasEstimate" in panel
    # The empty branch is a literal empty list, not a list of zeroed rows.
    assert "] : [];" in panel


def test_the_card_never_does_money_arithmetic_for_a_cost():
    """CLAUDE.md: money is formatted once, in Python. The page prints strings.

    The failure this prevents is the page dividing cents by 100 in JavaScript
    and disagreeing with the ledger three decimal places down.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    panel = html.split("function renderCostsAndGoals", 1)[1].split(
        "function renderFeedDetailPanel", 1
    )[0]

    assert "cost.setupTxt" in panel
    assert "cost.dailyTxt" in panel
    assert "cost.paybackTxt" in panel
    for arithmetic in ("setupCents /", "dailyCents /", "/ 100"):
        assert arithmetic not in panel, arithmetic


def test_a_cost_is_labelled_modelled_and_never_called_actual():
    """The same rule the ledger keeps: an unmeasured figure says it is modelled.

    Nothing measures a cost yet — the Meter judges revenue against observed
    outcomes and has no path to a spend receipt — so the card has to say so
    rather than letting a confident number imply otherwise.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    panel = html.split("function renderCostsAndGoals", 1)[1].split(
        "function renderFeedDetailPanel", 1
    )[0]

    assert "Costs are modelled, not measured." in panel
    assert "Actual cost" not in panel


def test_the_card_says_when_a_move_is_measured_before_it_breaks_even():
    """verify_after_days defaults to 14; a $900 setup at +$23/day takes 40.

    The Meter would stamp that VERIFIED 26 days before the owner was square, and
    the owner is entitled to know the first verdict measures the lift rather
    than the payback.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    panel = html.split("function renderCostsAndGoals", 1)[1].split(
        "function renderFeedDetailPanel", 1
    )[0]

    assert "cost.paysBackWithinWindow === false" in panel
    assert "before it breaks even" in panel


def test_find_to_post_carries_the_cost_estimate_untrusted():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    mapper = html.split("function findToPost", 1)[1].split(
        "function postsFromMemory", 1
    )[0]

    # `|| null` rather than `|| {}`: an absent estimate must be falsy at the
    # branch above, and an empty object is not.
    assert "costEstimate: find.costEstimate || null" in mapper


def test_maker_google_business_action_is_revision_bound_and_owner_confirmed():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'manifest?.action_type !== "google_business.publish_post"' in html
    assert 'data-artifact-id="${escapeHtml(String(artifact?.databaseId' in html
    assert 'body: JSON.stringify({ confirm: true, artifact_id: card.dataset.artifactId, revision: Number(card.dataset.revision || 1) })' in html
    assert "I reviewed this exact revision and want to publish it publicly" in html
    assert "Publish this exact post?" in html


def test_google_business_action_has_a_simple_mobile_bottom_sheet():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    mobile = html.split("@media (max-width:700px)", 1)[1].split("</style>", 1)[0]

    assert ".maker-publish-dialog" in mobile
    assert "margin:auto 0 0" in mobile
    assert "border-radius:22px 22px 0 0" in mobile
    assert "grid-template-columns:1fr 1fr" in mobile


def test_google_business_tokens_never_enter_the_maker_browser_contract():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    action = html.split("function makerGoogleBusinessActionHtml", 1)[1].split(
        "function loadGoogleBusinessStatus", 1
    )[0]

    assert "token_ciphertext" not in action
    assert "refresh_token" not in action
    assert "client_secret" not in action
    assert "Connect Google Business" not in action  # rendered only after a safe status response


def test_google_business_receipt_links_allow_https_only():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    helper = html.split("function safeExternalHref", 1)[1].split(
        "function latestGoogleBusinessTool", 1
    )[0]

    assert 'parsed.protocol === "https:"' in helper
    assert 'return ""' in helper
    assert "noopener noreferrer" in html


def test_reconsider_control_is_at_the_top_of_the_maker_workspace_not_chat_bottom():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    maker = html.split("function makerReviewPanel", 1)[1].split(
        "function maybeOpenRequestedTask", 1
    )[0]
    drawer = html.split("function renderDrawer", 1)[1].split(
        "function closeDrawer", 1
    )[0]

    assert "maker-review-decision-control" in maker
    assert "reconsiderPanelHtml(post, { compact: true })" in maker
    assert maker.index("maker-review-decision-control") < maker.index("maker-review-head")
    assert drawer.index("${makerReviewPanel(post)}") < drawer.index('class="drawer-chat-panel"')
    chat = drawer.split('class="drawer-chat-panel"', 1)[1]
    assert "reconsiderPanelHtml(post" not in chat
    assert '{ scroll: false }' in html
    assert "function scrollDrawerSectionIntoView" in html
    assert 'scrollDrawerSectionIntoView(review, { behavior: "smooth", offset: 12 })' in html


def test_maker_questions_use_a_guided_revision_reply_instead_of_a_long_template():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "function makerAnswerPayload" in html
    assert "function renderMakerGuidedComposer" in html
    assert "function activateMakerAnswerMode" in html
    assert 'form.dataset.chatAction = "maker_guided"' in html
    assert "data-use-maker-answer-template" in html
    assert 'requestPayload.action = "revise_draft"' in html
    assert "Required answer ${index + 1} of ${total}" in html
    assert "Answer one at a time" in html
    assert "Add a short answer before continuing." in html
    assert "Answers received. Maker is queuing the next revision" in html
    assert "makerAnswerPayload(makerGuidedReply)" in html
    assert "input.maxLength = 200" in html
    assert '"Waiting for you"' in html
    assert "Maker is paused until you answer the required items." in html
    assert "Complete all ${makerInput.questions.length} Answer lines before sending." not in html
    assert "function parseMakerAnswerMessage" in html
    assert "function renderOwnerChatHtml" in html
    assert "Answers sent to Maker" in html
    assert "→ ${response}" in html


def test_claude_chat_is_structured_and_legacy_long_answers_are_collapsible():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "function renderAssistantChatHtml" in html
    assert "function parseAssistantAnswer" in html
    assert "What I need from you" in html
    assert "Reply with" in html
    assert "Next step" in html
    assert 'message.innerHTML = renderAssistantChatHtml(text)' in html
    assert "Read the full response" in html
    assert "hasExplicitStructure" in html


def test_chat_composer_is_large_guided_and_voice_optional_on_mobile():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    styles = html.split('id="maker-guided-reply-v44"', 1)[1].split("</style>", 1)[0]

    assert '<textarea id="drawerInput"' in html
    assert 'id="drawerComposerContext"' in html
    assert 'id="drawerComposerBack"' in html
    assert 'id="drawerComposerCancel"' in html
    assert 'id="drawerVoiceButton"' in html
    assert 'event.key === "Enter" && !event.shiftKey' in html
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in html
    assert "Voice fills the field but never sends automatically." in html
    assert "do not interact while driving. Park first." in html
    assert ".drawer-composer-row" in styles
    assert "grid-template-columns: 44px minmax(0, 1fr) 48px" in styles
    assert "@media (max-width: 640px)" in styles
    assert "height: 52px" in styles
    assert "min-height: 48px" in styles
    voice = html.split("function startDrawerVoiceInput", 1)[1].split(
        "const drawerComposerInput", 1
    )[0]
    assert "recognition.onresult" in voice
    assert "requestSubmit" not in voice


def test_owner_reply_follows_the_latest_chat_after_submit_and_live_refresh():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    chat = html.split("function requestDrawerChatFollow", 1)[1].split(
        "function escapeHtml", 1
    )[0]

    assert "function scrollDrawerChatToLatest" in chat
    assert "drawerBody.scrollTo({ top: drawerBody.scrollHeight" in chat
    assert "requestDrawerChatFollow(post.id, 45000)" in chat
    assert "if (guided) clearMakerAnswerMode();" in chat
    assert 'appendDrawerMessage("user", submittedText, { behavior: "auto" })' in chat
    assert "refreshWorkflowSnapshot({ force: true, silent: true })" in chat
    assert "if (drawerShouldFollowChat(post.id))" in chat
    assert 'scrollDrawerChatToLatest({ behavior: "auto" })' in chat
    assert 'messages.forEach(message => appendDrawerMessage(' in chat
    assert '{ scroll: false }' in chat
    assert 'drawerBody.addEventListener("wheel", cancelDrawerChatFollow' in html
    assert 'drawerBody.addEventListener("touchstart", cancelDrawerChatFollow' in html


def test_maker_workspace_and_chat_name_the_draft_destination_and_owner_gate():
    """The destination and owner-gate story survives the drawer losing its
    chat. The drawer's conversation moved to the one Chat tab; what stays here
    is the Maker workspace naming where a draft goes and who gates it, and
    the guided-answer composer that still carries the destination strip."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    placement = html.split("function makerPlacementInfo", 1)[1].split(
        "function currentMakerInputState", 1
    )[0]
    drawer = html.split("function renderDrawer", 1)[1].split(
        "function closeDrawer", 1
    )[0]

    assert "activeArtifact?.useContext" in placement
    assert "stored.owner_gate" in placement
    assert "Where this draft goes" in placement
    assert "Who sees it:" in placement
    assert "Before it is used:" in placement
    assert "Draft destination" in placement
    assert "Not published" in html
    assert 'id="drawerComposerDestination"' in html
    assert "function configureDrawerDestination" in html
    # The drawer no longer hosts a chat thread — one conversation for the
    # whole app lives in the Chat tab, and the drawer routes there.
    assert 'id="drawerChatThread"' not in drawer
    # The drawer is the reading document inside the chat canvas now -- the
    # conversation sits beside it, so no trip-to-chat button. It carries the
    # full move: route, costs & goals, exhibits.
    assert "planMapHtml(post)" in drawer
    assert "renderCostsAndGoals(post)" in drawer
    assert "exhibitListHtml(post)" in drawer


def test_growth_decision_rows_escape_the_find_title():
    """A find title is model text over scraped web content, so it is escaped.

    Every other render of `post.title` escapes it — the feed card, the evidence
    button, the details button, the drawer heading. This row was the one that
    did not, and it is written into innerHTML twice: once as the visible label
    and once inside an aria-label attribute, where a bare quote is enough to
    break out of the attribute.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    row = html.split("function renderDecisionRow(post, status)", 1)[1].split(
        "function renderGrowthDecisionList", 1
    )[0]

    assert "${post.title}" not in row
    assert row.count("escapeHtml(post.title)") == 2
    assert "${post.id}" not in row
    assert "${post.featureKey}" not in row


# ------------------------------------------------- reporting what a move earned

def _growth_outcome_source():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    strip = html.split("function outcomeStripHtml(post, status)", 1)[1].split(
        "async function persistOutcome(", 1
    )[0]
    return html, strip


def test_the_growth_tab_offers_a_place_to_report_what_a_move_earned():
    """The only way a verdict can ever be anything but an estimate.

    `NoOutcomeSource` is the honest default and it can never produce a verified
    win, so without a control here the ledger publishes estimates forever and
    the hit rate stays undefined. It lives on the approved list because that is
    where the moves that could have earned anything are.
    """
    html, strip = _growth_outcome_source()

    assert "function outcomeStripHtml(post, status)" in html
    # A disclosure, not a form open under every row. Collapsed it is one line.
    assert "<details class=\"outcome-panel\"" in strip
    assert "<summary>" in strip
    assert 'data-outcome-panel=' in strip
    assert 'data-save-outcome=' in strip
    assert 'data-outcome-amount' in strip
    assert 'data-outcome-basis' in strip
    # Only against moves the owner accepted. A passed-over find has no outcome.
    assert 'status !== "approved"' in strip


def test_a_reported_figure_is_labelled_as_reported_never_as_verified():
    """An owner's own number is not a verdict until the Meter judges it.

    Showing it as "actual" would be the mock's original sin — a figure the
    product had not measured, presented as one it had.
    """
    _, strip = _growth_outcome_source()

    assert "You reported" in strip
    assert "Actual" not in strip
    assert "Verified" not in strip
    # And it says when the number will actually be scored, so "reported" does
    # not read as "waiting on nothing".
    assert "post.verifyAfter" in strip
    # The caption is built from one date, not by patching a separator back into
    # a prose fragment — which is how "score it on scored on Sep 1" reached a
    # real screen.
    assert ".replace(\" · \"" not in strip


def test_a_measured_verdict_is_not_offered_a_correction_box():
    # A published miss cannot be edited away from the browser. The repository
    # refuses it too; the interface should not imply otherwise.
    _, strip = _growth_outcome_source()
    assert 'post.verdict === "verified" || post.verdict === "miss"' in strip


def test_the_page_never_does_arithmetic_on_the_typed_amount():
    """`12.34 * 100` is 1233.9999999999998 in JavaScript.

    The string the owner typed goes to the server, which parses it with
    Decimal. This is the same rule the growth chart keeps: money is formatted
    and computed once, in Python.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    persist = html.split("async function persistOutcome(", 1)[1].split(
        "async function saveOutcome(", 1
    )[0]

    assert "* 100" not in persist
    assert "parseFloat" not in persist
    assert "Number(" not in persist
    assert "amount:" in persist
    assert "basis:" in persist
    assert "/outcome" in persist
    assert "authHeaders(" in persist


def test_reporting_an_outcome_never_moves_the_forecast_or_the_record():
    """Nothing the owner does in the UI may increase the verified record.

    Reporting a figure is the closest this product comes to breaking that rule,
    so it is worth pinning: the save path updates the row it belongs to and
    nothing else. The headline stays a sum of predictions until the Meter says
    otherwise.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    save = html.split("async function saveOutcome(", 1)[1].split(
        "function renderDecisionRow(post, status)", 1
    )[0]

    assert "forecastTotal" not in save
    assert "renderRevenueChart" not in save
    assert "summary" not in save
    # It re-renders the lists it owns, and that is all.
    assert "renderGrowth()" in save


def test_the_reported_figure_survives_a_reload(data):
    """Straight through from CockroachDB, both paths.

    A form that forgets what was entered the moment the page reloads reads as
    broken, and the number is already in the cluster — build_web and the live
    snapshot both carry it, through the same helper, so the first paint and the
    refresh cannot disagree.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    assert "reportedOutcome: find.reportedOutcome" in html

    payload = build_web.build_model(data)
    rendered = payload["finds"][0]
    assert "reportedOutcome" in rendered


def test_a_reported_outcome_renders_the_figure_the_owner_entered():
    from brasstacks.finds import reported_outcome_view

    view = reported_outcome_view(
        {"amount_cents": 21000, "basis": "week", "daily_cents": 3000,
         "note": "Counted from the till.", "reported_at": None},
        money=build_web.money)

    assert view["amountTxt"] == "$210"
    assert view["basisLabel"] == "a week"
    assert view["dailyCents"] == 3000
    assert view["note"] == "Counted from the till."


def test_nothing_reported_is_absence_not_zero():
    # A reported zero is a measured miss. "Nobody has said" is a different fact
    # and the two must not collapse into one.
    from brasstacks.finds import reported_outcome_view

    assert reported_outcome_view(None, money=build_web.money) is None
    assert reported_outcome_view({}, money=build_web.money) is None
    zero = reported_outcome_view(
        {"amount_cents": 0, "basis": "week", "daily_cents": 0},
        money=build_web.money)
    assert zero is not None
    assert zero["amountCents"] == 0


def test_the_result_panel_is_styled_for_the_theme_growth_actually_runs_in():
    """The Growth tab is dark. The first cut of this shipped light surfaces.

    `#fbfaf7` under a midnight card read as a white band glued to the panel,
    spilling past its rounded corner. The dark rules have to live after every
    block that could override them, which is what the version suffix on a style
    id means in this file.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'id="owner-outcome-disclosure-v45"' in html
    dark = html.split('id="owner-outcome-disclosure-v45"', 1)[1].split("</style>", 1)[0]
    # Comments stripped: this block explains the light-theme surface it
    # replaced, and naming a colour is not shipping it.
    rules = re.sub(r"/\*.*?\*/", "", dark, flags=re.S)

    assert "body.growth-mode .outcome-panel" in rules
    assert "var(--owner-ink)" in rules
    assert "var(--owner-muted)" in rules
    # No light-theme surface among the declarations.
    assert "#fbfaf7" not in rules
    assert "background: #ffffff" not in rules

    # And it is the last style block, so nothing later can undo it.
    assert html.rindex('<style id="owner-outcome-disclosure-v45"') > html.rindex(
        '<style id="maker-guided-reply-v44"')


def test_opening_the_result_panel_does_not_rebuild_the_list():
    """A re-render mid-typing throws away what was typed.

    The first cut toggled a module variable and called renderGrowth(), so every
    open and close rebuilt both lists. <details> owns its own open state; the
    set only survives the renders that other things trigger.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    binding = html.split('document.querySelectorAll("[data-outcome-panel]")', 1)[1].split(
        "document.querySelectorAll(\"[data-save-outcome]\")", 1)[0]

    assert "renderGrowth()" not in binding
    assert "openOutcomePanels.add" in binding
    assert "openOutcomePanels.delete" in binding


# --- Guided demo tour -------------------------------------------------------
#
# A judge should be able to see the whole loop without a live backend or a
# password: press one button, screen-record, and watch the tour drive itself
# through For You, the ledger, and the Memory Engine. It is self-contained on
# purpose -- it seeds a local session and reads the committed fixture, so a
# slow or cold Lambda can never freeze the recording mid-take.

def test_the_login_page_does_not_advertise_the_demo():
    """The demo has one front door and it is the landing page.

    This page used to carry its own Play button, which offered "try the demo"
    to an owner who had arrived to sign in to their own workspace. The deep
    link still works -- the app's own replay uses it -- but nothing on the
    sign-in form points at it.
    """
    html = (build_web.SITE / "login.html").read_text(encoding="utf-8")

    assert 'id="playDemo"' not in html
    assert "Try the interactive demo" not in html
    # The ?tour= deep link still seeds a session locally, with no call to the
    # login endpoint, so a replay never depends on the backend being awake.
    assert '"../app/?tour=owner"' in html


def test_the_app_runs_the_interactive_demo_without_a_backend():
    """The app reads the demo flag, guarantees a session so its own guard does
    not bounce the demo to the login page, exposes switchView for the demo to
    drive, and gives the viewer the controls."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    # switchView is inside the module; the demo needs a handle on it.
    assert "window.__btSwitchView = switchView;" in html
    # A session is seeded before the "no session -> login" guard runs, so the
    # demo is never redirected away.
    assert "bt-tour-seed" in html
    # The control bar, and every control the viewer needs to drive it.
    assert "bt-demo-bar" in html
    assert "bt-demo-next" in html
    assert "bt-demo-back" in html
    assert "bt-demo-exit" in html
    # It reaches the money shot and the Memory Engine by name.
    assert 'view("growth")' in html
    assert 'view("admin")' in html
    # The operator half needs the admin session, which is read once at load, so
    # the hand-off is a navigation rather than a view switch.
    assert '"?workspace=admin&tour=console"' in html


def test_the_demo_never_advances_on_its_own():
    """The viewer drives it. This is the whole difference between the demo and
    the self-playing tour it replaced: a judge who stops to read something must
    not have the page move underneath them.

    The narration went with it. A synthesised voice that cannot be paused is
    worse than no voice, and it made the demo unwatchable in a shared room. The
    assertion is negative on purpose -- it is the only way to stop the timer
    quietly coming back."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "SpeechSynthesisUtterance" not in html
    assert "speechSynthesis" not in html
    # Advancing is bound to input, never to a clock.
    assert 'nextBtn.addEventListener("click"' in html
    assert 'e.key === "ArrowRight"' in html


def test_the_demo_leaves_the_app_clickable():
    """A demo whose overlay swallows clicks is a video with extra steps.

    Only the control bar takes pointer events; the spotlight is a ring drawn
    over the page rather than a mask cut out of a dimming layer, because a
    dimming layer has to intercept clicks to look right."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert ".bt-demo-spot{" in html
    assert "pointer-events:none" in html.split(".bt-demo-spot{")[1][:200]


def test_the_guided_demo_ledger_is_disclosed_as_seeded():
    """The README must say the ledger the tour shows is seeded, backdated
    history with illustrative figures -- the mechanism real, the clock compressed."""
    readme = (build_web.REPO / "README.md").read_text(encoding="utf-8")

    assert "seeded, backdated history" in readme
    assert "illustrative" in readme
    # The honest half: what is compressed is the clock, not the mechanism.
    assert "the clock, not the mechanism" in readme


def test_for_you_is_a_scrolling_feed_with_a_stats_rail():
    """The deck showed one swipeable card at a time. Owners scroll a feed now:
    cards grouped by day, newest first, beside a rail whose numbers are counted
    from the same posts the feed renders -- real CockroachDB rows, never a
    typed-in figure. The one-card chrome (arrows, dots) went with the deck."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'class="feed-group-label"' in html
    assert "function feedGroupLabel" in html
    assert "function renderFeedRail" in html
    rail = html.split("function renderFeedRail", 1)[1][:2600]
    # Evidence rows are summed from the posts, topics counted from their briefs.
    assert "evidenceCount" in rail
    assert "feedBrief?.tags" in rail
    # The one-card navigation chrome is gone.
    assert 'id="previousPost"' not in html
    assert 'id="feedDots"' not in html

    # The conversion CSS has the last word over every deck-era height and
    # position rule, so cards flow at natural height in one scrolling column.
    css = html.split('<style id="feed-scroll-v66">', 1)[1].split("</style>", 1)[0]
    assert "flex-direction: column" in css
    assert "position: static !important" in css
    # The page owns the scroll on phones. The deck made each card its own
    # inner scroller and locked touch to its gestures; every one of those
    # traps must lose to the feed.
    assert "touch-action: auto !important" in css
    assert "overscroll-behavior: auto !important" in css


def test_the_card_detail_panel_is_three_uniform_rows():
    """The panel led with a highlighted, numbered first step -- a paragraph tall
    enough to dwarf the rows under it, saying what the Execution plan's first
    bullet already says. It is now three identical quiet rows: plan, costs,
    evidence. And the evidence chevron points down like the two disclosures
    beside it, not sideways -- one set of rows, one direction."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    panel = html.split("function renderFeedDetailPanel", 1)[1].split(
        "function findToPost", 1)[0]
    assert "feed-next-first" not in panel
    assert "feed-next-heading" not in panel
    assert 'class="feed-plan"' in panel
    assert 'class="feed-more"' in panel
    assert 'class="feed-detail-evidence"' in panel

    # The rows carry no chevron at all any more -- the row itself is the
    # affordance, and the sheet opens on tap.
    face = html.split('<style id="facelift-sly-v70">', 1)[1].split("</style>", 1)[0]
    assert "content: none !important" in face

    # And the panel is boxless: the rows are the shapes, the container is not.
    # A bordered panel wrapping three bordered rows was a box in a box.
    flat = html.split('<style id="feed-scroll-v66">', 1)[1].split("</style>", 1)[0]
    assert "background: transparent !important" in flat
    assert "box-shadow: none !important" in flat


def test_the_demo_supplies_board_never_scolds_its_own_session():
    """The guided demo runs on a placeholder session by design; the injected
    sample store is the content. The board's refresh gets a 401 from /orders
    and used to answer with "session expired -- sign out and back in", i.e.
    the demo scolding itself on camera. The note is gated off when a tour is
    running."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    before = html.split('note("Your session has expired', 1)[0][-500:]
    assert '"tour"' in before


def test_the_supplies_board_carries_the_clean_dashboard_pass():
    """One visual system for the Quartermaster's board: icon chips, quiet
    uniform zone rows, one accent -- and it must be the last word over the
    accumulated orders-mode styling, in both themes and at phone width."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    css = html.split('<style id="supplies-clean-v67">', 1)[1].split("</style>", 1)[0]
    assert ".orders-zone-ico" in css
    assert ".orders-seg" in css
    assert "body.day-mode" in css
    assert "@media (max-width: 640px)" in css


def test_the_demo_orders_through_the_preview_quartermaster():
    """In the guided demo, ordering in chat must WORK. The demo session is a
    placeholder, so the live /orders API refuses it ("sign in first" on
    camera). In tour mode the board boots the preview engine -- the local
    mirror of the tested Python -- and the chat routes orders through it:
    same parse, same purchase authority, a receipt on the board."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    # The board chooses the preview engine while a tour is running.
    gate = html.split("ordersLive();", 1)[0][-500:]
    assert "tour" in gate
    # The preview exposes the chat's order hook, answering the live contract.
    assert "window.__btPreviewAsk" in html
    hook = html.split("window.__btPreviewAsk", 1)[1][:1600]
    assert "needs_approval" in hook or "submit(" in hook
    # The chat tries the preview engine before refusing to act.
    branch = html.split("if (!chatLive) {", 1)[1][:1200]
    assert "__btPreviewAsk" in branch


def test_demo_decisions_take_the_local_path():
    """Do it / Pass must work while exploring the demo. On the deployed build
    the buttons waited for a live decision sync the placeholder session can
    never reach -- rendered disabled, "Checking latest decision status" forever
    -- and decide() would have refused or POSTed a 401 anyway. In tour mode the
    buttons enable and the decision takes the demoOnly local path, exactly like
    a build with no decision API."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert 'const DEMO_TOUR = Boolean(appQuery.get("tour"));' in html
    # The buttons enable in the demo.
    assert "decisionApiConfigured && workflowApiConfigured && !DEMO_TOUR" in html
    # decide() skips both live-verification refusals.
    assert 'if (!DEMO_TOUR && workflowApiConfigured && workflowRefreshState !== "live")' in html
    assert "if (!DEMO_TOUR && decisionApiConfigured && workflowApiConfigured && !post.serverVerified)" in html
    # And the decision is stored locally, never POSTed on a placeholder session.
    assert "if (!base || DEMO_TOUR) return { demoOnly: true };" in html


def test_all_three_detail_rows_open_the_same_sheet():
    """Evidence opened a centred dialog; the plan and costs rows expanded
    inline underneath, three rows with two behaviours. All three are buttons
    now, opening the one sheet chrome -- same scrim, same panel, same close --
    with only the eyebrow and body changing."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    panel = html.split("function renderFeedDetailPanel", 1)[1].split(
        "function findToPost", 1)[0]
    # Buttons, not inline disclosures.
    assert '<details class="feed-plan"' not in panel
    assert '<details class="feed-more"' not in panel
    assert 'data-open-sheet="plan"' in panel
    assert 'data-open-sheet="costs"' in panel
    # One sheet builder drives all three, and the sheet says which it is so
    # the tour can tell them apart.
    assert "function detailSheetMarkup" in html
    assert "function openDetailSheet" in html
    assert "data-sheet-kind" in html

    # Day mode whitened the sheet's panel but left its text pale-on-pale;
    # the readability pass re-inks every piece of sheet content.
    sheets = html.split('<style id="feed-sheets-v68">', 1)[1].split("</style>", 1)[0]
    assert "body.day-mode .evidence-sheet-title" in sheets
    assert "body.day-mode .evidence-copy p" in sheets

    # The execution plan reads as a route, not a bullet list: numbered nodes
    # on a connecting line, the final step marked as the destination.
    assert "function planMapHtml" in html
    assert 'class="plan-map"' in html
    plan = html.split("function planSheetMarkup", 1)[1][:600]
    assert "planMapHtml" in plan


def test_evidence_rows_carry_an_honest_source_badge():
    """Each evidence row shows an icon for where it came from -- derived from
    the row's own source/kind, never invented. A named platform gets its mark;
    an anonymous "web" row gets a globe, reviews a star, trends a chart. No
    brand logo may appear on a row that does not name that brand."""
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "function evidenceSourceBadge" in html
    # Evidence rows read as exhibits: a quoted excerpt that clips long
    # scrapes behind an expand, the similarity kept as a number beside its
    # match bar -- and a way back to the source when the row stored one.
    assert "evidence-exhibit" in html
    assert "data-expand-evidence" in html
    assert "similarity.toFixed(3)" in html
    assert "exhibit-tag" not in html
    assert 'class="exhibit-link"' in html
    assert 'rel="noopener noreferrer"' in html
    # The pipeline carries observation.source_url end to end.
    export = (build_web.REPO / "scripts" / "export_fixture.py").read_text(encoding="utf-8")
    assert "o.source_url" in export
    build = (build_web.REPO / "scripts" / "build_web.py").read_text(encoding="utf-8")
    assert '"sourceUrl"' in build
    badge = html.split("function evidenceSourceBadge", 1)[1][:2600]
    # Kind-driven fallbacks, matched against the row's own text.
    assert "review" in badge and "trend" in badge
    # And the sheet rows actually use it.
    sheet = html.split("function exhibitListHtml", 1)[1][:2400]
    assert "evidenceSourceBadge" in sheet


def test_the_sly_facelift_dresses_both_surfaces():
    """The face-lift: warm near-black, bone ink, one brass accent, hairline
    borders, micro-label typography -- applied as final token-override blocks
    so every earlier honesty rule and theme block keeps its text intact."""
    landing = (build_web.SITE / "landing.html").read_text(encoding="utf-8")
    app = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert '<style id="facelift-sly-v1">' in landing
    face = landing.split('<style id="facelift-sly-v1">', 1)[1].split("</style>", 1)[0]
    assert "#0c0b09" in face.lower()
    assert "#e2a85c" in face.lower()

    assert '<style id="facelift-sly-v70">' in app
    owner = app.split('<style id="facelift-sly-v70">', 1)[1].split("</style>", 1)[0]
    assert "--owner-accent: #e2a85c" in owner.lower()
    assert "--owner-shell: #0c0b09" in owner.lower()
    # The second token family is retuned on the mode bodies too.
    assert "--owner-surface-raised: #1d1b14" in owner
    # The hero carries the living-network canvas, motion-respectful.
    assert 'id="netMap"' in landing
    assert "prefers-reduced-motion" in landing.split('<script id="net-map">', 1)[1]
    # The burn pass: drifting embers in the same canvas, and a cursor glow
    # that only follows a fine pointer and never runs under reduced motion.
    assert "embers" in landing.split('<script id="net-map">', 1)[1]
    assert 'id="burnGlow"' in landing
