"""Build the public site from live CockroachDB data.

    python scripts/export_fixture.py   # refresh demo.json from the cluster
    python scripts/build_web.py        # render web/

Output:

    web/index.html        the landing page      (site/landing.html + data)
    web/app/index.html    the dashboard         (site/app.html + data)
    web/signup/index.html the onboarding flow    (site/signup.html + data)

The templates in `site/` are the product's source. `Product Demo/` holds the
original mock, which predates this project and stays untouched as the provenance
artifact — it is design reference, not a build input.

Why the build remains even with a live workflow endpoint: it gives the product
an immediate, failure-tolerant first paint and keeps the public landing claims
grounded in a reproducible CockroachDB snapshot. Memory Engine then overlays
current decisions, runs, Maker artifacts, and Meter verdicts from the read-only
API while an operator is looking at it.

Every money value crossing into the page is integer cents, formatted once here.
The page never does arithmetic on money.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend" / "src"))
from brasstacks.agents.coster import cost_for_display  # noqa: E402
from brasstacks.analyst_trace import parse_analyst_trace  # noqa: E402
from brasstacks.ask_trace import parse_ask_trace  # noqa: E402
from brasstacks.artifact_usage import artifact_use_context  # noqa: E402
from brasstacks.finds import (  # noqa: E402
    SUMMARY_MAX_CHARS,
    clean_owner_copy,
    feed_brief_for_display,
    owner_card_summary,
    reported_outcome_view,
)
from brasstacks.repository import WITHHELD_STATUS  # noqa: E402

SITE = REPO / "site"
#: Anything here is copied to web/assets/ verbatim. The admin console's backdrop
#: lives here: a painted scene reaches a depth — haze, bloom, a real map — that
#: hand-written vector chrome does not, so the build takes an image over its own
#: drawn scene when one is supplied.
ASSETS = SITE / "assets"

#: Preferred first. The two modern formats are a fraction of the size at this
#: resolution, and the backdrop is the largest thing the page loads.
BACKDROP_FORMATS = (".avif", ".webp", ".png", ".jpg", ".jpeg")
BACKDROP_NAME = "hud-backdrop"
FIXTURE = REPO / "db" / "fixtures" / "demo.json"
#: Stand-in data for the parts of the machinery that do not record themselves
#: yet. Deliberately a separate file: `export_fixture.py` rebuilds demo.json
#: from scratch on every run, so anything hand-added there is deleted the next
#: time someone exports, and demo.json's own comment claims the whole file came
#: from the live cluster.
PLACEHOLDERS = REPO / "db" / "fixtures" / "admin_placeholder.json"
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

#: Rows the Analyst pulls back per hypothesis query, from
#: `analyst.DEFAULT_PER_QUERY_LIMIT`. Kept in sync by a test, like the queries.
PER_QUERY_LIMIT = 6

#: The most observations one night's retrieval can put in front of the model:
#: every query, filled. A product of two constants, not a measurement — the page
#: has to say so, or a judge reads 36 as something that was counted.
RAW_HIT_CEILING = len(ANALYST_QUERIES) * PER_QUERY_LIMIT

#: The prefix `ask._trail_note` puts on each stored SQL line, from
#: `ask.TRAIL_PREFIX`. The API strips it before the browser ever sees it.
TRAIL_PREFIX = "sql> "

#: What `ask._trail_note` writes when the model answered without touching the
#: cluster. Worth showing rather than hiding: it is the one case where the
#: answer did not come from the memory layer at all.
NO_TOOL_CALLS = "answered with no tool calls"

#: The question length the Ask handler accepts, from
#: `handlers.ask.MAX_QUESTION_CHARS`, so the page's input agrees with the API.
MAX_QUESTION_CHARS = 500

#: The tables the Ask agent is told about up front, from `ask.SCHEMA_HINT`. On
#: the page this is the disclosure of what a question can reach — the service
#: account is read-only, and this is the whole surface.
ASK_TABLES = [
    "business", "business_fact", "observation", "find", "find_evidence",
    "ledger_entry", "artifact", "agent_run",
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


def card_summary(find: dict) -> str:
    """One sentence for the card face.

    The rationale is the agent's full argument and runs to a paragraph. Putting
    that on the card is what made the first real deck unreadable — CLAUDE.md
    records it as the reason main was reverted rather than the card retuned.
    The argument is still one tap away; it is just not the first thing read.
    """
    text = " ".join(clean_owner_copy(find.get("summary") or "").split())
    if not text:
        rationale = " ".join(clean_owner_copy(find.get("rationale") or "").split())
        first, _, _ = rationale.partition(". ")
        text = first.strip()
        if text and not text.endswith("."):
            text += "."
    return owner_card_summary(text)


def _date_value(value: object) -> date | None:
    """Coerce fixture timestamps to dates without depending on local time."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def verification_days(find: dict, default: int = 14) -> int:
    """Return the promised window, stable across build and live refresh."""
    verify_after = _date_value(find.get("verify_after"))
    created_at = _date_value(find.get("created_at"))
    if verify_after is None or created_at is None:
        return default
    return max(1, (verify_after - created_at).days)


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


def when(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%-d %b") if False else \
        datetime.fromisoformat(value).strftime("%d %b").lstrip("0")


def artifacts(row: dict) -> list[dict]:
    """Return Maker artifacts using the same contract as the live workflow API.

    ``stored`` describes only the optional S3 copy. The durable CockroachDB row
    remains the artifact even when an upload fails. Presentation metadata is
    carried into the resilient first paint so the owner sees the same intended
    destination before and after the live workflow overlay arrives.
    """
    out = []
    for a in row.get("artifacts") or []:
        bucket, key = a.get("s3_bucket"), a.get("s3_key")
        artifact_id = str(a.get("id") or "")
        metadata = a.get("metadata") if isinstance(a.get("metadata"), dict) else {}
        artifact_type = str(
            metadata.get("artifact_type") or a.get("kind") or "general_draft"
        )
        out.append({
            "id": artifact_id[:8],
            "databaseId": artifact_id or None,
            "kind": a.get("kind") or "draft",
            "title": " ".join((a.get("title") or "").split()),
            "preview": " ".join((a.get("preview") or "").split()),
            "body": a.get("body") or None,
            "summary": a.get("summary") or None,
            "ownerAction": a.get("owner_action") or None,
            "reviewState": a.get("review_state") or "ready_for_review",
            "metadata": metadata,
            "artifactType": artifact_type,
            "useContext": artifact_use_context(
                artifact_type, stored=metadata.get("use_context")
            ),
            "ownerQuestions": list(metadata.get("owner_questions") or []),
            "sections": list(metadata.get("sections") or []),
            "revision": int(a.get("revision") or 1),
            "parentArtifactId": a.get("parent_artifact_id"),
            "decisionCycle": int(a.get("decision_cycle") or 1),
            "supersededAt": a.get("superseded_at"),
            "createdAt": a.get("created_at"),
            "stored": bool(bucket and key),
            "location": f"s3://{bucket}/{key}" if bucket and key else None,
            "taskId": str(a.get("task_id") or "") or None,
            "idempotencyKey": a.get("idempotency_key"),
        })
    return out


def timeline(finds: list[dict]) -> list[dict]:
    """The operational history, folded out of stamps the cluster actually stored.

    Two event kinds, one column each: the Analyst proposed a find
    (`created_at`), the Meter judged it (`measured_at`). Nothing is interpolated
    between them, so a gap is a stretch where nothing was recorded and the page
    is free to say so.

    `verify_after` is deliberately not here. It is a commitment rather than
    something that happened, and most of the seeded finds are due after the
    export was taken — admitting it as an event put dates in the future at the
    top of a log of the past. It stays on the find, where the page reads it as
    one end of a span.

    This exists because the run list alone under-tells the story: the seeded
    cluster holds a single agent_run row, while the finds it produced span
    twelve proposal dates and eight measurement dates. Those are the same
    nights; only one of them was written down as a run.
    """
    events = []
    for f in finds:
        # A withheld find has a `created_at` like any other row, but it was
        # never put to the owner — a quality gate declined to show it, with the
        # reason in `find.withheld_reason`. Logging it as "proposed" would put a
        # proposal that never happened into the operational history, which is
        # the same shape of untruth as labelling a modelled figure Actual. It
        # has no measured event either, because nothing was ever acted on.
        if f["status"] == WITHHELD_STATUS:
            continue
        if f["createdAt"]:
            events.append({"kind": "proposed", "at": f["createdAt"], "findId": f["id"],
                           "title": f["shortTitle"], "amount": f["predictedDailyTxt"]})
        if f["measuredAt"] and f["verdict"]:
            events.append({"kind": "measured", "at": f["measuredAt"], "findId": f["id"],
                           "title": f["shortTitle"], "amount": f["actualDailyTxt"],
                           "verdict": f["verdict"]})
    # Newest first, and stable within a day so two events sharing a date keep
    # the order the finds came in rather than shuffling between builds.
    return sorted(events, key=lambda e: e["at"], reverse=True)


def parse_trail(note: str | None) -> list[str]:
    """Recover the SQL the Ask agent ran, from the note its run stored.

    The inverse of `ask._trail_note`, and pinned to it by a test rather than to
    a copy of its format.
    """
    if not note:
        return []
    return [line[len(TRAIL_PREFIX):] for line in note.splitlines()
            if line.startswith(TRAIL_PREFIX)]


def ask_sessions(runs: list[dict]) -> list[dict]:
    """Every Ask run, including its durable memory and token receipt."""
    out = []
    for r in runs:
        if r.get("agent") != "ask":
            continue
        trail = parse_trail(r.get("note"))
        trace = parse_ask_trace(r.get("note"))
        input_tokens = r.get("input_tokens")
        output_tokens = r.get("output_tokens")
        run_id = str(r.get("id") or "")
        out.append({
            "runId": run_id[:8],
            "databaseRunId": run_id or None,
            "askedAt": r.get("started_at"),
            "seconds": run_seconds(r),
            "status": r.get("status"),
            "question": trace.get("question") if trace else None,
            "answer": trace.get("answer") if trace else None,
            "findId": trace.get("find_id") if trace else None,
            "recentMessagesUsed": len(trace.get("recent_message_ids", [])) if trace else None,
            "relevantMessagesUsed": len(trace.get("relevant_message_ids", [])) if trace else None,
            "storedMessages": len(trace.get("stored_message_ids", [])) if trace else None,
            "queriedTheCluster": bool(trace.get("queried_the_cluster")) if trace else bool(trail),
            "traceSource": "cluster" if trace else "legacy",
            "trail": trail,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": (int(input_tokens or 0) + int(output_tokens or 0))
                if input_tokens is not None or output_tokens is not None else None,
        })
    return out


def analyst_run_receipt(runs: list[dict]) -> dict | None:
    """Newest Analyst run, including the structured retrieval and token receipt."""
    run = next((row for row in runs if row.get("agent") == "analyst"), None)
    if run is None:
        return None

    run_id = str(run.get("id") or "")
    trace = parse_analyst_trace(run.get("note"))
    query_hits = list(trace.get("query_hits") or []) if trace else []
    if len(query_hits) < len(ANALYST_QUERIES):
        query_hits.extend([None] * (len(ANALYST_QUERIES) - len(query_hits)))
    query_hits = query_hits[:len(ANALYST_QUERIES)]
    input_tokens = run.get("input_tokens")
    output_tokens = run.get("output_tokens")
    return {
        "runId": run_id or None,
        "shortRunId": run_id[:8] if run_id else None,
        "status": run.get("status"),
        "startedAt": run.get("started_at"),
        "finishedAt": run.get("finished_at"),
        "seconds": run_seconds(run),
        "modelId": run.get("model_id"),
        "error": run.get("error"),
        "note": str(run.get("note") or "").splitlines()[0] if run.get("note") else "",
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": (int(input_tokens) + int(output_tokens))
            if input_tokens is not None and output_tokens is not None else None,
        "tokensSource": "cluster" if input_tokens is not None else "unrecorded",
        "traceSource": "cluster" if trace else "legacy",
        "queryHits": query_hits,
        "rawHits": trace.get("raw_hits") if trace else None,
        "uniqueHits": trace.get("unique_hits") if trace else None,
        "citedHits": trace.get("cited_hits") if trace else None,
        "findId": trace.get("find_id") if trace else None,
        "queries": trace.get("queries") if trace else None,
        "perQueryLimit": trace.get("per_query_limit") if trace else None,
        "ownerMemoryIds": trace.get("owner_memory_ids") if trace else None,
    }


def retrieval_funnel(finds: list[dict]) -> dict:
    """How one night's retrieval narrowed down, stage by stage.

    Each stage carries where its number came from, because they genuinely
    differ: the first two are arithmetic on constants, the last is a row count,
    and the middle one was never written down. Labelling them all the same way
    would be the page's biggest lie — it is the difference between "the Analyst
    considered 36 observations" and "the Analyst could have considered at most
    36 observations".
    """
    return {
        "source": "derived",
        "queries": ANALYST_QUERIES,
        "perQueryLimit": PER_QUERY_LIMIT,
        "byFind": {f["id"]: [
            {"key": "queries", "label": "hypothesis queries",
             "n": len(ANALYST_QUERIES), "source": "code"},
            {"key": "raw", "label": f"rows retrieved, ceiling",
             "n": RAW_HIT_CEILING, "source": "code"},
            # The Analyst records this in its own run note ("N retrieved; …"),
            # so it becomes real the moment an analyst run exists in the export.
            {"key": "deduped", "label": "unique after dedup",
             "n": None, "source": "unrecorded"},
            {"key": "cited", "label": "cited as evidence",
             "n": f["evidenceCount"], "source": "cluster"},
        ] for f in finds},
    }


# ---------------------------------------------------------------- view model


def build_model(data: dict) -> dict:
    business = data["business"]
    owner = data.get("owner") or {}
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
        run_database_id = str(f.get("run_id") or "")
        title = " ".join((f.get("title") or "").split())
        move = " ".join((f.get("move") or "").split())
        summary_text = card_summary(f)
        feed_brief = feed_brief_for_display(
            f.get("feed_brief"),
            title=title,
            summary=summary_text,
            move=move,
            verify_after_days=verification_days(f),
            evidence_kinds=[str(item.get("kind") or "") for item in evidence],
        )
        finds.append({
            "id": f["id"][:8],
            "databaseId": f["id"],
            "runId": run_database_id[:8] if run_database_id else None,
            "runDatabaseId": run_database_id or None,
            "origin": "agent_run" if run_database_id else "historical_import",
            "emoji": f["emoji"] or "💡",
            "title": title,
            "shortTitle": short_title(title),
            "tinyTitle": short_title(title, 20),
            "move": move,
            "bullets": bullets(move),
            # The card face. Falls back to the rationale's first sentence for
            # rows written before the Analyst produced summaries, so an older
            # export still renders rather than showing an empty card.
            "summary": summary_text,
            "rationale": " ".join((f["rationale"] or "").split()),
            "feedBrief": feed_brief,
            # Beside the revenue figure, never netted against it. See
            # workflow_snapshot.build_workspace — both paths read the same
            # helper so the first paint and the live refresh cannot disagree.
            "costEstimate": cost_for_display(
                f.get("cost_estimate"),
                predicted_daily_cents=predicted,
                verify_after_days=verification_days(f),
            ),
            "predictedDaily": predicted,
            "predictedDailyTxt": money(predicted),
            "predictedMonthlyTxt": money(predicted * PER_MONTH),
            "predictedMonthlyShort": short_money(predicted * PER_MONTH),
            "actualDaily": int(actual) if actual is not None else None,
            "actualDailyTxt": money(int(actual)) if actual is not None else None,
            # What the owner reported, if anything. Same helper as
            # workflow_snapshot, so the first paint and the live refresh cannot
            # show a different figure for the same find.
            "reportedOutcome": reported_outcome_view(
                f.get("reported_outcome"), money=money),
            "confidence": round((f["confidence"] or 0) * 100),
            "status": f["status"],
            "verdict": f["verdict"],
            "method": f["method"],
            "note": f["note"],
            "measuredAt": f["measured_at"],
            "verifyAfter": f["verify_after"],
            # The three stamps the admin timeline is folded out of. They were
            # already in the export and dropped here, which is why one night of
            # agent_run rows looked like the whole history.
            "createdAt": f.get("created_at"),
            "decidedAt": f.get("decided_at"),
            "periodStart": f.get("period_start"),
            "periodEnd": f.get("period_end"),
            "artifacts": artifacts(f),
            "evidenceCount": len(evidence),
            "topSimilarity": round(evidence[0]["similarity"], 3) if evidence else None,
            "evidence": [{
                "id": str(e.get("observation_id") or "")[:8] or None,
                "observationId": str(e.get("observation_id") or "") or None,
                "rank": int(e.get("rank") or 0),
                "content": " ".join(e["content"].split()),
                "kind": e["kind"],
                "source": SOURCE_LABEL.get(e["kind"], e["kind"]),
                "sourceName": e.get("source_name"),
                "subject": e.get("subject"),
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
    latest_analyst_run = analyst_run_receipt(data.get("runs", []))

    hit = summary["hit_rate"]
    decision_endpoint = (os.environ.get("DECISION_API_ENDPOINT") or "").rstrip("/")
    workflow_endpoint = (os.environ.get("WORKFLOW_API_ENDPOINT") or "").rstrip("/")
    ask_endpoint = (os.environ.get("ASK_API_ENDPOINT") or "").rstrip("/")
    onboarding_endpoint = (os.environ.get("ONBOARDING_API_ENDPOINT") or "").rstrip("/")
    profile_endpoint = (os.environ.get("PROFILE_API_ENDPOINT") or "").rstrip("/")
    login_endpoint = (os.environ.get("LOGIN_API_ENDPOINT") or "").rstrip("/")
    if not login_endpoint and decision_endpoint:
        login_endpoint = f"{decision_endpoint}/login"
    register_endpoint = (os.environ.get("REGISTER_API_ENDPOINT") or "").rstrip("/")
    if not register_endpoint and decision_endpoint:
        register_endpoint = f"{decision_endpoint}/register"
    run_endpoint = (os.environ.get("RUN_API_ENDPOINT") or "").rstrip("/")
    if not run_endpoint and decision_endpoint:
        run_endpoint = f"{decision_endpoint}/run"
    admin_endpoint = (os.environ.get("ADMIN_API_ENDPOINT") or "").rstrip("/")
    if not admin_endpoint and decision_endpoint:
        admin_endpoint = f"{decision_endpoint}/admin/workspaces"
    if not workflow_endpoint and decision_endpoint:
        workflow_endpoint = f"{decision_endpoint}/workflow"
    if not ask_endpoint and decision_endpoint:
        ask_endpoint = f"{decision_endpoint}/ask"
    if not profile_endpoint and decision_endpoint:
        profile_endpoint = f"{decision_endpoint}/profile"

    google_start = (os.environ.get("GOOGLE_START_API_ENDPOINT") or "").rstrip("/")
    google_complete = (os.environ.get("GOOGLE_COMPLETE_API_ENDPOINT") or "").rstrip("/")
    if decision_endpoint:
        google_start = google_start or f"{decision_endpoint}/auth/google/start"
        google_complete = google_complete or f"{decision_endpoint}/auth/google/complete"
    # One switch for the whole feature. Nothing at build time can tell whether
    # an OAuth client actually exists in the Google console, and a Sign in with
    # Google button that 404s is worse than no button at all — so the button is
    # drawn only when someone has said, explicitly, that the client is there.
    if str(os.environ.get("GOOGLE_OAUTH_ENABLED") or "").strip().lower() not in {
            "1", "true", "yes", "on"}:
        google_start = google_complete = ""

    # The scan shares the onboarding Lambda, so its URL is always the
    # onboarding URL plus a segment. Derived rather than configured so there is
    # one fewer env var to forget in the deploy workflow.
    menu_scan_endpoint = (os.environ.get("MENU_SCAN_API_ENDPOINT") or "").rstrip("/")
    if not menu_scan_endpoint and onboarding_endpoint:
        menu_scan_endpoint = f"{onboarding_endpoint}/menu-scan"
    if not menu_scan_endpoint and decision_endpoint:
        menu_scan_endpoint = f"{decision_endpoint}/onboarding/menu-scan"

    return {
        "api": {
            "menuScanEndpoint": menu_scan_endpoint or None,
            "decisionEndpoint": decision_endpoint or None,
            "workflowEndpoint": workflow_endpoint or None,
            "askEndpoint": ask_endpoint or None,
            "onboardingEndpoint": onboarding_endpoint or None,
            "profileEndpoint": profile_endpoint or None,
            "loginEndpoint": login_endpoint or None,
            "registerEndpoint": register_endpoint or None,
            "runEndpoint": run_endpoint or None,
            "adminEndpoint": admin_endpoint or None,
            "googleStartEndpoint": google_start or None,
            "googleCompleteEndpoint": google_complete or None,
        },
        "owner": {
            "id": owner.get("id"),
            "username": owner.get("username"),
            "name": owner.get("display_name") or owner.get("username") or "Business owner",
            "email": owner.get("email"),
            "lastLoginAt": owner.get("last_login_at"),
        },
        "business": {
            "id": business.get("id"),
            "name": business["name"],
            "category": business.get("category"),
            "city": business.get("city"),
            "region": business.get("region"),
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
            "conversationMessages": int(corpus.get("conversation_messages") or 0),
            "evidenceRows": sum(f["evidenceCount"] for f in finds),
            "earliest": corpus.get("earliest"),
            "latest": corpus.get("latest"),
        },
        # agent_run rows, for the operator view. The seeded corpus carries only
        # the run that built it — the admin screen says so rather than padding
        # the list out to look busier than the cluster is.
        "runs": [{
            "id": r["id"][:8],
            "databaseId": r["id"],
            "agent": r["agent"],
            "status": r["status"],
            "startedAt": r["started_at"],
            "finishedAt": r.get("finished_at"),
            "seconds": run_seconds(r),
            "note": r.get("note") or "",
            "modelId": r.get("model_id"),
            "error": r.get("error"),
            # New Analyst and Maker runs write provider usage here. Historical
            # fixture rows may still be null; null means unavailable, never a
            # zero-token run.
            "inputTokens": r.get("input_tokens"),
            "outputTokens": r.get("output_tokens"),
            "tokensSource": ("cluster" if r.get("input_tokens") is not None
                             else "unrecorded"),
            "analystTrace": parse_analyst_trace(r.get("note"))
                if r.get("agent") == "analyst" else None,
        } for r in data.get("runs", [])],
        "statusLine": status_line,
        "finds": finds,
        "timeline": timeline(finds),
        "artifactCount": sum(len(f["artifacts"]) for f in finds),
        "retrieval": retrieval_funnel(finds),
        "analyst": {
            "source": "cluster",
            "queryCount": len(ANALYST_QUERIES),
            "perQueryLimit": PER_QUERY_LIMIT,
            "rawHitCeiling": RAW_HIT_CEILING,
            "queries": [
                {
                    "index": index + 1,
                    "text": query,
                    "hits": (latest_analyst_run or {}).get("queryHits", [None] * len(ANALYST_QUERIES))[index],
                }
                for index, query in enumerate(ANALYST_QUERIES)
            ],
            "latestRun": latest_analyst_run,
        },
        "ask": {
            "source": "cluster",
            "maxQuestionChars": MAX_QUESTION_CHARS,
            "tables": ASK_TABLES,
            "sessions": ask_sessions(data.get("runs", [])),
        },
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
        "backdrop": find_backdrop(ASSETS),
        "generated": data.get("_generated"),
        "_by_id": list(by_id),
    }


# ------------------------------------------------------------- placeholders
#
# The operator view has to show how the machinery works before every part of
# that machinery records itself. Some historical demo rows predate structured
# run receipts, while Ask examples are not durable business facts. Clearly
# labelled stand-in explanatory content is legitimate; anything that could be
# mistaken for measured cluster data is not.
#
# So placeholders arrive by one route, after `build_model` has done its work,
# and that route refuses anything it cannot police. `build_model` stays a pure
# function of the cluster export and never learns this concept exists.


class PlaceholderError(RuntimeError):
    """A placeholder tried to become indistinguishable from a measurement."""


#: Keys the honesty tests guard and the page labels as cluster rows. A
#: placeholder that could write here could put an invented month on the growth
#: chart or an extra night in the run list, which is exactly what
#: `test_admin_run_list_is_not_padded` exists to stop.
GUARDED_KEYS = frozenset({
    "runs", "finds", "summary", "months", "ratings", "kinds", "corpus",
    "proposed", "saved", "measuring", "earning", "judged",
})

#: Placeholders may fake operational figures and never financial ones. Every
#: honesty rule in CLAUDE.md is a claim about the owner's money, so drawing the
#: line at money makes those rules hold even if a panel ships without its badge.
MONEY_KEY = re.compile(r"(cents|daily|monthly|txt)$", re.I)

#: And the same rule for prose. A stand-in answer reading "the patio night
#: brought in $19/day" states an earning as plainly as a column would; being
#: inside a sentence does not make it less of a claim about the owner's money.
#: SQL text is exempt — `predicted_daily_cents` in a SELECT is a column name the
#: agent really queried, not a figure the page is asserting.
MONEY_PROSE = re.compile(r"[$£€]\s*\d")


def _reject_money(node, block: str, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if MONEY_KEY.search(key):
                raise PlaceholderError(
                    f"placeholder block {block!r} carries money at "
                    f"{path}{key!r} — placeholders may state how the machinery "
                    f"ran, never what it earned")
            _reject_money(value, block, f"{path}{key}.")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _reject_money(value, block, f"{path}{i}.")
    elif isinstance(node, str) and MONEY_PROSE.search(node):
        raise PlaceholderError(
            f"placeholder block {block!r} quotes money in prose at "
            f"{path.rstrip('.')!r} — placeholders may state how the machinery "
            f"ran, never what it earned")


def merge_placeholders(model: dict, placeholders: dict) -> dict:
    """Fold declared stand-in blocks into a built model.

    Each block states its own `source`, inline, rather than living under a
    `placeholder` bucket — so wiring the real thing later means filling the
    block and flipping one word, with no key moving and no renderer rewritten.
    That is the whole point of the envelope.
    """
    blocks = {k: v for k, v in placeholders.items() if not k.startswith("_")}
    for name, block in blocks.items():
        if name in GUARDED_KEYS:
            raise PlaceholderError(
                f"{name!r} is a guarded key: it holds rows the page presents as "
                f"the cluster's own, so a placeholder may not write it")
        if not isinstance(block, dict) or "source" not in block:
            raise PlaceholderError(
                f"placeholder block {name!r} declares no source; a block that "
                f"does not say where it came from cannot be marked on the page")
        _reject_money(block, name)
        # Shallow, so a block that is partly real keeps its real fields: `ask`
        # already carries the question limit and table list the handler
        # publishes, and only its sessions are a stand-in. The declared source
        # still wins, so a panel is badged by its weakest part.
        base = model.get(name)
        model[name] = {**base, **block} if isinstance(base, dict) else block

    model["provenance"] = {
        "fixture": FIXTURE.name,
        "blocks": {k: v["source"] for k, v in model.items()
                   if isinstance(v, dict) and "source" in v},
    }
    return model


def find_backdrop(assets: Path) -> str | None:
    """The console backdrop image, as the app page would reference it.

    Returns a path relative to `web/app/`, which is where the only page that
    uses it is written — hence the `../`. None when nothing has been supplied,
    which is the state of a fresh clone and is not an error: the admin view
    falls back to the scene it draws itself.
    """
    for suffix in BACKDROP_FORMATS:
        candidate = assets / f"{BACKDROP_NAME}{suffix}"
        if candidate.is_file():
            return f"../assets/{candidate.name}"
    return None


def copy_assets() -> int:
    """Mirror site/assets into web/assets, replacing whatever was there.

    Replaced rather than merged so a renamed or deleted source file does not
    linger in the output and keep being served.
    """
    if not ASSETS.is_dir():
        return 0
    out = OUT_DIR / "assets"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(ASSETS, out,
                    ignore=shutil.ignore_patterns("*.md", ".gitkeep"))
    return sum(1 for path in out.rglob("*") if path.is_file())


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
    if PLACEHOLDERS.exists():
        model = merge_placeholders(
            model, json.loads(PLACEHOLDERS.read_text(encoding="utf-8")))

    (OUT_DIR / "app").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "signup").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "login").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "register").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "auth" / "complete").mkdir(parents=True, exist_ok=True)
    assets = copy_assets()
    (OUT_DIR / "index.html").write_text(
        render(SITE / "landing.html", model), encoding="utf-8")
    (OUT_DIR / "app" / "index.html").write_text(
        render(SITE / "app.html", model), encoding="utf-8")
    (OUT_DIR / "signup" / "index.html").write_text(
        render(SITE / "signup.html", model), encoding="utf-8")
    (OUT_DIR / "login" / "index.html").write_text(
        render(SITE / "login.html", model), encoding="utf-8")
    (OUT_DIR / "register" / "index.html").write_text(
        render(SITE / "register.html", model), encoding="utf-8")
    # Where Google's redirect lands. Built unconditionally even when the button
    # is off: a live OAuth client whose callback URL 404s is a worse failure
    # than an unreachable page nobody links to.
    (OUT_DIR / "auth" / "complete" / "index.html").write_text(
        render(SITE / "auth-complete.html", model), encoding="utf-8")

    s = model["summary"]
    print("built web/{index,register/index,login/index,signup/index,"
          "auth/complete/index,app/index}.html")
    print(f"  {len(model['proposed'])} waiting · {len(model['saved'])} saved · "
          f"{len(model['measuring'])} measuring · {len(model['judged'])} judged")
    print(f"  {s['verified']}V / {s['miss']}M of {s['judged']} judged "
          f"= {s['hitRate']}% · {s['dailyTxt']}/day")
    print(f"  {len(model['months'])} chart months "
          f"({sum(1 for m in model['months'] if m['projected'])} projected)")
    print(f"  {model['corpus']['evidenceRows']} evidence rows across "
          f"{len(model['finds'])} finds")
    print(f"  {assets} asset file(s) · console backdrop: "
          f"{model['backdrop'] or 'none — drawing the scene instead'}")


if __name__ == "__main__":
    build()
