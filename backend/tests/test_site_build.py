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
    for name in ("app.html", "landing.html"):
        html = (build_web.SITE / name).read_text(encoding="utf-8")
        assert build_web.DATA_TAG.search(html), name


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
    """`agent_run` has token columns and nothing in the codebase ever writes
    them. Zero would read as "this run was free"; null plus a stated source
    reads as what it is."""
    data["runs"] = [{"id": "r" * 36, "agent": "radar", "status": "ok",
                     "started_at": "2026-07-28T10:00:00+00:00",
                     "finished_at": "2026-07-28T10:01:18+00:00",
                     "note": "127 observed", "model_id": None}]
    run = build_web.build_model(data)["runs"][0]
    assert run["inputTokens"] is None and run["outputTokens"] is None
    assert run["tokensSource"] == "unrecorded"
    assert run["modelId"] is None and run["error"] is None


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
