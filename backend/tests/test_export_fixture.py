"""The exporter is production code too — it decides what the site can be honest about.

It used to be untestable: `import psycopg` at module scope and one long `main()`
that connected, queried and wrote in a single breath. That put it outside the
project's own rule that every cloud call is faked at the boundary, and it meant
the one piece of code deciding what reaches the page had no coverage at all.

`export(cur, business_id)` now runs against any DB-API cursor, so these tests
drive the real SQL and the real derivation logic with no cluster.
"""

from __future__ import annotations

import pytest

import export_fixture


BUSINESS = "bf5e7c88-0000-0000-0000-000000000000"


class StubCursor:
    """A DB-API cursor that answers from a script, keyed by query name.

    It reverse-looks-up the SQL in `export_fixture.QUERIES`, so a query the
    catalogue does not contain is an error rather than a silent empty result —
    the exporter cannot smuggle in an unnamed query.
    """

    def __init__(self, results):
        self.results = results
        self.executed = []
        self._columns = []
        self._rows = []

    def execute(self, sql, params=None):
        name = next((n for n, q in export_fixture.QUERIES.items() if q == sql), None)
        assert name is not None, f"query is not in the catalogue:\n{sql}"
        self.executed.append((name, sql, params))
        self._columns, self._rows = self.results.get(name, ([], []))

    @property
    def description(self):
        return [(c,) for c in self._columns]

    def fetchall(self):
        return list(self._rows)


def script(**over):
    """A minimal but complete answer for every query the exporter issues."""
    base = {
        "business": (
            ["id", "name", "category", "city", "region",
             "goal_monthly_cents", "goal_note"],
            [(BUSINESS, "Rosa's Trattoria", "restaurant", "Columbus", "OH",
              800000, None)],
        ),
        "finds": (
            ["id", "emoji", "title", "rationale", "move",
             "predicted_daily_cents", "confidence", "verify_after", "status",
             "created_at", "run_id", "run_agent", "verdict",
             "actual_daily_cents", "method", "note", "measured_at",
             "period_start", "period_end", "ledger_run_id"],
            [("f1", "🍰", "Tiramisu → $9", "Because.", "Reprice it.",
              2300, 0.88, "2026-07-01", "live", "2026-06-01T02:00:00+00:00",
              "run-analyst", "analyst", "verified", 2500, "terminal sales",
              "233 sold.", "2026-06-24T06:00:00+00:00", "2026-06-01",
              "2026-06-15", "run-meter")],
        ),
        "evidence": (
            ["find_id", "rank", "similarity", "observation_id", "content",
             "kind", "source_name", "subject", "observed_at"],
            [("f1", 0, 0.702, "obs-1", "Best tiramisu in the city.", "review",
              "review_site", None, "2026-06-02T19:00:00+00:00")],
        ),
        "artifacts": (
            ["id", "find_id", "kind", "title", "preview", "s3_bucket",
             "s3_key", "created_at"],
            [],
        ),
        "summary": (
            ["verified", "estimated", "miss", "verified_daily_cents"],
            [(1, 0, 0, 2500)],
        ),
        "runs": (
            ["id", "agent", "status", "started_at", "finished_at", "note",
             "model_id", "error", "input_tokens", "output_tokens",
             "observations", "finds", "artifacts", "ledger_entries"],
            [("run-analyst", "analyst", "ok", "2026-06-01T02:00:00+00:00",
              "2026-06-01T02:01:18+00:00", "41 retrieved; proposed 'x'",
              "claude-opus-5", None, 8123, 642, 0, 1, 0, 0)],
        ),
        "run_count": (["total"], [(1,)]),
        "corpus": (
            ["observations", "earliest", "latest", "unattributed", "runs"],
            [(127, "2026-06-02T19:00:00+00:00", "2026-07-27T19:00:00+00:00",
              0, 1)],
        ),
        "monthly": (
            ["month", "verified_daily_cents", "verified", "miss"],
            [("2026-06-01", 2500, 1, 0)],
        ),
        "kinds": (["kind", "count"], [("review", 79)]),
        "ratings": (
            ["week", "avg_rating", "reviews"],
            [("2026-06-01", 4.14, 7)],
        ),
        "cluster": (
            ["database", "version", "now"],
            [("defaultdb", "CockroachDB CCL v26.2.1",
              "2026-07-31T04:12:00+00:00")],
        ),
    }
    base.update(over)
    return base


@pytest.fixture
def cur():
    return StubCursor(script())


# ------------------------------------------------------------------ lineage


def test_a_find_carries_the_run_that_produced_it(cur):
    [found] = export_fixture.export(cur, BUSINESS)["finds"]
    assert found["run_id"] == "run-analyst"
    assert found["seeded"] is False


def test_a_find_written_by_a_radar_run_is_marked_as_seeded():
    """Every seeded find carries the run_id of the Radar run that built the
    corpus, because scripts/seed.py threads one run through observations, finds
    and ledger rows alike. Rendering those as Analyst work would credit
    retrieval for reasoning it never did."""
    columns, [row] = script()["finds"]
    row = list(row)
    row[columns.index("run_agent")] = "radar"
    stub = StubCursor(script(finds=(columns, [tuple(row)])))

    [found] = export_fixture.export(stub, BUSINESS)["finds"]
    assert found["seeded"] is True


def test_a_find_with_no_run_at_all_is_seeded_not_unknown():
    """Absent lineage is still not Analyst lineage. The console needs a boolean
    it can style, not a null it has to guess about."""
    columns, [row] = script()["finds"]
    row = list(row)
    row[columns.index("run_id")] = None
    row[columns.index("run_agent")] = None
    stub = StubCursor(script(finds=(columns, [tuple(row)])))

    assert export_fixture.export(stub, BUSINESS)["finds"][0]["seeded"] is True


def test_a_run_carries_what_it_read_and_what_it_wrote(cur):
    [run] = export_fixture.export(cur, BUSINESS)["runs"]
    assert run["model_id"] == "claude-opus-5"
    assert (run["input_tokens"], run["output_tokens"]) == (8123, 642)
    assert run["finds"] == 1


def test_the_run_list_reports_how_many_it_left_out(cur):
    """The list is capped. A capped list that does not say so reads as the whole
    record — the old LIMIT 12 would have silently dropped the seed run after
    three nights while the corpus panel still claimed its 127 observations."""
    payload = export_fixture.export(cur, BUSINESS)
    assert payload["run_count"] == 1
    assert len(payload["runs"]) <= payload["run_count"]


def test_the_corpus_says_how_much_of_it_has_no_run(cur):
    corpus = export_fixture.export(cur, BUSINESS)["corpus"]
    assert corpus["unattributed"] == 0
    assert corpus["runs"] == 1


# ------------------------------------------------------------------ receipt


def test_the_receipt_carries_the_sql_that_actually_ran(cur):
    receipt = export_fixture.export(cur, BUSINESS)["_receipt"]
    by_name = {q["name"]: q for q in receipt["queries"]}

    assert by_name["finds"]["sql"] == export_fixture.QUERIES["finds"].strip()
    assert "%s" in by_name["finds"]["sql"], (
        "the placeholder is intact — the receipt shows the parameterised query, "
        "never one with a value interpolated into it")
    assert by_name["finds"]["rows"] == 1
    assert by_name["finds"]["client_ms"] >= 0


def test_the_receipt_never_contains_a_connection_string(cur):
    """The receipt is rendered verbatim on a public page. Nothing in it may be
    a credential, and the only way to be sure is to assert it."""
    import json

    blob = json.dumps(export_fixture.export(cur, BUSINESS))
    assert "postgresql://" not in blob
    assert "sslmode" not in blob
    assert "@" not in blob.replace("\\u0040", "")


def test_the_stamp_comes_from_the_cluster_clock_not_this_machine(cur):
    """A build-time snapshot may say when it was taken, but the timestamp has to
    come from the thing being snapshotted. A local clock would be the fake-live
    problem wearing a different hat."""
    payload = export_fixture.export(cur, BUSINESS)
    assert payload["_generated"] == "2026-07-31T04:12:00+00:00"
    assert payload["_receipt"]["version"].startswith("CockroachDB")


def test_a_cluster_that_will_not_describe_itself_still_exports():
    """A permission-scoped role may not be able to read crdb_internal. Losing
    the stamp is acceptable; losing the fixture is not."""
    stub = StubCursor(script(cluster=([], [])))
    payload = export_fixture.export(stub, BUSINESS)

    assert payload["_generated"] is None
    assert payload["_receipt"]["version"] is None
    assert len(payload["finds"]) == 1


# ------------------------------------------------------------------ catalogue


def test_the_export_never_reaches_into_crdb_internal():
    """CockroachDB Cloud restricts `crdb_internal` and `system` to admin roles.

    Measured against the live cluster: `crdb_internal.cluster_id()` raises
    InsufficientPrivilege for the role this exporter connects as, and because it
    shared a SELECT with `now()`, the failure took the whole "as of" stamp with
    it — the console then reported having no export stamp at all. Nothing the
    exporter runs may depend on an interface the deployment will not grant.
    """
    for name, sql in export_fixture.QUERIES.items():
        assert "crdb_internal" not in sql, name
        assert "system." not in sql, name


def test_the_cluster_id_is_configured_rather_than_queried(cur):
    """It is an identifier we already hold, not a fact to be discovered."""
    payload = export_fixture.export(cur, BUSINESS, cluster_id="41b89d47")
    assert payload["_receipt"]["clusterId"] == "41b89d47"

    # Absent is absent — never an empty string dressed up as a value.
    assert export_fixture.export(cur, BUSINESS)["_receipt"]["clusterId"] is None


def test_every_export_query_is_named_and_scoped_to_one_business(cur):
    """One tenant's rows reach the page. A query without a business filter
    would leak another tenant's data the day a second one exists.

    `cluster` is the one exemption, and it is exempt because it describes the
    cluster rather than the tenant — it returns no tenant data at all."""
    export_fixture.export(cur, BUSINESS)

    ran = {name for name, _, _ in cur.executed}
    assert ran == set(export_fixture.QUERIES), "every catalogued query is used"

    for name, sql, params in cur.executed:
        if name == "cluster":
            assert params is None and "%s" not in sql
            continue
        assert params == (BUSINESS,), name
        assert sql.count("%s") == 1, name
        # `business` is scoped by its own primary key; everything else by the
        # foreign key, directly or through an aliased join.
        assert ("business_id = %s" in sql
                or "FROM business WHERE id = %s" in sql), name


def test_the_exporter_nests_evidence_under_the_find_that_cited_it(cur):
    [found] = export_fixture.export(cur, BUSINESS)["finds"]
    assert [e["observation_id"] for e in found["evidence"]] == ["obs-1"]
    assert "find_id" not in found["evidence"][0], "the parent id is the key, not a column"


def test_the_hit_rate_ignores_estimates(cur):
    """An estimate is not a judgement. Counting it would move a rate that only
    measured outcomes are allowed to move."""
    columns, _ = script()["summary"]
    stub = StubCursor(script(summary=(columns, [(3, 9, 1, 2500)])))

    summary = export_fixture.export(stub, BUSINESS)["summary"]
    assert summary["judged"] == 4
    assert summary["hit_rate"] == pytest.approx(0.75)
