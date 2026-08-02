"""Live, read-only workflow snapshots for the Memory Engine.

The static site build remains the resilient first paint. This module provides the
small runtime overlay that removes its only important limitation: operator state
no longer waits for a new export. It reads the current CockroachDB rows and shapes
them into the same owner-workspace contract the browser already understands.

The endpoint returns workflow state, receipts, and the evidence attached to finds.
It never returns observation embeddings or the full memory corpus. That boundary
keeps the response small and preserves the product's token-efficient pattern:
search durable memory in CockroachDB, then move only the relevant evidence across
the network and into model context.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from brasstacks.agents.analyst import ANALYST_QUERIES, DEFAULT_PER_QUERY_LIMIT
from brasstacks.agents.ask import TRAIL_PREFIX
from brasstacks.analyst_trace import parse_analyst_trace

PER_MONTH = 30
RAW_HIT_CEILING = len(ANALYST_QUERIES) * DEFAULT_PER_QUERY_LIMIT
MAX_QUESTION_CHARS = 500
ASK_TABLES = (
    "business", "observation", "find", "find_evidence",
    "ledger_entry", "artifact", "agent_run",
)

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

_RETRIEVED_PATTERNS = (
    re.compile(r"\b(\d+)\s+retrieved\b", re.I),
    re.compile(r"\b(\d+)\s+observations?\s+retrieved\b", re.I),
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def money(cents: int) -> str:
    dollars = cents / 100
    return f"${dollars:,.0f}" if cents % 100 == 0 else f"${dollars:,.2f}"


def short_money(cents: int) -> str:
    dollars = cents / 100
    return f"${dollars / 1000:.1f}k" if dollars >= 1000 else f"${dollars:,.0f}"


def clamp(text: str | None, limit: int) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def short_title(title: str, limit: int = 34) -> str:
    value = re.split(r"\s*[:(]", " ".join(title.split()), maxsplit=1)[0].strip()
    return clamp(value, limit)


def bullets(move: str | None) -> list[str]:
    text = " ".join((move or "").split())
    if not text:
        return []
    parts = re.split(r"\s*\((?:\d+|[a-e])\)\s*", text)
    parts = [part for part in (part.strip() for part in parts) if part]
    if len(parts) > 1:
        return [part.rstrip(" ;,") for part in parts]
    return [
        sentence.strip().rstrip(";")
        for sentence in re.split(r"(?<=[.;])\s+", text)
        if sentence.strip()
    ]


def when(value: Any) -> str:
    raw = _iso(value)
    if not raw:
        return ""
    parsed = datetime.fromisoformat(raw)
    return parsed.strftime("%d %b").lstrip("0")


def run_seconds(row: dict[str, Any]) -> int | None:
    started, finished = _iso(row.get("started_at")), _iso(row.get("finished_at"))
    if not started or not finished:
        return None
    return round((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds())


def parse_retrieved_count(note: str | None) -> int | None:
    for pattern in _RETRIEVED_PATTERNS:
        match = pattern.search(note or "")
        if match:
            return int(match.group(1))
    return None


def _artifacts(row: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for artifact in row.get("artifacts") or []:
        bucket, key = artifact.get("s3_bucket"), artifact.get("s3_key")
        artifact_id = str(artifact.get("id") or "")
        result.append({
            "id": artifact_id[:8],
            "databaseId": artifact_id or None,
            "kind": artifact.get("kind") or "draft",
            "title": " ".join((artifact.get("title") or "").split()),
            "preview": " ".join((artifact.get("preview") or "").split()),
            "createdAt": _iso(artifact.get("created_at")),
            "stored": bool(bucket and key),
            "location": f"s3://{bucket}/{key}" if bucket and key else None,
        })
    return result


def _parse_trail(note: str | None) -> list[str]:
    if not note:
        return []
    return [
        line[len(TRAIL_PREFIX):]
        for line in note.splitlines()
        if line.startswith(TRAIL_PREFIX)
    ]


def _ask_sessions(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sessions = []
    for run in runs:
        if run.get("agent") != "ask":
            continue
        trail = _parse_trail(run.get("note"))
        run_id = str(run.get("id") or "")
        sessions.append({
            "runId": run_id[:8],
            "askedAt": _iso(run.get("started_at")),
            "seconds": run_seconds(run),
            "status": run.get("status"),
            "queriedTheCluster": bool(trail),
            "trail": trail,
        })
    return sessions


def _deduped_by_find(finds: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, int]:
    """Match an Analyst run receipt to the find it proposed.

    New runs carry an exact find id in the structured trace. Older runs are
    matched by the human note so historical data remains readable.
    """
    result: dict[str, int] = {}
    analyst_runs = [run for run in runs if run.get("agent") == "analyst"]
    for run in analyst_runs:
        note = str(run.get("note") or "")
        trace = parse_analyst_trace(note)
        if trace and trace.get("find_id"):
            result[str(trace["find_id"])] = int(trace["unique_hits"])
            continue

        count = parse_retrieved_count(note)
        if count is None:
            continue
        for found in finds:
            title = str(found.get("title") or "")
            if title and title in note:
                result[str(found["id"])] = count
                break
    return result


def _analyst_run_receipt(raw_runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest Analyst run as an operator-facing trace receipt."""
    run = next((row for row in raw_runs if row.get("agent") == "analyst"), None)
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
        "startedAt": _iso(run.get("started_at")),
        "finishedAt": _iso(run.get("finished_at")),
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
        "uniqueHits": trace.get("unique_hits") if trace else parse_retrieved_count(run.get("note")),
        "citedHits": trace.get("cited_hits") if trace else None,
        "findId": trace.get("find_id") if trace else None,
        "queries": trace.get("queries") if trace else None,
        "perQueryLimit": trace.get("per_query_limit") if trace else None,
    }


#: Mirrors brasstacks.finds.SUMMARY_MAX_CHARS and build_web.card_summary. The
#: card has the same amount of room whichever path filled it.
SUMMARY_MAX_CHARS = 180


def card_summary(raw_find: dict) -> str:
    """One sentence for the card face, from the model or from the rationale.

    A find written before the Analyst produced summaries still has to render, so
    the rationale's first sentence is the fallback — done once here rather than
    in the page.
    """
    text = " ".join((raw_find.get("summary") or "").split())
    if not text:
        rationale = " ".join((raw_find.get("rationale") or "").split())
        first, _, _ = rationale.partition(". ")
        text = first.strip()
        if text and not text.endswith("."):
            text += "."
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    cut = text[:SUMMARY_MAX_CHARS - 1]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(",;:") + "…"


def build_workspace(data: dict[str, Any]) -> dict[str, Any]:
    """Shape one business's current rows for the operator UI.

    This function is deliberately pure. The static first paint and the live
    endpoint can be compared in tests without a database or network.
    """
    business = data["business"]
    raw_summary = data.get("summary") or {}
    raw_corpus = data.get("corpus") or {}
    raw_runs = list(data.get("runs") or [])

    verified = _int(raw_summary.get("verified"))
    estimated = _int(raw_summary.get("estimated"))
    miss = _int(raw_summary.get("miss"))
    judged = _int(raw_summary.get("judged"), verified + miss)
    hit_rate = raw_summary.get("hit_rate")
    if hit_rate is None and judged:
        hit_rate = verified / judged
    daily = _int(raw_summary.get("verified_daily_cents"))

    goal_monthly = _int(business.get("goal_monthly_cents"), 800000)
    monthly_now = daily * PER_MONTH

    finds: list[dict[str, Any]] = []
    for raw_find in data.get("finds") or []:
        evidence = sorted(raw_find.get("evidence") or [], key=lambda item: _int(item.get("rank")))
        predicted = _int(raw_find.get("predicted_daily_cents"))
        actual_raw = raw_find.get("actual_daily_cents")
        actual = _int(actual_raw) if actual_raw is not None else None
        find_id = str(raw_find.get("id") or "")
        run_database_id = str(raw_find.get("run_id") or "")
        finds.append({
            "id": find_id[:8],
            "databaseId": find_id,
            "runId": run_database_id[:8] if run_database_id else None,
            "runDatabaseId": run_database_id or None,
            "origin": "agent_run" if run_database_id else "historical_import",
            "emoji": raw_find.get("emoji") or "💡",
            "title": " ".join((raw_find.get("title") or "").split()),
            "shortTitle": short_title(raw_find.get("title") or ""),
            "tinyTitle": short_title(raw_find.get("title") or "", 20),
            "move": " ".join((raw_find.get("move") or "").split()),
            "bullets": bullets(raw_find.get("move")),
            # The card face. Absent on rows written before the field existed,
            # and the live read did not select it at all until 2026-08-02 —
            # which is why the board fell back to the full rationale and the
            # deck overflowed again.
            "summary": card_summary(raw_find),
            "rationale": " ".join((raw_find.get("rationale") or "").split()),
            "predictedDaily": predicted,
            "predictedDailyTxt": money(predicted),
            "predictedMonthlyTxt": money(predicted * PER_MONTH),
            "predictedMonthlyShort": short_money(predicted * PER_MONTH),
            "actualDaily": actual,
            "actualDailyTxt": money(actual) if actual is not None else None,
            "confidence": round(_float(raw_find.get("confidence")) * 100),
            "status": str(raw_find.get("status") or "proposed"),
            "verdict": raw_find.get("verdict"),
            "method": raw_find.get("method"),
            "note": raw_find.get("note"),
            "measuredAt": _iso(raw_find.get("measured_at")),
            "verifyAfter": _iso(raw_find.get("verify_after")),
            "createdAt": _iso(raw_find.get("created_at")),
            "decidedAt": _iso(raw_find.get("decided_at")),
            "periodStart": _iso(raw_find.get("period_start")),
            "periodEnd": _iso(raw_find.get("period_end")),
            "artifacts": _artifacts(raw_find),
            "evidenceCount": len(evidence),
            "topSimilarity": round(_float(evidence[0].get("similarity")), 3) if evidence else None,
            "evidence": [{
                "id": str(item.get("observation_id") or "")[:8] or None,
                "observationId": str(item.get("observation_id") or "") or None,
                "rank": _int(item.get("rank")),
                "content": " ".join((item.get("content") or "").split()),
                "kind": item.get("kind"),
                "source": SOURCE_LABEL.get(item.get("kind"), item.get("kind") or "memory"),
                "sourceName": item.get("source_name"),
                "subject": item.get("subject"),
                "when": when(item.get("observed_at")),
                "similarity": round(_float(item.get("similarity")), 3),
            } for item in evidence],
        })

    proposed = sorted(
        (found for found in finds if found["verdict"] is None and found["status"] == "proposed"),
        key=lambda found: -found["predictedDaily"],
    )
    saved = [found for found in finds if found["verdict"] is None and found["status"] == "later"]
    measuring = [
        found for found in finds
        if found["verdict"] is None and found["status"] in ("accepted", "live")
    ]
    earning = [found for found in finds if found["verdict"] == "verified"]
    judged_finds = [found for found in finds if found["verdict"] is not None]

    runs = [{
        "id": str(run.get("id") or "")[:8],
        "databaseId": str(run.get("id") or "") or None,
        "agent": run.get("agent"),
        "status": run.get("status"),
        "startedAt": _iso(run.get("started_at")),
        "finishedAt": _iso(run.get("finished_at")),
        "seconds": run_seconds(run),
        "note": run.get("note") or "",
        "modelId": run.get("model_id"),
        "error": run.get("error"),
        "inputTokens": run.get("input_tokens"),
        "outputTokens": run.get("output_tokens"),
        "tokensSource": "cluster" if run.get("input_tokens") is not None else "unrecorded",
        "analystTrace": parse_analyst_trace(run.get("note"))
            if run.get("agent") == "analyst" else None,
    } for run in raw_runs]

    latest_analyst_run = _analyst_run_receipt(raw_runs)
    deduped = _deduped_by_find(data.get("finds") or [], raw_runs)
    retrieval_by_find = {}
    for found in finds:
        retrieval_by_find[found["id"]] = [
            {"key": "queries", "label": "hypothesis queries", "n": len(ANALYST_QUERIES), "source": "code"},
            {"key": "raw", "label": "rows retrieved, ceiling", "n": RAW_HIT_CEILING, "source": "code"},
            {"key": "deduped", "label": "unique after dedup", "n": deduped.get(found["databaseId"]), "source": "cluster" if found["databaseId"] in deduped else "unrecorded"},
            {"key": "cited", "label": "cited as evidence", "n": found["evidenceCount"], "source": "cluster"},
        ]

    bits = [f"{money(daily)}/day earning now"]
    if proposed:
        bits.append(f"{len(proposed)} waiting on you")
    if measuring:
        bits.append(f"{len(measuring)} still measuring")

    return {
        "source": "live",
        "generatedAt": _iso(data.get("_generated")),
        "business": {
            "id": str(business.get("id") or ""),
            "name": business.get("name") or "Unnamed business",
            "category": business.get("category"),
            "city": business.get("city"),
            "region": business.get("region"),
            "goalMonthly": goal_monthly,
            "goalMonthlyTxt": money(goal_monthly),
            "goalNote": business.get("goal_note") or "",
        },
        "summary": {
            "verified": verified,
            "miss": miss,
            "estimated": estimated,
            "judged": judged,
            "hitRate": round(float(hit_rate) * 100) if hit_rate is not None else None,
            "dailyCents": daily,
            "dailyTxt": money(daily),
            "monthlyNow": monthly_now,
            "monthlyNowTxt": money(monthly_now),
            "toGoTxt": money(max(goal_monthly - monthly_now, 0)),
            "goalPct": min(round(monthly_now / goal_monthly * 100), 100) if goal_monthly else 0,
        },
        "corpus": {
            "observations": _int(raw_corpus.get("observations")),
            "evidenceRows": sum(found["evidenceCount"] for found in finds),
            "earliest": _iso(raw_corpus.get("earliest")),
            "latest": _iso(raw_corpus.get("latest")),
        },
        "runs": runs,
        "statusLine": " · ".join(bits),
        "finds": finds,
        "artifactCount": sum(len(found["artifacts"]) for found in finds),
        "retrieval": {
            "source": "live",
            "queries": list(ANALYST_QUERIES),
            "perQueryLimit": DEFAULT_PER_QUERY_LIMIT,
            "byFind": retrieval_by_find,
        },
        "analyst": {
            "source": "cluster",
            "queryCount": len(ANALYST_QUERIES),
            "perQueryLimit": DEFAULT_PER_QUERY_LIMIT,
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
            "tables": list(ASK_TABLES),
            "sessions": _ask_sessions(raw_runs),
        },
        "proposed": [found["id"] for found in proposed],
        "saved": [found["id"] for found in saved],
        "measuring": [found["id"] for found in measuring],
        "earning": [found["id"] for found in earning],
        "judged": [found["id"] for found in judged_finds],
        "kinds": [{
            "kind": item.get("kind"),
            "label": KIND_LABEL.get(item.get("kind"), item.get("kind")),
            "emoji": KIND_EMOJI.get(item.get("kind"), "•"),
            "count": _int(item.get("count")),
        } for item in data.get("kinds") or []],
    }


def _rows(cursor: Any, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    columns = [getattr(description, "name", description[0]) for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _group(rows: list[dict[str, Any]], key: str = "business_id") -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    return grouped


def load_workspaces(conn: Any, business_ids: Sequence[str], *, runs_per_agent: int = 4) -> list[dict[str, Any]]:
    """Read configured tenants in set-based queries and return UI workspaces.

    There is intentionally no request parameter for an arbitrary business id.
    The Lambda receives an allowlist from configuration, so a public demo URL
    cannot be used to enumerate every tenant in the cluster.
    """
    ids = tuple(dict.fromkeys(str(UUID(value)) for value in business_ids))
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))

    with conn.cursor() as cursor:
        businesses = _rows(cursor, f"""
            SELECT id, name, category, city, region, goal_monthly_cents, goal_note
            FROM business
            WHERE id IN ({placeholders})
        """, ids)

        finds = _rows(cursor, f"""
            SELECT business_id, id, run_id, emoji, title, summary,
                   rationale, move,
                   predicted_daily_cents, confidence, verify_after, status,
                   decided_at, created_at
            FROM find
            WHERE business_id IN ({placeholders})
            ORDER BY business_id, created_at DESC
        """, ids)

        evidence = _rows(cursor, f"""
            SELECT f.business_id, fe.find_id, fe.rank, fe.similarity,
                   o.id AS observation_id, o.content, o.kind,
                   o.source_name, o.subject, o.observed_at
            FROM find_evidence fe
            JOIN find f ON f.id = fe.find_id
            JOIN observation o ON o.id = fe.observation_id
            WHERE f.business_id IN ({placeholders})
            ORDER BY f.business_id, fe.find_id, fe.rank
        """, ids)

        artifacts = _rows(cursor, f"""
            SELECT f.business_id, a.id, a.find_id, a.kind, a.title, a.preview,
                   a.s3_bucket, a.s3_key, a.created_at
            FROM artifact a
            JOIN find f ON f.id = a.find_id
            WHERE f.business_id IN ({placeholders})
            ORDER BY f.business_id, a.created_at DESC
        """, ids)

        ledger = _rows(cursor, f"""
            SELECT business_id, find_id, verdict, actual_daily_cents, method,
                   note, measured_at, period_start, period_end
            FROM ledger_entry
            WHERE business_id IN ({placeholders})
            ORDER BY business_id, find_id, measured_at DESC
        """, ids)

        runs = _rows(cursor, f"""
            SELECT business_id, id, agent, status, started_at, finished_at,
                   note, input_tokens, output_tokens, model_id, error
            FROM (
                SELECT ar.*,
                       row_number() OVER (
                           PARTITION BY business_id, agent ORDER BY started_at DESC
                       ) AS workflow_rank,
                       EXISTS (
                           SELECT 1
                           FROM find referenced_find
                           WHERE referenced_find.run_id = ar.id
                             AND referenced_find.business_id = ar.business_id
                             AND referenced_find.status IN ('proposed', 'later', 'accepted', 'live')
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM ledger_entry referenced_ledger
                                 WHERE referenced_ledger.find_id = referenced_find.id
                             )
                       ) AS referenced_find_run
                FROM agent_run ar
                WHERE business_id IN ({placeholders})
            ) ranked
            WHERE workflow_rank <= %s OR referenced_find_run
            ORDER BY business_id, started_at DESC
        """, (*ids, runs_per_agent))

        corpus = _rows(cursor, f"""
            SELECT b.id AS business_id, count(o.id) AS observations,
                   min(o.observed_at) AS earliest, max(o.observed_at) AS latest
            FROM business b
            LEFT JOIN observation o ON o.business_id = b.id
            WHERE b.id IN ({placeholders})
            GROUP BY b.id
        """, ids)

        kinds = _rows(cursor, f"""
            SELECT business_id, kind, count(*) AS count
            FROM observation
            WHERE business_id IN ({placeholders})
            GROUP BY business_id, kind
            ORDER BY business_id, count DESC
        """, ids)

    business_map = {str(row["id"]): row for row in businesses}
    finds_by_business = _group(finds)
    evidence_by_find = _group(evidence, "find_id")
    artifacts_by_find = _group(artifacts, "find_id")
    ledger_by_find = _group(ledger, "find_id")
    runs_by_business = _group(runs)
    corpus_by_business = {str(row["business_id"]): row for row in corpus}
    kinds_by_business = _group(kinds)
    ledger_by_business = _group(ledger)

    workspaces = []
    for business_id in ids:
        business = business_map.get(business_id)
        if business is None:
            continue

        raw_finds = []
        for row in finds_by_business.get(business_id, []):
            find_id = str(row["id"])
            latest_ledger = ledger_by_find.get(find_id, [None])[0]
            raw = dict(row)
            raw.pop("business_id", None)
            raw["evidence"] = [
                {key: value for key, value in item.items() if key not in {"business_id", "find_id"}}
                for item in evidence_by_find.get(find_id, [])
            ]
            raw["artifacts"] = [
                {key: value for key, value in item.items() if key != "business_id"}
                for item in artifacts_by_find.get(find_id, [])
            ]
            for key in (
                "verdict", "actual_daily_cents", "method", "note",
                "measured_at", "period_start", "period_end",
            ):
                raw[key] = latest_ledger.get(key) if latest_ledger else None
            raw_finds.append(raw)

        business_ledger = ledger_by_business.get(business_id, [])
        verified = sum(1 for row in business_ledger if str(row.get("verdict")) == "verified")
        estimated = sum(1 for row in business_ledger if str(row.get("verdict")) == "estimated")
        miss = sum(1 for row in business_ledger if str(row.get("verdict")) == "miss")
        verified_daily = sum(
            _int(row.get("actual_daily_cents"))
            for row in business_ledger
            if str(row.get("verdict")) == "verified"
        )
        judged = verified + miss

        workspaces.append(build_workspace({
            "business": business,
            "summary": {
                "verified": verified,
                "estimated": estimated,
                "miss": miss,
                "verified_daily_cents": verified_daily,
                "judged": judged,
                "hit_rate": verified / judged if judged else None,
            },
            "corpus": corpus_by_business.get(business_id, {
                "observations": 0, "earliest": None, "latest": None,
            }),
            "finds": raw_finds,
            "runs": [
                {key: value for key, value in row.items()
                 if key not in {"business_id", "workflow_rank", "referenced_find_run"}}
                for row in runs_by_business.get(business_id, [])
            ],
            "kinds": [
                {key: value for key, value in row.items() if key != "business_id"}
                for row in kinds_by_business.get(business_id, [])
            ],
        }))

    return workspaces


__all__ = [
    "build_workspace", "load_workspaces", "parse_retrieved_count",
    "money", "short_money",
]
