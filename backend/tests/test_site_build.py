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

import pytest

import build_web
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


@pytest.mark.parametrize("note, expected", [
    # The Analyst's success note, verbatim from analyst.py.
    ("41 retrieved; proposed 'Lunch service' at +2300c/day, "
     "verify after 2026-08-17", 41),
    # Its failure note, which is worded differently and still carries the count.
    ("6 observations retrieved", 6),
    # The Radar seed run. No retrieval happened; the panel must not print 0.
    ("seeded 127 observations", None),
    ("memory is empty; no find proposed", None),
    ("", None),
    (None, None),
])
def test_run_retrieved_reads_the_count_out_of_the_run_note(note, expected):
    """"41 retrieved → 5 cited" is the most persuasive pair of numbers on the
    admin screen: the model saw 41 rows and could defend 5. The count is already
    in the run note, so it needs no column and no migration — but a run that
    retrieved nothing must report None, not 0, or a Radar row would claim it
    searched memory and came back empty-handed."""
    assert build_web.run_retrieved({"note": note}) == expected


def test_runs_carry_their_cost_and_their_output(data):
    data["runs"] = [{
        "id": "1" * 36, "agent": "analyst", "status": "ok",
        "started_at": "2026-07-30T02:00:00+00:00",
        "finished_at": "2026-07-30T02:01:18+00:00",
        "note": "41 retrieved; proposed 'x' at +2300c/day, verify after 2026-08-17",
        "model_id": "claude-opus-5", "error": None,
        "input_tokens": 8123, "output_tokens": 642,
        "observations": 0, "finds": 1, "artifacts": 0, "ledger_entries": 0,
    }]
    [run] = build_web.build_model(data)["runs"]

    assert run["modelId"] == "claude-opus-5"
    assert (run["inputTokens"], run["outputTokens"]) == (8123, 642)
    assert run["retrieved"] == 41
    assert run["produced"] == {"observations": 0, "finds": 1,
                               "artifacts": 0, "ledgerEntries": 0}


def test_a_run_that_called_no_model_reports_unknown_not_zero(data):
    """The Meter never calls a model. The view has to be able to print "no model
    call" rather than "0 tokens", and that distinction only survives if None
    survives the trip through the model."""
    data["runs"] = [{
        "id": "2" * 36, "agent": "meter", "status": "ok",
        "started_at": "2026-07-30T02:02:00+00:00",
        "finished_at": "2026-07-30T02:02:04+00:00",
        "note": "nothing due", "model_id": None, "error": None,
        "input_tokens": None, "output_tokens": None,
    }]
    [run] = build_web.build_model(data)["runs"]

    assert run["inputTokens"] is None
    assert run["outputTokens"] is None
    assert run["modelId"] is None


# ------------------------------------------------------------ find lineage


def test_a_find_carries_the_run_that_produced_it(data):
    data["finds"] = [find(run_id="7f3a91c2-0000-0000-0000-000000000000",
                          run_agent="analyst", seeded=False)]
    [found] = build_web.build_model(data)["finds"]

    assert found["runId"] == "7f3a91c2"
    assert found["seeded"] is False


def test_a_seeded_find_says_so_rather_than_showing_a_blank(data):
    """The console draws the gap deliberately: a seeded find has no Analyst run
    behind it, and saying so beside finds that do is a stronger claim than a
    uniform row of chips would be."""
    data["finds"] = [find(run_id=None, run_agent=None, seeded=True)]
    [found] = build_web.build_model(data)["finds"]

    assert found["seeded"] is True
    assert found["runId"] is None


def test_a_find_reports_when_it_was_found_as_a_fixed_date(data):
    """Never a relative time. The fixture is a frozen export, so "3 days ago"
    would be true on the day of the build and quietly become fiction after it."""
    [found] = build_web.build_model(data)["finds"]
    assert found["foundAtTxt"] == "1 Jun"


def test_a_find_is_labelled_by_the_kind_of_evidence_it_leaned_on(data):
    """Replaces the mock's invented "feature" tag with something the rows can
    support: the commonest kind among the observations actually cited."""
    data["finds"] = [find(evidence=[
        {"rank": 0, "similarity": 0.7, "observation_id": "o1", "content": "a",
         "kind": "review", "source_name": None, "subject": None,
         "observed_at": "2026-06-02T19:00:00+00:00"},
        {"rank": 1, "similarity": 0.6, "observation_id": "o2", "content": "b",
         "kind": "rival_price", "source_name": None, "subject": None,
         "observed_at": "2026-06-02T19:00:00+00:00"},
        {"rank": 2, "similarity": 0.5, "observation_id": "o3", "content": "c",
         "kind": "review", "source_name": None, "subject": None,
         "observed_at": "2026-06-02T19:00:00+00:00"},
    ])]
    [found] = build_web.build_model(data)["finds"]
    assert found["topKindLabel"] == "Reviews"


def test_a_find_with_no_evidence_claims_no_source(data):
    data["finds"] = [find(evidence=[])]
    [found] = build_web.build_model(data)["finds"]
    assert found["topKindLabel"] is None


# ---------------------------------------------------------- export receipt


def test_the_receipt_reaches_the_page(data):
    data["_generated"] = "2026-07-31T04:12:00+00:00"
    data["_receipt"] = {
        "database": "defaultdb", "version": "CockroachDB CCL v26.2.1",
        "clusterId": "abc-123",
        "queries": [{"name": "finds", "sql": "SELECT 1", "rows": 15,
                     "client_ms": 42.5}],
    }
    model = build_web.build_model(data)

    assert model["generated"] == "2026-07-31T04:12:00+00:00"
    assert model["receipt"]["version"] == "CockroachDB CCL v26.2.1"
    assert model["receipt"]["queries"][0]["rows"] == 15


def test_a_fixture_with_no_receipt_still_builds(data):
    """An older fixture, or one exported by a role that cannot read
    crdb_internal. The page says the stamp is missing; it does not invent one
    and it does not fail to build."""
    model = build_web.build_model(data)
    assert model["generated"] is None
    assert model["receipt"] is None


# ------------------------------------------------------ the Maker's drafts


def test_a_shelved_find_is_not_a_drafted_artifact(data):
    """The console rendered `saved.length` as "N drafted" — the Later jar, not
    the artifact table. A find the owner shelved is not a deliverable that
    exists, and the artifact table is empty."""
    data["finds"] = [find(status="later", verdict=None, actual_daily_cents=None,
                          measured_at=None)]
    model = build_web.build_model(data)
    assert len(model["saved"]) == 1
    assert model["artifacts"] == 0


def test_a_drafted_artifact_is_counted_on_the_find_that_earned_it(data):
    data["finds"] = [find(artifacts=[
        {"id": "a1", "kind": "review_reply", "title": "Reply to Dana",
         "preview": "Thank you for…", "created_at": "2026-06-25T02:00:00+00:00"},
    ])]
    model = build_web.build_model(data)
    assert model["artifacts"] == 1
    assert model["finds"][0]["artifactCount"] == 1
