from __future__ import annotations

import json
from pathlib import Path

from brasstacks.workflow_snapshot import build_workspace, parse_retrieved_count


FIXTURE = Path(__file__).resolve().parents[2] / "db" / "fixtures" / "demo.json"


def demo_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_build_workspace_returns_the_live_operator_contract():
    """The contract, not the tenant.

    This asserted `name == "Rosa's Trattoria"`, 127 observations and 7 judged
    finds — all facts about one seeded demo, none about the shape the operator
    view depends on. The fixture is regenerated from whichever business the
    cluster holds, so pinning its contents made every real export a test
    failure. What matters is that the keys exist and agree with each other.
    """
    data = demo_data()
    workspace = build_workspace(data)

    assert workspace["source"] == "live"
    assert workspace["business"]["id"]
    assert workspace["business"]["name"] == data["business"]["name"]
    assert workspace["corpus"]["observations"] == data["corpus"]["observations"]
    assert workspace["artifactCount"] == sum(
        len(find.get("artifacts") or []) for find in workspace["finds"]
    )

    by_id = {find["id"]: find for find in workspace["finds"]}
    assert set(workspace["proposed"]).issubset(by_id)
    assert set(workspace["saved"]).issubset(by_id)
    assert set(workspace["measuring"]).issubset(by_id)
    assert all(find["databaseId"] for find in workspace["finds"])
    assert all("evidenceCount" in find for find in workspace["finds"])
    first_evidence = next(
        evidence
        for find in workspace["finds"]
        for evidence in find["evidence"]
    )
    assert first_evidence["observationId"]
    assert first_evidence["id"] == first_evidence["observationId"][:8]
    assert isinstance(first_evidence["rank"], int)
    assert "sourceName" in first_evidence
    assert "subject" in first_evidence


def test_build_workspace_carries_decisions_tokens_and_artifacts():
    data = demo_data()
    data["finds"][0]["status"] = "accepted"
    data["finds"][0]["decided_at"] = "2026-08-01T18:00:00+00:00"
    data["finds"][0]["artifacts"] = [{
        "id": "10000000-0000-0000-0000-000000000001",
        "kind": "menu",
        "title": "Dinner menu draft",
        "preview": "A compact preview",
        "s3_bucket": "drafts",
        "s3_key": "menu.txt",
        "created_at": "2026-08-01T18:02:00+00:00",
    }]
    data["runs"][0]["input_tokens"] = 1200
    data["runs"][0]["output_tokens"] = 180
    data["runs"][0]["model_id"] = "model-x"
    data["runs"][0]["error"] = None

    workspace = build_workspace(data)
    first = workspace["finds"][0]

    assert first["status"] == "accepted"
    assert first["decidedAt"] == "2026-08-01T18:00:00+00:00"
    assert first["artifacts"][0]["stored"] is True
    assert workspace["artifactCount"] >= 1
    assert workspace["runs"][0]["inputTokens"] == 1200
    assert workspace["runs"][0]["outputTokens"] == 180


def test_build_workspace_does_not_mutate_the_database_rows():
    data = demo_data()
    before = json.dumps(data, sort_keys=True)
    build_workspace(data)
    assert json.dumps(data, sort_keys=True) == before


def test_retrieval_count_is_recovered_from_the_analyst_receipt():
    assert parse_retrieved_count("24 retrieved; proposed 'Lunch' at +900c/day") == 24
    assert parse_retrieved_count("24 observations retrieved") == 24
    assert parse_retrieved_count("memory is empty; no find proposed") is None



def test_live_operator_query_keeps_recent_receipts_for_each_agent():
    """A busy Radar must not crowd Maker or Meter out of the operator view."""
    source = (Path(__file__).resolve().parents[1] / "src" / "brasstacks" / "workflow_snapshot.py").read_text(encoding="utf-8")
    assert "PARTITION BY business_id, agent ORDER BY started_at DESC" in source
    assert "runs_per_agent: int = 4" in source
    assert "referenced_find_run" in source
    assert "OR referenced_find_run" in source
    assert "referenced_find.status IN ('proposed', 'later', 'accepted', 'live')" in source
    assert "FROM ledger_entry referenced_ledger" in source


def test_workspace_exposes_an_exact_analyst_run_trace():
    """The operator view links a stored find to the run and receipt that made it."""
    from brasstacks.analyst_trace import encode_analyst_trace

    data = demo_data()
    run_id = "20000000-0000-0000-0000-000000000002"
    find_id = data["finds"][0]["id"]
    data["finds"][0]["run_id"] = run_id
    data["runs"].insert(0, {
        "id": run_id,
        "agent": "analyst",
        "status": "ok",
        "started_at": "2026-07-29T10:00:00+00:00",
        "finished_at": "2026-07-29T10:00:12+00:00",
        "note": "24 retrieved; proposed a move\n" + encode_analyst_trace(
            query_hits=[6, 6, 5, 4, 2, 1],
            raw_hits=24,
            unique_hits=17,
            cited_hits=9,
            find_id=find_id,
        ),
        "input_tokens": 2140,
        "output_tokens": 286,
        "model_id": "claude-test",
        "error": None,
    })

    workspace = build_workspace(data)
    trace = workspace["analyst"]["latestRun"]
    first = next(find for find in workspace["finds"] if find["databaseId"] == find_id)

    assert trace["runId"] == run_id
    assert trace["rawHits"] == 24
    assert trace["uniqueHits"] == 17
    assert trace["citedHits"] == 9
    assert trace["queryHits"] == [6, 6, 5, 4, 2, 1]
    assert trace["inputTokens"] == 2140
    assert trace["outputTokens"] == 286
    assert trace["findId"] == find_id
    assert first["runDatabaseId"] == run_id
    assert first["origin"] == "agent_run"


def test_workspace_marks_historical_find_without_run_receipt_honestly():
    """A find with no agent run behind it must say so.

    Constructed rather than read out of the fixture. The invariant is about
    finds that carry no run receipt — seeded history, imports — and the shipped
    fixture only contains one while the demo tenant is seeded. Once a real
    business runs a real night, every find has a receipt and this stopped being
    exercised at all, which is worse than it failing.
    """
    data = demo_data()
    data["finds"] = [dict(data["finds"][0], run_id=None)]
    data["runs"] = []

    workspace = build_workspace(data)
    first = workspace["finds"][0]

    assert first["runDatabaseId"] is None
    assert first["origin"] == "historical_import"
    assert workspace["analyst"]["latestRun"] is None
