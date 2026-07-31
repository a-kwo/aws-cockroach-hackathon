"""Build the public site from live CockroachDB data.

    python scripts/export_fixture.py   # refresh demo.json from the cluster
    python scripts/build_web.py        # render web/

Output:

    web/index.html        the landing page      (site/landing.html + data)
    web/app/index.html    the dashboard         (site/app.html + data)

The templates in `site/` are the product's source. `Product Demo/` holds the
original mock, which predates this project and stays untouched as the provenance
artifact — it is design reference, not a build input.

Why a build step rather than a fetch at runtime: there is no API yet, and when
one lands it will serve exactly the payload this script computes. Making that
payload the contract now means the API can be dropped in without the pages
changing. It also means the landing page's numbers come from the cluster, so a
marketing claim cannot drift from the record it describes.

Every money value crossing into the page is integer cents, formatted once here.
The page never does arithmetic on money.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "site"
FIXTURE = REPO / "db" / "fixtures" / "demo.json"
OUT_DIR = REPO / "web"

#: A month of a per-day rate. The product speaks in "a month, every month", and
#: 30 is the figure the seeded predictions were written against.
PER_MONTH = 30

#: The concrete hypothesis queries the Analyst issues every night, from
#: backend/src/brasstacks/agents/analyst.py. Shown in the run receipt so the
#: retrieval step is visible rather than asserted. Kept in sync by a test.
ANALYST_QUERIES = [
    "Which dishes do customers praise most, and are they underpriced compared to nearby restaurants?",
    "Customers complain about waiting a long time for a table, or leaving without eating",
    "Is there demand at times or days this restaurant is not currently open, such as lunch?",
    "What are nearby competing restaurants charging, and what have they changed recently?",
    "What do the lowest-rated reviews complain about, and is it something the owner can fix?",
    "What local trends or nearby developments could change demand in the next few months?",
]

KIND_LABEL = {
    "review": "Reviews",
    "trend": "Local trends",
    "rival_price": "Rival prices",
    "rival_menu": "Rival menus",
    "social": "Forums",
    "owner_upload": "From you",
}
KIND_EMOJI = {
    "review": "⭐", "trend": "📈", "rival_price": "💵",
    "rival_menu": "📋", "social": "💬", "owner_upload": "📎",
}

SOURCE_LABEL = {
    "review": "a customer review",
    "social": "a local forum post",
    "trend": "a local trend",
    "rival_price": "a rival's prices",
    "rival_menu": "a rival's menu",
    "owner_upload": "something you told me",
}


# ---------------------------------------------------------------- formatting


def money(cents: int) -> str:
    d = cents / 100
    return f"${d:,.0f}" if cents % 100 == 0 else f"${d:,.2f}"


def short_money(cents: int) -> str:
    """'$2.9k' where the layout has very little room."""
    d = cents / 100
    return f"${d / 1000:.1f}k" if d >= 1000 else f"${d:,.0f}"


def clamp(text: str, limit: int) -> str:
    """Trim on a word boundary. Never mid-word — the road labels used to clip."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def short_title(title: str, limit: int = 34) -> str:
    title = re.split(r"\s*[:(]", " ".join(title.split()), maxsplit=1)[0].strip()
    return clamp(title, limit)


def bullets(move: str) -> list[str]:
    """Split an agent paragraph into the concrete actions it contains.

    Real `move` text arrives in four shapes: one sentence, an enumerated list
    "(1) … (2) …", a lettered list, and several sentences. The mock was written
    for one line and the previous build truncated everything after the first
    clause, throwing away most of what the agent actually said. Linear's agent
    panel shows the better answer: one line per concrete action.
    """
    text = " ".join((move or "").split())
    if not text:
        return []
    parts = re.split(r"\s*\((?:\d+|[a-e])\)\s*", text)
    parts = [p for p in (p.strip() for p in parts) if p]
    if len(parts) > 1:
        return [p.rstrip(" ;,") for p in parts]
    sentences = re.split(r"(?<=[.;])\s+", text)
    return [s.strip().rstrip(";") for s in sentences if s.strip()]


def month_label(value: str) -> str:
    return date.fromisoformat(value[:10]).strftime("%b")


def run_seconds(row: dict) -> int | None:
    """Wall-clock seconds an agent_run took, or None while it is still in flight.

    None rather than 0 on purpose: the admin view renders a missing duration as
    "running", and a zero would read as a night that finished instantly.
    """
    started, finished = row.get("started_at"), row.get("finished_at")
    if not started or not finished:
        return None
    return round(
        (datetime.fromisoformat(finished) - datetime.fromisoformat(started))
        .total_seconds()
    )


#: The Analyst writes its retrieval count into the run note — "41 retrieved;
#: proposed …" on the happy path, "6 observations retrieved" when the model call
#: failed. Reading it back needs no column and no migration. Pinned against the
#: agent's real output by a test in test_agents.py, so a reworded note fails
#: loudly rather than silently blanking the number on screen.
RETRIEVED = re.compile(r"^(\d+)\s+(?:observations\s+)?retrieved\b")


def run_retrieved(row: dict) -> int | None:
    """How many observations a run's vector search returned, or None.

    None rather than 0 for a run that never searched — a Radar row reporting
    "0 retrieved" would claim it queried memory and came back empty-handed.
    """
    match = RETRIEVED.match((row.get("note") or "").strip())
    return int(match.group(1)) if match else None


def top_kind_label(evidence: list[dict]) -> str | None:
    """The commonest kind among the observations a find actually cited.

    Replaces the mock's invented "feature" tag — Competitors / Reviews / Demand
    — with a label the rows can support. None when a find cites nothing, because
    a find with no evidence has no source to name.
    """
    if not evidence:
        return None
    counts = Counter(e["kind"] for e in evidence)
    # Ties broken by citation order, so the label is stable across builds.
    kind = max(counts, key=lambda k: (counts[k], -[e["kind"] for e in evidence].index(k)))
    return KIND_LABEL.get(kind, kind)


def when(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%-d %b") if False else \
        datetime.fromisoformat(value).strftime("%d %b").lstrip("0")


# ---------------------------------------------------------------- view model


def build_model(data: dict) -> dict:
    business = data["business"]
    summary = data["summary"]
    corpus = data["corpus"]

    goal_monthly = business.get("goal_monthly_cents") or 800000
    daily = int(summary["verified_daily_cents"])
    monthly_now = daily * PER_MONTH

    finds = []
    for f in data["finds"]:
        evidence = sorted(f["evidence"], key=lambda e: e["rank"])
        predicted = int(f["predicted_daily_cents"])
        actual = f["actual_daily_cents"]
        run_id = f.get("run_id")
        finds.append({
            "id": f["id"][:8],
            # Lineage. `seeded` is derived by the exporter, which is where the
            # agent that wrote the run is known; the default here is True so an
            # older fixture with no lineage at all claims Analyst work for
            # nothing.
            "runId": run_id[:8] if run_id else None,
            "seeded": bool(f.get("seeded", True)),
            # A fixed date, never a relative one: the fixture is a frozen
            # export, so "3 days ago" is true on build day and fiction after it.
            "foundAtTxt": when(f.get("created_at")),
            "topKindLabel": top_kind_label(evidence),
            "emoji": f["emoji"] or "💡",
            "title": " ".join(f["title"].split()),
            "shortTitle": short_title(f["title"]),
            "tinyTitle": short_title(f["title"], 20),
            "move": " ".join((f["move"] or "").split()),
            "bullets": bullets(f["move"]),
            "rationale": " ".join((f["rationale"] or "").split()),
            "predictedDaily": predicted,
            "predictedDailyTxt": money(predicted),
            # Integer cents as well as the formatted string: the Growth forecast
            # is the one figure that depends on a runtime choice, so the page
            # has to be able to sum it. Cents, so the sum stays exact.
            "predictedMonthly": predicted * PER_MONTH,
            "predictedMonthlyTxt": money(predicted * PER_MONTH),
            "predictedMonthlyShort": short_money(predicted * PER_MONTH),
            "actualDaily": int(actual) if actual is not None else None,
            "actualDailyTxt": money(int(actual)) if actual is not None else None,
            "confidence": round((f["confidence"] or 0) * 100),
            "status": f["status"],
            "verdict": f["verdict"],
            "method": f["method"],
            "note": f["note"],
            "measuredAt": f["measured_at"],
            "verifyAfter": f["verify_after"],
            "evidenceCount": len(evidence),
            # The Maker's deliverables, counted where they were written. The
            # console used to render the Later-jar bucket as "N drafted", which
            # claimed a draft that does not exist.
            "artifactCount": len(f.get("artifacts") or []),
            "topSimilarity": round(evidence[0]["similarity"], 3) if evidence else None,
            "evidence": [{
                "content": " ".join(e["content"].split()),
                "kind": e["kind"],
                "source": SOURCE_LABEL.get(e["kind"], e["kind"]),
                "when": when(e["observed_at"]),
                "similarity": round(e["similarity"], 3),
            } for e in evidence],
        })

    by_id = {f["id"]: f for f in finds}
    open_finds = [f for f in finds if f["verdict"] is None
                  and f["status"] in ("proposed", "later")]
    # Biggest opportunity first — what an owner with ten minutes would want.
    # Deliberately not ordered by retrieval similarity: leading with the
    # best-matched find would imply that a closer match is a better bet, and in
    # this dataset it is not.
    proposed = sorted((f for f in open_finds if f["status"] == "proposed"),
                      key=lambda f: -f["predictedDaily"])
    saved = [f for f in open_finds if f["status"] == "later"]
    measuring = [f for f in finds if f["verdict"] is None
                 and f["status"] in ("accepted", "live")]
    earning = [f for f in finds if f["verdict"] == "verified"]
    judged = [f for f in finds if f["verdict"] is not None]

    # --- the growth chart. Real months, then one projection tied to named
    # --- pending finds. Never a ramp of invented futures.
    months = []
    running = 0
    for row in data["monthly"]:
        running += int(row["verified_daily_cents"])
        months.append({
            "label": month_label(row["month"]),
            "iso": row["month"][:10],
            "cents": running * PER_MONTH,
            "baseCents": running * PER_MONTH,
            "txt": short_money(running * PER_MONTH),
            "verified": row["verified"],
            "miss": row["miss"],
            "projected": False,
        })

    pending_cents = sum(f["predictedDaily"] for f in measuring)
    if months and pending_cents:
        last = date.fromisoformat(data["monthly"][-1]["month"][:10])
        nxt = date(last.year + (last.month == 12), (last.month % 12) + 1, 1)
        months.append({
            "label": nxt.strftime("%b"),
            "iso": nxt.isoformat(),
            "cents": (running + pending_cents) * PER_MONTH,
            "baseCents": (running + pending_cents) * PER_MONTH,
            "txt": short_money((running + pending_cents) * PER_MONTH),
            "verified": 0,
            "miss": 0,
            "projected": True,
            "note": (
                "if the one find still being measured lands as predicted"
                if len(measuring) == 1 else
                f"if the {len(measuring)} finds still being measured land as predicted"
            ),
        })

    # --- one status line, computed. Replaces three scripted claims that
    # --- contradicted each other on screen.
    bits = [f"{money(daily)}/day earning now"]
    if proposed:
        bits.append(f"{len(proposed)} waiting on you")
    if measuring:
        bits.append(f"{len(measuring)} still measuring")
    status_line = " · ".join(bits)

    hit = summary["hit_rate"]
    return {
        "business": {
            "name": business["name"],
            "city": business.get("city"),
            "goalMonthly": goal_monthly,
            "goalMonthlyTxt": money(goal_monthly),
            "goalNote": business.get("goal_note") or "",
        },
        "summary": {
            "verified": summary["verified"],
            "miss": summary["miss"],
            "estimated": summary["estimated"],
            "judged": summary["judged"],
            "hitRate": round(hit * 100) if hit is not None else None,
            "dailyCents": daily,
            "dailyTxt": money(daily),
            "monthlyNow": monthly_now,
            "monthlyNowTxt": money(monthly_now),
            "toGoTxt": money(max(goal_monthly - monthly_now, 0)),
            "goalPct": min(round(monthly_now / goal_monthly * 100), 100),
        },
        "corpus": {
            "observations": corpus["observations"],
            "evidenceRows": sum(f["evidenceCount"] for f in finds),
            "earliest": corpus.get("earliest"),
            "latest": corpus.get("latest"),
        },
        # agent_run rows, for the operator view. The seeded corpus carries only
        # the run that built it — the admin screen says so rather than padding
        # the list out to look busier than the cluster is.
        "runs": [{
            "id": r["id"][:8],
            "agent": r["agent"],
            "status": r["status"],
            "startedAt": r["started_at"],
            "finishedAt": r.get("finished_at"),
            "seconds": run_seconds(r),
            "note": r.get("note") or "",
            "error": r.get("error"),
            # None, never 0. A run that called no model must be able to say so
            # — see providers.recorded_tokens.
            "modelId": r.get("model_id"),
            "inputTokens": r.get("input_tokens"),
            "outputTokens": r.get("output_tokens"),
            "retrieved": run_retrieved(r),
            "produced": {
                "observations": r.get("observations") or 0,
                "finds": r.get("finds") or 0,
                "artifacts": r.get("artifacts") or 0,
                "ledgerEntries": r.get("ledger_entries") or 0,
            },
        } for r in data.get("runs", [])],
        # The list is capped at export time. Carrying the total is what lets the
        # panel say it is showing a window rather than the whole record.
        "runCount": data.get("run_count", len(data.get("runs", []))),
        "statusLine": status_line,
        # What the Maker has actually drafted. Zero until it runs, and the
        # console says "none drafted yet" rather than borrowing another count.
        "artifacts": sum(f["artifactCount"] for f in finds),
        "finds": finds,
        "proposed": [f["id"] for f in proposed],
        "saved": [f["id"] for f in saved],
        "measuring": [f["id"] for f in measuring],
        "earning": [f["id"] for f in earning],
        "judged": [f["id"] for f in judged],
        "months": months,
        # What the agent is actually watching, and what the reviews actually
        # say. The mock scored six invented "business health" dimensions
        # against invented peers; these two are the only such panels the data
        # can support, so they are the only two that remain.
        "kinds": [{
            "kind": k["kind"],
            "label": KIND_LABEL.get(k["kind"], k["kind"]),
            "emoji": KIND_EMOJI.get(k["kind"], "•"),
            "count": k["count"],
        } for k in data["kinds"]],
        "ratings": [{
            "week": r["week"][:10],
            "label": date.fromisoformat(r["week"][:10]).strftime("%d %b").lstrip("0"),
            "avg": float(r["avg_rating"]),
            "reviews": r["reviews"],
        } for r in data["ratings"]],
        "queries": ANALYST_QUERIES,
        # The cluster's own clock and the SQL that ran, in place of a pulsing
        # dot. Both may be absent — an older fixture, or a role that cannot read
        # crdb_internal — and the page says so rather than inventing a stamp.
        "generated": data.get("_generated"),
        "receipt": data.get("_receipt"),
        "_by_id": list(by_id),
    }


# ---------------------------------------------------------------- rendering


DATA_TAG = re.compile(
    r'(<script id="bt-data" type="application/json">)(.*?)(</script>)', re.S)


def render(template: Path, model: dict) -> str:
    html = template.read_text(encoding="utf-8")
    if not DATA_TAG.search(html):
        raise SystemExit(f"{template.name} has no <script id=\"bt-data\"> block")
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    # `</script>` inside JSON string data would close the tag early.
    payload = payload.replace("</", "<\\/")
    return DATA_TAG.sub(
        lambda m: m.group(1) + payload + m.group(3), html, count=1)


def build() -> None:
    if not FIXTURE.exists():
        raise SystemExit(
            "db/fixtures/demo.json missing — run scripts/export_fixture.py")

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    model = build_model(data)

    (OUT_DIR / "app").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(
        render(SITE / "landing.html", model), encoding="utf-8")
    (OUT_DIR / "app" / "index.html").write_text(
        render(SITE / "app.html", model), encoding="utf-8")

    s = model["summary"]
    print("built web/index.html and web/app/index.html")
    print(f"  {len(model['proposed'])} waiting · {len(model['saved'])} saved · "
          f"{len(model['measuring'])} measuring · {len(model['judged'])} judged")
    print(f"  {s['verified']}V / {s['miss']}M of {s['judged']} judged "
          f"= {s['hitRate']}% · {s['dailyTxt']}/day")
    print(f"  {len(model['months'])} chart months "
          f"({sum(1 for m in model['months'] if m['projected'])} projected)")
    print(f"  {model['corpus']['evidenceRows']} evidence rows across "
          f"{len(model['finds'])} finds")


if __name__ == "__main__":
    build()
