"""Splice real CockroachDB data into the product mock, producing web/index.html.

The mock in `Product Demo/` is the design the owner chose, and it is the frontend.
Rather than reinterpret it, this treats it as a template: the hardcoded literals
its scripted demo relies on are replaced with values exported from the live
cluster, and nothing else about it is touched.

    python scripts/export_fixture.py    # refresh demo.json from the cluster
    python scripts/build_web.py         # splice it into web/index.html

Why splice rather than fetch at runtime: the mock declares its data as top-level
`const`, so a script cannot reassign it, and rewriting the mock into modules would
be the reinterpretation this is trying to avoid. A build step keeps the mock
pristine and the transformation reviewable in a diff.

The one thing the mock has no answer for is long agent prose — it was written for
one-line finds and real Analyst output runs to twelve lines. `shorten()` below
carries over the logic from the React build's prose parser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOCK = REPO / "Product Demo" / "brasstacks-jar-demo.html"
FIXTURE = REPO / "frontend" / "src" / "fixtures" / "demo.json"
OUT_DIR = REPO / "web"
OUT = OUT_DIR / "index.html"

PER_MONTH = 30


def money(cents: int) -> str:
    d = cents / 100
    return f"${d:,.0f}" if cents % 100 == 0 else f"${d:,.2f}"


def short_money(cents: int) -> str:
    """'$2.9k' for the road labels, which have very little room."""
    d = cents / 100
    return f"${d / 1000:.1f}k" if d >= 1000 else f"${d:.0f}"


def shorten(text: str, limit: int = 92) -> str:
    """First clause of an agent paragraph, for a UI built to hold one line.

    Real `move` fields run to twelve lines in four different shapes. The mock's
    coin card holds roughly one, so take the clause before the first enumerator
    and trim on a word boundary. The full text still reaches the UI through the
    find's own detail, so nothing is lost — only this label is short.
    """
    text = " ".join((text or "").split())
    cut = re.split(r"\s*\((?:1|a)\)", text, maxsplit=1)[0]
    cut = re.split(r"(?<=[.;])\s", cut, maxsplit=1)[0]
    cut = cut.rstrip(" ,;:—-")
    if len(cut) <= limit:
        return cut
    return cut[:limit].rsplit(" ", 1)[0] + "…"


def title_for_coin(find: dict, limit: int = 34) -> str:
    """The coin label. Four words is the mock's rhythm; titles run to 98 chars."""
    title = " ".join(find["title"].split())
    title = re.split(r"\s*[:(]", title, maxsplit=1)[0].strip()
    if len(title) <= limit:
        return title
    return title[:limit].rsplit(" ", 1)[0] + "…"


def js(value) -> str:
    """JSON is a valid JS expression, and this keeps quoting correct."""
    return json.dumps(value, ensure_ascii=False)


def replace_array(html: str, name: str, body: str) -> str:
    """Swap a top-level `const NAME = [ ... ];` for generated contents."""
    pattern = re.compile(rf"const {name} = \[.*?\n\];", re.S)
    if not pattern.search(html):
        raise SystemExit(f"could not find `const {name} = [...]` in the mock")
    return pattern.sub(f"const {name} = [\n{body}\n];", html, count=1)


def build() -> None:
    if not FIXTURE.exists():
        raise SystemExit("frontend/src/fixtures/demo.json missing — run scripts/export_fixture.py")

    html = MOCK.read_text(encoding="utf-8")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    business = data["business"]
    summary = data["summary"]
    finds = data["finds"]

    open_finds = [f for f in finds if f["verdict"] is None and f["status"] in ("proposed", "later")]
    undecided = [f for f in open_finds if f["status"] == "proposed"]
    judged = [f for f in finds if f["verdict"] is not None]

    # --- FINDS: the coins that drop into the jars -------------------------
    coins = []
    for find in undecided[:6]:
        per_day = find["predicted_daily_cents"]
        coins.append(
            "  { emoji: %s, what: %s, day: %d, valTxt: %s, month: %s }"
            % (
                js(find["emoji"] or "💡"),
                js(title_for_coin(find)),
                round(per_day / 100),
                js(f"+{money(per_day)}"),
                js(f"≈ {money(per_day * PER_MONTH)} a month, every month"),
            )
        )
    html = replace_array(html, "FINDS", ",\n".join(coins) or "")

    # --- SIGNALS: the stops on the road to the goal -----------------------
    stops = []
    total = max(len(open_finds), 1)
    for i, find in enumerate(open_finds[:5]):
        monthly = find["predicted_daily_cents"] * PER_MONTH
        sources = len(find["evidence"])
        stops.append(
            "  { id: %s, kind: 'opp', icon: %s, tag: 'From your memory', t: %.2f, lbl: %s,\n"
            "    title: %s,\n"
            "    chart: { type: 'spark', pts: [20, 21, 20, 22, 21, 22], proj: [32, 44],"
            " stat: %s, good: true },\n"
            "    move: %s,\n"
            "    goal: %s,\n"
            "    why: %s, conf: %d,\n"
            "    val: %d, plan: %s,\n"
            "    act: %s, wip: %s }"
            % (
                js(find["id"][:8]),
                js(find["emoji"] or "💡"),
                0.18 + (0.64 * i / total),
                js(f"{title_for_coin(find, 18)} +{short_money(monthly)}"),
                js(" ".join(find["title"].split())),
                js(f"{sources} memories"),
                js(shorten(find["move"] or find["title"], 120)),
                js(f"Worth <b>+{money(monthly)}/mo</b> toward your goal"),
                js(f"{sources} things your agent remembers"),
                round(find["confidence"] * 100),
                round(monthly / 100),
                js(f"{find['emoji'] or '💡'} {title_for_coin(find, 22)}"),
                js("Do it — draft it for me"),
                js(f"{title_for_coin(find, 26)} — drafted for your approval"),
            )
        )
    for find in judged[:1]:
        stops.append(
            "  { id: %s, kind: 'okk', icon: %s, tag: 'Already checked', done: true, t: 0.10,"
            " lbl: %s,\n    title: %s,\n"
            "    chart: { type: 'bars', a: { l: 'Predicted', v: %d }, b: { l: 'Actual', v: %d } },\n"
            "    note: %s }"
            % (
                js(find["id"][:8]),
                js(find["emoji"] or "✓"),
                js(f"{title_for_coin(find, 14)} ✓"),
                js(" ".join(find["title"].split())),
                round(find["predicted_daily_cents"] / 100),
                round((find["actual_daily_cents"] or 0) / 100),
                js(find["note"] or "Checked against your sales."),
            )
        )
    html = replace_array(html, "SIGNALS", ",\n".join(stops) or "")

    # --- scalars ----------------------------------------------------------
    per_day_dollars = summary["verified_daily_cents"] / 100
    monthly_now = round(summary["verified_daily_cents"] * PER_MONTH / 100)
    goal_monthly = round((business["goal_monthly_cents"] or 800000) / 100)

    subs = [
        (r"let dayRate = \d+;", f"let dayRate = {round(per_day_dollars)};"),
        (r"let lifetime = [\d.]+;", f"let lifetime = {monthly_now:.2f};"),
        (r"const BASE_M = \d+;", f"const BASE_M = {monthly_now};"),
        (r"let GOAL_M = \d+;", f"let GOAL_M = {goal_monthly};"),
        (r"let GOAL_WHEN = '[^']*';", f"let GOAL_WHEN = {js(business.get('goal_note') or 'by fall')};"),
        # Header pills: real counts rather than the scripted demo's numbers.
        (r'(<span class="ln-s" id="lb4v">)[^<]*(</span>)', rf"\g<1>+${round(per_day_dollars)}/day\g<2>"),
        (r'(<span class="ln-s" id="lb5v">)[^<]*(</span>)',
         rf"\g<1>{len(open_finds)} to review\g<2>"),
        (r'(<div class="who">)[^<]*(</div>)', rf"\g<1>{business['name']}\g<2>"),
    ]
    for pattern, repl in subs:
        html, n = re.subn(pattern, repl, html, count=1)
        if n == 0:
            print(f"  warning: no match for {pattern[:48]}")

    # A banner, so nobody mistakes seeded demo data for a live customer.
    html = html.replace(
        "</body>",
        '<div style="position:fixed;bottom:8px;left:12px;font:11px/1.4 system-ui;'
        'color:#8B8778;z-index:99;pointer-events:none">'
        f"Live data · {data['corpus']['observations']} observations · "
        f"{summary['verified']} of {summary['judged']} calls verified"
        "</div>\n</body>",
    )

    OUT_DIR.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")

    print(f"built {OUT.relative_to(REPO)}")
    print(f"  {len(coins)} coins from undecided finds")
    print(f"  {len(stops)} stops on the road")
    print(f"  earning now ${per_day_dollars:.2f}/day · {monthly_now} of {goal_monthly} a month")


if __name__ == "__main__":
    build()
