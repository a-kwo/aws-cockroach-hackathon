"""The honesty invariant that lives in JavaScript.

`test_site_build.py` guards what the build produces. It cannot guard what the
page does with it — and the single most important rule on the Growth screen is a
rule about the page: **nothing the owner does in the interface may raise the
verified figure.** The mock this frontend descends from broke that twice, once by
growing the money bar when a chat question was answered and once by bumping the
verified daily rate when a find was accepted.

So the four functions that decide it are fenced in `site/app.html` between
BT-PURE-START and BT-PURE-END, take everything they need as arguments, and are
lifted out and run under node here. Purity is the mechanism, not a style
preference: `growthTotals` cannot reach `btData.summary` by accident if it can
only see what it was passed.

Skipped rather than failed where node is absent, so a contributor with a Python
environment alone still gets a green board.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap

import pytest

import build_web

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; the JS invariants are checked where it is",
)

FENCE = re.compile(r"BT-PURE-START(.*?)BT-PURE-END", re.S)


def pure_region() -> str:
    html = (build_web.SITE / "app.html").read_text(encoding="utf-8")
    match = FENCE.search(html)
    assert match, "the BT-PURE fence is gone from site/app.html"
    return match.group(1)


def run_js(body: str, data: dict, decisions: dict):
    """Evaluate `body` with the fenced functions in scope; print JSON on stdout."""
    script = "\n".join([
        pure_region(),
        f"const DATA = {json.dumps(data)};",
        f"const DECISIONS = {json.dumps(decisions)};",
        textwrap.dedent(body),
    ])
    done = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# ------------------------------------------------------------------ fixtures


def js_find(**over):
    base = {
        "id": "aaaaaaaa", "title": "Lunch service", "status": "proposed",
        "verdict": None, "predictedDaily": 2300, "predictedMonthly": 69000,
        "predictedMonthlyTxt": "$690", "emoji": "🍰", "evidence": [],
    }
    base.update(over)
    return base


@pytest.fixture
def data():
    return {
        "summary": {"monthlyNowTxt": "$3,795", "verified": 6},
        "finds": [
            js_find(id="aaaaaaaa", predictedMonthly=69000),
            js_find(id="bbbbbbbb", predictedMonthly=18000),
            js_find(id="cccccccc", status="live", verdict="verified",
                    predictedMonthly=60000),
            js_find(id="dddddddd", status="live", verdict=None,
                    predictedMonthly=291000),
            js_find(id="eeeeeeee", status="later", predictedMonthly=12000),
        ],
        "proposed": ["aaaaaaaa", "bbbbbbbb"],
    }


# --------------------------------------------------- the load-bearing rule


def test_approving_everything_does_not_move_the_verified_figure(data):
    """The rule, stated as a test. Approve every open find and the verified
    figure is byte-identical to the one shown before any decision — and both are
    exactly the string the build produced from the ledger.

    If this ever fails, the interface has started paying the owner in numbers
    nobody measured."""
    every = {f["id"]: {"status": "approved", "at": 1}
             for f in data["finds"] if f["status"] == "proposed"}

    out = run_js("""
        const none = growthTotals(DATA, {});
        const all = growthTotals(DATA, DECISIONS);
        console.log(JSON.stringify({
          none: none.verifiedTxt, all: all.verifiedTxt,
          noneForecast: none.forecastCents, allForecast: all.forecastCents,
        }));
    """, data, every)

    assert out["none"] == out["all"] == data["summary"]["monthlyNowTxt"]
    # The forecast is the figure that is allowed to move, and it must.
    assert out["allForecast"] > out["noneForecast"]


def test_verified_money_is_never_counted_twice(data):
    """A verified find's money is already inside the verified figure. Adding it
    to the forecast as well would double-count it — the same failure
    test_only_verified_money_counts_toward_the_daily_rate guards in Python."""
    out = run_js("""
        const t = growthTotals(DATA, {});
        console.log(JSON.stringify({
          forecast: t.forecastCents,
          approvedIds: t.approved.map(f => f.id),
          judgedIds: t.judged.map(f => f.id),
        }));
    """, data, {})

    assert "cccccccc" not in out["approvedIds"]
    assert out["judgedIds"] == ["cccccccc"]
    # Only the accepted-but-unmeasured find contributes before any decision.
    assert out["forecast"] == 291000


def test_the_forecast_is_summed_in_whole_cents(data):
    """Money is integer cents everywhere in this system, including here. A
    floating-point sum would drift a cent or two and print a figure that does
    not reconcile with the rows behind it."""
    out = run_js("""
        const t = growthTotals(DATA, DECISIONS);
        console.log(JSON.stringify({cents: t.forecastCents, txt: t.forecastTxt}));
    """, data, {"aaaaaaaa": {"status": "approved", "at": 1}})

    assert out["cents"] == 291000 + 69000
    assert isinstance(out["cents"], int)
    assert out["txt"] == build_web.money(out["cents"])


# ------------------------------------------------------- decision precedence


@pytest.mark.parametrize("status, verdict, local, expected", [
    ("proposed", None, None, "pending"),
    ("proposed", None, "approved", "approved"),
    ("proposed", None, "rejected", "rejected"),
    # The database's word beats the browser's, every time.
    ("live", None, "rejected", "approved"),
    ("accepted", None, "rejected", "approved"),
    ("rejected", None, "approved", "rejected"),
    ("retired", None, "approved", "rejected"),
    ("later", None, "approved", "saved"),
    ("live", "verified", "rejected", "judged"),
    ("live", "miss", "approved", "judged"),
])
def test_the_database_status_outranks_local_storage(status, verdict, local, expected):
    """CockroachDB is the record; localStorage is a demo overlay that applies
    only to finds the database still lists as proposed."""
    find = js_find(status=status, verdict=verdict)
    decisions = {find["id"]: {"status": local, "at": 1}} if local else {}

    out = run_js("""
        console.log(JSON.stringify(effectiveDecision(DATA.finds[0], DECISIONS)));
    """, {"finds": [find]}, decisions)

    assert out == expected


def test_a_stale_id_from_an_older_demo_is_inert():
    """The storage key was deliberately not bumped: old ids were slugs and new
    ones are eight hex characters, so a leftover entry can never collide. It has
    to be ignored rather than crash or leak into a count."""
    data = {"finds": [js_find()], "proposed": ["aaaaaaaa"],
            "summary": {"monthlyNowTxt": "$0"}}
    decisions = {"afternoon-bundle": {"status": "approved", "at": 1},
                 "weekend-demand": {"status": "rejected", "at": 2}}

    out = run_js("""
        console.log(JSON.stringify({
          deck: deckFinds(DATA, DECISIONS).map(f => f.id),
          approved: growthTotals(DATA, DECISIONS).approved.length,
        }));
    """, data, decisions)

    assert out["deck"] == ["aaaaaaaa"]
    assert out["approved"] == 0


# --------------------------------------------------------------- deck order


def test_the_deck_holds_only_open_proposals_in_build_order(data):
    """The build sorts `proposed` by predicted value, biggest first, and
    deliberately not by retrieval similarity. The page consumes that order
    rather than re-deriving it — there is no sort here to get wrong."""
    out = run_js("""
        console.log(JSON.stringify(deckFinds(DATA, DECISIONS).map(f => f.id)));
    """, data, {})

    assert out == ["aaaaaaaa", "bbbbbbbb"]


def test_deciding_a_find_removes_it_from_the_deck_without_reordering(data):
    out = run_js("""
        console.log(JSON.stringify(deckFinds(DATA, DECISIONS).map(f => f.id)));
    """, data, {"aaaaaaaa": {"status": "approved", "at": 1}})

    assert out == ["bbbbbbbb"]


def test_the_deck_never_contains_a_find_the_database_already_decided(data):
    """Live, judged and shelved finds are the record, not a decision waiting to
    be made. Only `proposed` reaches the deck."""
    out = run_js("""
        console.log(JSON.stringify(deckFinds(DATA, {}).map(f => f.status)));
    """, data, {})

    assert set(out) == {"proposed"}


# ------------------------------------------------------------- money format


@pytest.mark.parametrize("cents", [0, 400, 2300, 2350, 12650, 800000, 291000, 1])
def test_the_one_money_formatter_in_js_matches_the_python(cents):
    """Every other figure on the page is a string the build produced. This is
    the single exception, so it has to render identically to build_web.money()
    or two figures from the same rows will disagree on screen."""
    out = run_js(f"""
        console.log(JSON.stringify(centsToText({cents})));
    """, {}, {})

    assert out == build_web.money(cents)
