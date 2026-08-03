"""Export the live demo tenant to a JSON fixture the site build reads.

The frontend is being built before the API exists. Rather than invent mock data —
which would let the UI drift from what the backend can actually produce — this
dumps the real seeded tenant. When the API lands, the fixture is replaced by
fetch calls returning the same shapes.

    python scripts/export_fixture.py

Raw SQL here rather than repository methods: this is a temporary bridge, and the
API layer will use the repository properly.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "db" / "fixtures" / "demo.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brasstacks.config import Settings  # noqa: E402
from brasstacks.profile_schema import ensure_profile_schema  # noqa: E402


def jsonable(value):
    if isinstance(value, (UUID, Decimal)):
        return float(value) if isinstance(value, Decimal) else str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def rows(cur, sql, params) -> list[dict]:
    cur.execute(sql, params)
    columns = [d[0] for d in cur.description]
    return [
        {c: jsonable(v) for c, v in zip(columns, row)}
        for row in cur.fetchall()
    ]


def main() -> int:
    settings = Settings.load()
    if not settings.business_id:
        raise SystemExit("BRASSTACKS_BUSINESS_ID is not set. Run scripts/seed.py.")

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        ensure_profile_schema(conn)
        with conn.cursor() as cur:
            [business] = rows(cur, """
                SELECT id, name, category, city, region, goal_monthly_cents, goal_note
                FROM business WHERE id = %s
            """, (settings.business_id,))

            owner_rows = rows(cur, """
                SELECT id, username, display_name, email, last_login_at
                FROM owner_account
                WHERE business_id = %s
                ORDER BY created_at
                LIMIT 1
            """, (settings.business_id,))
            owner = owner_rows[0] if owner_rows else {}

            finds = rows(cur, """
                SELECT f.id, f.run_id, f.emoji, f.title, f.summary, f.rationale, f.move,
                       f.predicted_daily_cents, f.confidence, f.verify_after,
                       f.status, f.decided_at, f.created_at,
                       le.verdict, le.actual_daily_cents, le.method, le.note,
                       le.measured_at, le.period_start, le.period_end
                FROM find f
                LEFT JOIN ledger_entry le ON le.find_id = f.id
                WHERE f.business_id = %s
                ORDER BY f.created_at DESC
            """, (settings.business_id,))

            evidence = rows(cur, """
                SELECT fe.find_id, fe.rank, fe.similarity,
                       o.id AS observation_id, o.content, o.kind,
                       o.source_name, o.subject, o.observed_at
                FROM find_evidence fe
                JOIN observation o ON o.id = fe.observation_id
                JOIN find f ON f.id = fe.find_id
                WHERE f.business_id = %s
                ORDER BY fe.find_id, fe.rank
            """, (settings.business_id,))

            # The Maker's deliverables. `preview` is the opening of the draft
            # itself, never a summary, so the UI can show her what she is about
            # to approve rather than a description of it.
            artifacts = rows(cur, """
                SELECT a.id, a.find_id, a.kind, a.title, a.preview,
                       a.s3_bucket, a.s3_key, a.created_at, a.task_id,
                       a.idempotency_key, a.body
                FROM artifact a
                JOIN find f ON f.id = a.find_id
                WHERE f.business_id = %s
                ORDER BY a.created_at DESC
            """, (settings.business_id,))

            [summary] = rows(cur, """
                SELECT
                    count(*) FILTER (WHERE verdict = 'verified')  AS verified,
                    count(*) FILTER (WHERE verdict = 'estimated') AS estimated,
                    count(*) FILTER (WHERE verdict = 'miss')      AS miss,
                    coalesce(sum(actual_daily_cents)
                             FILTER (WHERE verdict = 'verified'), 0) AS verified_daily_cents
                FROM ledger_entry WHERE business_id = %s
            """, (settings.business_id,))

            runs = rows(cur, """
                WITH recent AS (
                    SELECT id, agent, status, started_at, finished_at, note,
                           input_tokens, output_tokens, model_id, error
                    FROM agent_run
                    WHERE business_id = %s
                    ORDER BY started_at DESC
                    LIMIT 12
                ), referenced AS (
                    SELECT ar.id, ar.agent, ar.status, ar.started_at,
                           ar.finished_at, ar.note, ar.input_tokens,
                           ar.output_tokens, ar.model_id, ar.error
                    FROM agent_run ar
                    JOIN find f ON f.run_id = ar.id
                    WHERE f.business_id = %s
                      AND f.status IN ('proposed', 'later', 'accepted', 'live')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM ledger_entry le
                          WHERE le.find_id = f.id
                      )
                )
                SELECT DISTINCT id, agent, status, started_at, finished_at, note,
                       input_tokens, output_tokens, model_id, error
                FROM (
                    SELECT * FROM recent
                    UNION ALL
                    SELECT * FROM referenced
                ) receipts
                ORDER BY started_at DESC
            """, (settings.business_id, settings.business_id))

            [corpus] = rows(cur, """
                SELECT count(*) FILTER (
                           WHERE coalesce(source_name, '') NOT IN ('owner_chat', 'ask_agent')
                       ) AS observations,
                       count(*) FILTER (
                           WHERE source_name IN ('owner_chat', 'ask_agent')
                       ) AS conversation_messages,
                       min(observed_at) FILTER (
                           WHERE coalesce(source_name, '') NOT IN ('owner_chat', 'ask_agent')
                       ) AS earliest,
                       max(observed_at) FILTER (
                           WHERE coalesce(source_name, '') NOT IN ('owner_chat', 'ask_agent')
                       ) AS latest
                FROM observation WHERE business_id = %s
            """, (settings.business_id,))

            # --- chart aggregates, computed in SQL so the UI never derives
            # --- money and cannot drift from the stored cent values.

            monthly = rows(cur, """
                SELECT date_trunc('month', measured_at)::DATE AS month,
                       coalesce(sum(actual_daily_cents)
                                FILTER (WHERE verdict = 'verified'), 0) AS verified_daily_cents,
                       count(*) FILTER (WHERE verdict = 'verified') AS verified,
                       count(*) FILTER (WHERE verdict = 'miss')     AS miss
                FROM ledger_entry
                WHERE business_id = %s
                GROUP BY 1 ORDER BY 1
            """, (settings.business_id,))

            kinds = rows(cur, """
                SELECT kind, count(*) AS count
                FROM observation
                WHERE business_id = %s
                  AND coalesce(source_name, '') NOT IN ('owner_chat', 'ask_agent')
                GROUP BY 1 ORDER BY 2 DESC
            """, (settings.business_id,))

            ratings = rows(cur, """
                SELECT date_trunc('week', observed_at)::DATE AS week,
                       round(avg(rating)::NUMERIC, 2) AS avg_rating,
                       count(*) AS reviews
                FROM observation
                WHERE business_id = %s AND rating IS NOT NULL
                GROUP BY 1 ORDER BY 1
            """, (settings.business_id,))

    by_find: dict[str, list[dict]] = {}
    for row in evidence:
        by_find.setdefault(row.pop("find_id"), []).append(row)
    for find in finds:
        find["evidence"] = by_find.get(find["id"], [])

    # Artifacts hang off their find the same way evidence does: the deliverable
    # belongs to the promise that produced it.
    drafts: dict[str, list[dict]] = {}
    for row in artifacts:
        drafts.setdefault(row.pop("find_id"), []).append(row)
    for find in finds:
        find["artifacts"] = drafts.get(find["id"], [])

    judged = summary["verified"] + summary["miss"]
    summary["hit_rate"] = (summary["verified"] / judged) if judged else None
    summary["judged"] = judged

    payload = {
        "_comment": "Exported from the live CockroachDB demo tenant by "
                    "scripts/export_fixture.py. Used as the resilient first "
                    "paint; the live workflow endpoint overlays current state "
                    "using the same browser contract.",
        "business": business,
        "owner": owner,
        "summary": summary,
        "corpus": corpus,
        "finds": finds,
        "runs": runs,
        "monthly": monthly,
        "kinds": kinds,
        "ratings": ratings,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    open_finds = [f for f in finds if f["verdict"] is None]
    print(f"exported to {OUT_PATH.relative_to(REPO_ROOT)}")
    print(f"  {len(finds)} finds ({len(open_finds)} awaiting a decision)")
    print(f"  {len(evidence)} evidence rows")
    print(f"  {corpus['observations']} observations in the corpus")
    print(f"  ledger: {summary['verified']}V / {summary['miss']}M / "
          f"{summary['estimated']}E")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
