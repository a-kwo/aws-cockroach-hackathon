"""What site/app.html is not allowed to contain.

These read the template as text rather than running it, in the same spirit as
`test_templates_carry_the_data_placeholder`. Two kinds of rule:

* **No invented business.** The Autopilot tab shipped five hardcoded
  recommendations for a coffee shop that does not exist, on the same screen as
  two tabs rendering audited rows for a different one. Nothing here may reappear.
* **No re-rendering the deck on navigation.** That is a fix, not a preference:
  rebuilding `queueContent.innerHTML` mounts fresh nodes already at their final
  computed style, so the CSS transition has nothing to interpolate from and every
  arrow press, dot press and arrow key snaps. It was fixed once; this keeps it
  fixed.
"""

from __future__ import annotations

import re

import pytest

import build_web


#: Whole-line `//` comments only — anything narrower would eat the `//` in a URL.
LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.M)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


@pytest.fixture(scope="module")
def html():
    return (build_web.SITE / "app.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(html):
    """The template with every comment removed.

    These tests assert on what the page *does*. Without this they assert on what
    it says about itself, and a comment explaining a bug that was fixed reads
    identically to the bug.
    """
    stripped = HTML_COMMENT.sub("", html)
    stripped = BLOCK_COMMENT.sub("", stripped)
    return LINE_COMMENT.sub("", stripped)


def strip_safe_calls(source: str) -> str:
    """Remove every `safe( … )` call, parens balanced, leaving what it wrapped out.

    Lets a test say "this field never reaches the page unescaped" directly,
    rather than trying to write one regex that understands nested template
    literals.
    """
    out, i = [], 0
    while True:
        at = source.find("safe(", i)
        if at == -1:
            out.append(source[i:])
            return "".join(out)
        out.append(source[i:at])
        depth, j = 0, at + len("safe(") - 1
        while j < len(source):
            if source[j] == "(":
                depth += 1
            elif source[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1


def body_of(html: str, name: str) -> str:
    """The source of one top-level `function name(...) { ... }`, brace-matched."""
    start = html.index(f"function {name}(")
    depth, i, opened = 0, html.index("{", start), False
    while i < len(html):
        if html[i] == "{":
            depth, opened = depth + 1, True
        elif html[i] == "}":
            depth -= 1
            if opened and depth == 0:
                return html[start:i + 1]
        i += 1
    raise AssertionError(f"could not find the end of {name}()")


# --------------------------------------------------------- no invented data


def test_the_page_names_no_business_of_its_own(html):
    """The tenant's name is a column on the `business` row. It used to be a
    literal in the topbar naming a different business from the one every other
    panel described."""
    assert "Northstar" not in html
    assert 'id="brandBusiness"' in html


def test_the_hardcoded_recommendation_array_is_gone(html):
    assert "const posts = [" not in html
    for invented in ("afternoon-bundle", "Oak & Pine", "coffee-pastry-bundle",
                     "earlier-sunday-opening", "cold brew"):
        assert invented not in html, invented


def test_the_unbuilt_stand_in_invents_nothing(html):
    """This block is what someone sees who opens the template without running
    the build. Every collection in it is empty and every figure absent, so an
    unbuilt page shows no find, no money and no business rather than a
    convincing fiction."""
    import json

    match = re.search(
        r'<script id="bt-data" type="application/json">(.*?)</script>', html, re.S)
    assert match, "the data placeholder is gone"
    stand_in = json.loads(match.group(1))

    assert stand_in["finds"] == []
    assert stand_in["proposed"] == []
    assert stand_in["months"] == []
    assert stand_in["runs"] == []
    assert stand_in["business"]["name"] == ""
    assert stand_in["summary"] == {}


def test_the_reporting_period_is_not_a_literal(html, code):
    """"August 2026" happened to match one build's projection and would have
    gone quietly stale on the next."""
    assert "August 2026" not in code
    assert 'id="growthPeriod"' in html


# ------------------------------------------------- the deck does not snap


@pytest.mark.parametrize("name", ["goToPost", "applyCardPositions", "updateFeedChrome"])
def test_only_a_full_render_rewrites_the_deck(code, name):
    """Navigation swaps position classes on the nodes that are already mounted.
    The moment one of these reaches for innerHTML or a render call, the deck
    starts snapping again — silently, because nothing else breaks."""
    source = body_of(code, name)
    assert "innerHTML" not in source, f"{name} rebuilds the deck"
    for renderer in ("renderApp(", "renderAutopilot("):
        assert renderer not in source, f"{name} re-renders the deck"


def test_the_deck_is_written_in_exactly_one_place(code):
    """`queueContent.innerHTML` is assigned twice — the empty state and the deck
    — and both live inside renderAutopilot(). A third assignment anywhere else
    is how the snapping bug comes back."""
    assert code.count("queueContent.innerHTML") == 2
    assert body_of(code, "renderAutopilot").count("queueContent.innerHTML") == 2


@pytest.mark.parametrize("selector", [
    ".feed-card", "feedStage", "data-post-id", "data-index",
    "data-feed-index", "data-decide", "data-open-post",
])
def test_the_gesture_code_still_has_everything_it_queries(html, selector):
    assert selector in html


# ------------------------------------------------------- escaping and purity


def test_the_pure_region_is_fenced_and_self_contained(html, code):
    """The fence is what backend/tests/test_app_logic_js.py lifts out. If these
    functions start reading btData or state directly, the invariant they encode
    stops being testable — which is the point of the fence."""
    assert "// BT-PURE-START" in html and "// BT-PURE-END" in html, "the fence is gone"
    # Sliced from the raw file, then de-commented: the prose above these
    # functions explains what they must not touch and so names all of it, and
    # the sentinels are themselves line comments.
    region = html.split("// BT-PURE-START", 1)[1].split("// BT-PURE-END", 1)[0]
    region = LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", region))

    for name in ("centsToText", "effectiveDecision", "deckFinds", "growthTotals"):
        assert f"function {name}(" in region, name
    for forbidden in ("btData", "state.", "document.", "localStorage"):
        assert forbidden not in region, forbidden


def test_the_growth_record_is_never_recomputed_from_decisions(code):
    """The verified figure is assigned from the build's string. If it is ever
    summed here, the interface has gained the ability to pay the owner."""
    source = body_of(code, "growthTotals")
    verified = [line for line in source.splitlines() if "verifiedTxt" in line]
    assert verified, "growthTotals no longer reports a verified figure"
    assert all("monthlyNowTxt" in line for line in verified)


#: Every field on these templates that carries text a model wrote, or that a
#: model's text can reach. Numbers and booleans are not listed: they cannot
#: carry markup, and escapeHtml throws on them anyway.
AGENT_WRITTEN = [
    "find.title", "find.move", "find.emoji", "find.id",
    "find.topKindLabel", "find.foundAtTxt", "find.predictedMonthlyTxt",
    "find.rationale", "find.actualDailyTxt", "top.content", "top.source",
]


@pytest.mark.parametrize("renderer", ["renderFeedCard", "renderDecisionRow",
                                      "renderDrawer"])
def test_agent_written_text_reaches_the_page_escaped(code, renderer):
    """Interpolating unescaped literals was merely careless while the array was
    five hardcoded strings. The same templates now carry `move`, `rationale` and
    stored review text written by a model and by strangers on review sites."""
    # `.textContent = x` is dropped first: it assigns a string to a text node
    # and never parses markup, so it is safe by construction and escaping it
    # would double-encode an ampersand in a real restaurant's name.
    source = re.sub(r"\.textContent\s*=[^;]*;", "", body_of(code, renderer), flags=re.S)
    bare = strip_safe_calls(source)
    leaked = [field for field in AGENT_WRITTEN if field in bare]
    assert not leaked, f"{renderer} interpolates unescaped: {leaked}"
