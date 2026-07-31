"""Export the live demo tenant to a JSON fixture the site build reads.

The frontend is being built before the API exists. Rather than invent mock data —
which would let the UI drift from what the backend can actually produce — this
dumps the real seeded tenant. When the API lands, the fixture is replaced by
fetch calls returning the same shapes.

    python scripts/export_fixture.py

Raw SQL here rather than repository methods: this is a temporary bridge, and the
API layer will use the repository properly. The queries live in one named
catalogue (`QUERIES`) and `export(cur, business_id)` runs against any DB-API
cursor, so the code that decides what the public page may claim is testable with
no cluster and no credentials — the same "fake at the boundary" rule the agents
follow. `psycopg` is imported inside `main()` for the same reason.

Every query is also recorded in `_receipt`: its name, the SQL verbatim, how many
rows came back, and how long the round trip took. That receipt is what the admin
view shows instead of a pulsing "live" dot. A dot over a static snapshot is a
claim the page cannot support; the SQL, the cluster version and the cluster's own
clock are facts it can.
"""

from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "db" / "fixtures" / "demo.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brasstacks.config import Settings  # noqa: E402

#: How many agent_run rows reach the page. Was 12, which was sized for a cluster
#: holding exactly one run: three nights alone produce twelve rows and would have
#: pushed the seed run off the list while the corpus panel still credited it with
#: 127 observations. The total is exported alongside so a capped list can say so.
RUN_LIMIT = 200


QUERIES: dict[str, str] = {
    "business": """
        SELECT id, name, category, city, region, goal_monthly_cents, goal_note
        FROM business WHERE id = %s
    """,

    # `run_agent` is the lineage that matters: a find written by scripts/seed.py
    # carries the *Radar* run that built the corpus, not an Analyst run. Without
    # this join the console would render seeded finds as retrieval-driven
    # reasoning, which is exactly the claim this project cannot fake.
    "finds": """
        SELECT f.id, f.emoji, f.title, f.rationale, f.move,
               f.predicted_daily_cents, f.confidence, f.verify_after,
               f.status, f.created_at, f.run_id, ar.agent AS run_agent,
               le.verdict, le.actual_daily_cents, le.method, le.note,
               le.measured_at, le.period_start, le.period_end,
               le.run_id AS ledger_run_id
        FROM find f
        LEFT JOIN ledger_entry le ON le.find_id = f.id
        LEFT JOIN agent_run ar ON ar.id = f.run_id
        WHERE f.business_id = %s
        ORDER BY f.created_at DESC
    """,

    "evidence": """
        SELECT fe.find_id, fe.rank, fe.similarity,
               o.id AS observation_id, o.content, o.kind,
               o.source_name, o.subject, o.observed_at
        FROM find_evidence fe
        JOIN observation o ON o.id = fe.observation_id
        JOIN find f ON f.id = fe.find_id
        WHERE f.business_id = %s
        ORDER BY fe.find_id, fe.rank
    """,

    # The Maker's deliverables. `preview` is the opening of the draft itself,
    # never a summary, so the UI can show her what she is about to approve
    # rather than a description of it.
    "artifacts": """
        SELECT a.id, a.find_id, a.kind, a.title, a.preview,
               a.s3_bucket, a.s3_key, a.created_at
        FROM artifact a
        JOIN find f ON f.id = a.find_id
        WHERE f.business_id = %s
        ORDER BY a.created_at DESC
    """,

    "summary": """
        SELECT
            count(*) FILTER (WHERE verdict = 'verified')  AS verified,
            count(*) FILTER (WHERE verdict = 'estimated') AS estimated,
            count(*) FILTER (WHERE verdict = 'miss')      AS miss,
            coalesce(sum(actual_daily_cents)
                     FILTER (WHERE verdict = 'verified'), 0) AS verified_daily_cents
        FROM ledger_entry WHERE business_id = %s
    """,

    # What each run cost and what it produced. The counts are correlated
    # subqueries rather than joins so a run that wrote nothing still returns a
    # row with zeros instead of vanishing.
    "runs": f"""
        SELECT ar.id, ar.agent, ar.status, ar.started_at, ar.finished_at, ar.note,
               ar.model_id, ar.error, ar.input_tokens, ar.output_tokens,
               (SELECT count(*) FROM observation o WHERE o.run_id = ar.id)
                   AS observations,
               (SELECT count(*) FROM find f WHERE f.run_id = ar.id) AS finds,
               (SELECT count(*) FROM artifact a WHERE a.run_id = ar.id) AS artifacts,
               (SELECT count(*) FROM ledger_entry le WHERE le.run_id = ar.id)
                   AS ledger_entries
        FROM agent_run ar
        WHERE ar.business_id = %s
        ORDER BY ar.started_at DESC LIMIT {RUN_LIMIT}
    """,

    "run_count": """
        SELECT count(*) AS total FROM agent_run WHERE business_id = %s
    """,

    "corpus": """
        SELECT count(*) AS observations,
               min(observed_at) AS earliest,
               max(observed_at) AS latest,
               count(*) FILTER (WHERE run_id IS NULL) AS unattributed,
               count(DISTINCT run_id) AS runs
        FROM observation WHERE business_id = %s
    """,

    # --- chart aggregates, computed in SQL so the UI never derives money and
    # --- cannot drift from the stored cent values.

    "monthly": """
        SELECT date_trunc('month', measured_at)::DATE AS month,
               coalesce(sum(actual_daily_cents)
                        FILTER (WHERE verdict = 'verified'), 0) AS verified_daily_cents,
               count(*) FILTER (WHERE verdict = 'verified') AS verified,
               count(*) FILTER (WHERE verdict = 'miss')     AS miss
        FROM ledger_entry
        WHERE business_id = %s
        GROUP BY 1 ORDER BY 1
    """,

    "kinds": """
        SELECT kind, count(*) AS count
        FROM observation WHERE business_id = %s
        GROUP BY 1 ORDER BY 2 DESC
    """,

    "ratings": """
        SELECT date_trunc('week', observed_at)::DATE AS week,
               round(avg(rating)::NUMERIC, 2) AS avg_rating,
               count(*) AS reviews
        FROM observation
        WHERE business_id = %s AND rating IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """,

    # Not tenant-scoped, because it describes the cluster rather than the
    # tenant. `version()` is a live-cluster fact a static page could not
    # plausibly invent, and `now()` is the cluster's clock rather than the
    # exporting laptop's.
    "cluster": """
        SELECT current_database() AS database,
               version() AS version,
               crdb_internal.cluster_id()::STRING AS cluster_id,
               now() AS now
    """,
}


def jsonable(value):
    if isinstance(value, (UUID, Decimal)):
        return float(value) if isinstance(value, Decimal) else str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def export(cur, business_id: str) -> dict:
    """Run every catalogued query against `cur` and assemble the payload.

    Takes a cursor rather than a connection so it can be driven by a stub. The
    receipt is built here, not by the caller, because a receipt that is not
    produced by the same code path as the data could describe a query that never
    ran.
    """
    receipt: list[dict] = []

    def rows(name: str, params=None) -> list[dict]:
        sql = QUERIES[name]
        started = time.perf_counter()
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        out = [
            {c: jsonable(v) for c, v in zip(columns, row)}
            for row in cur.fetchall()
        ]
        receipt.append({
            "name": name,
            "sql": sql.strip(),
            "rows": len(out),
            # Wall time from the exporting machine, round trip included — not
            # the cluster's own execution time. The panel says so.
            "client_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        return out

    scoped = (business_id,)

    [business] = rows("business", scoped)
    finds = rows("finds", scoped)
    evidence = rows("evidence", scoped)
    artifacts = rows("artifacts", scoped)
    [summary] = rows("summary", scoped)
    runs = rows("runs", scoped)
    [run_total] = rows("run_count", scoped)
    [corpus] = rows("corpus", scoped)
    monthly = rows("monthly", scoped)
    kinds = rows("kinds", scoped)
    ratings = rows("ratings", scoped)

    # A permission-scoped role may not be able to read crdb_internal. Losing the
    # stamp is acceptable; losing the fixture is not.
    try:
        cluster = next(iter(rows("cluster")), {})
    except Exception:
        cluster = {}

    by_find: dict[str, list[dict]] = {}
    for row in evidence:
        by_find.setdefault(row.pop("find_id"), []).append(row)

    # Artifacts hang off their find the same way evidence does: the deliverable
    # belongs to the promise that produced it.
    drafts: dict[str, list[dict]] = {}
    for row in artifacts:
        drafts.setdefault(row.pop("find_id"), []).append(row)

    for find in finds:
        find["evidence"] = by_find.get(find["id"], [])
        find["artifacts"] = drafts.get(find["id"], [])
        # Derived here rather than in SQL so the rule is testable, and stated as
        # a boolean rather than a null so the console can style it. A find whose
        # run was Radar's — or which has no run at all — was written by
        # scripts/seed.py before any Analyst had ever run. Rendering it as
        # retrieval-driven reasoning would credit a search that never happened.
        find["seeded"] = find.get("run_agent") != "analyst"

    judged = summary["verified"] + summary["miss"]
    summary["hit_rate"] = (summary["verified"] / judged) if judged else None
    summary["judged"] = judged

    return {
        "_comment": "Exported from the live CockroachDB demo tenant by "
                    "scripts/export_fixture.py. Replaced by API calls once the "
                    "API layer exists; the shapes are the contract.",
        # The cluster's clock, not this machine's. Read by scripts/build_web.py
        # and shown as the "as of" stamp.
        "_generated": cluster.get("now"),
        "_receipt": {
            "database": cluster.get("database"),
            "version": cluster.get("version"),
            "clusterId": cluster.get("cluster_id"),
            "queries": receipt,
        },
        "business": business,
        "summary": summary,
        "corpus": corpus,
        "finds": finds,
        "runs": runs,
        "run_count": run_total["total"],
        "monthly": monthly,
        "kinds": kinds,
        "ratings": ratings,
    }


def main() -> int:
    # Imported here rather than at module scope so the tests — and anyone
    # reading the query catalogue — do not need psycopg installed.
    import psycopg

    settings = Settings.load()
    if not settings.business_id:
        raise SystemExit("BRASSTACKS_BUSINESS_ID is not set. Run scripts/seed.py.")

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            payload = export(cur, settings.business_id)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    finds = payload["finds"]
    summary = payload["summary"]
    open_finds = [f for f in finds if f["verdict"] is None]
    seeded = [f for f in finds if f["seeded"]]
    evidence_rows = sum(len(f["evidence"]) for f in finds)

    print(f"exported to {OUT_PATH.relative_to(REPO_ROOT)}")
    print(f"  {len(finds)} finds ({len(open_finds)} awaiting a decision, "
          f"{len(seeded)} seeded rather than reasoned)")
    print(f"  {evidence_rows} evidence rows")
    print(f"  {payload['corpus']['observations']} observations in the corpus")
    print(f"  {payload['run_count']} agent_run rows "
          f"({len(payload['runs'])} exported)")
    print(f"  ledger: {summary['verified']}V / {summary['miss']}M / "
          f"{summary['estimated']}E")
    if payload["_generated"]:
        print(f"  cluster clock: {payload['_generated']}")
    else:
        print("  cluster would not describe itself; no 'as of' stamp recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
