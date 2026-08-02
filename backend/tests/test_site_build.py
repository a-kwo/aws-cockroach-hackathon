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
    """The nav offers signup; the page itself still opens the product.

    The two hero buttons deliberately bypass onboarding. A judge — or anyone
    else — must be able to reach the working demo without handing over a name,
    and that was the point of the assertion this replaces.
    """
    html = (build_web.SITE / "landing.html").read_text(encoding="utf-8")
    assert 'class="nav-cta" href="register/">Sign up</a>' in html
    assert 'href="app/"><span>Show me what I missed</span>' in html
    assert 'href="app/"><span>Show me my morning move</span>' in html


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


def test_new_owner_workspace_is_honest_until_radar_runs():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    assert "let posts = onboardingMode ? []" in html
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


def test_deploy_template_exposes_the_workflow_read_route():
    template = (build_web.REPO / "deploy" / "template.yaml").read_text(encoding="utf-8")
    assert "WorkflowFunction:" in template
    assert 'Path: /workflow' in template
    assert 'Method: GET' in template
    assert "WorkflowEndpoint:" in template


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
    assert "0 LLM tokens" in APP
    assert "/workflow" in APP
    assert "SQL-only CockroachDB read" in APP


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
    lose the lot."""
    html = (build_web.SITE / "signup.html").read_text(encoding="utf-8")

    assert 'if (!currentSession()) window.location.replace("../register/");' in html
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
    stored = html.split("localStorage.setItem(SESSION_KEY", 1)[1][:220]

    assert "token:" in stored
    assert "businessId:" in stored
    assert "password" not in stored


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
    assert 'window.location.replace("../login/")' in html


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
    # The distinction is made from the data, not from a flag someone can forget
    # to set: no finds AND no runs means nothing has ever looked.
    assert "const neverRan = !(btData.finds || []).length" in html
    assert "!(btData.runs || []).length" in html


def test_the_board_can_start_a_night_for_the_signed_in_business():
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "runEndpoint" in html
    assert "startFirstNight" in html
    # Same guard as everywhere else: the tenant comes from the session, so the
    # request carries no business id to spend someone else's money against.
    start = html.split("async function startFirstNight", 1)[1][:900]
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

    The operator owns no business, so For You and Growth would be empty for
    them — their session carries no tenant and the workflow endpoint answers
    401 by design. An owner must never see the Memory Engine, which reads
    across tenants.
    """
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")

    assert "const OPERATOR_SESSION = Boolean(readSession()?.isAdmin);" in html
    assert 'OPERATOR_SESSION\n        ? ["autopilot", "growth"]' in html
    assert ': ["admin"];' in html
    # Several call sites ask for "autopilot" by name; one guard inside
    # switchView beats four at the call sites, and cannot be forgotten at a fifth.
    assert 'if (OPERATOR_SESSION) viewName = "admin";' in html


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
