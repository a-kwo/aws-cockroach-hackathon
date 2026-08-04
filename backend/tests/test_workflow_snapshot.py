from __future__ import annotations

import json
from pathlib import Path

from brasstacks.workflow_snapshot import build_workspace, parse_retrieved_count


#: A frozen sample, not the shipped fixture. db/fixtures/demo.json is
#: regenerated from whichever business the live cluster holds, so its contents
#: are not a fact about this code — and every honest export broke this file:
#: once when the tenant was renamed, and again when a fresh tenant exported with
#: zero finds and IndexError'd four tests at finds[0]. See the sample's own
#: comment for what it was captured from.
SAMPLE = Path(__file__).resolve().parent / "data" / "workspace_sample.json"


def demo_data() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


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


def test_workspace_exposes_bounded_task_receipts_and_full_draft_for_owner_review():
    data = demo_data()
    find = data["finds"][0]
    task_id = "30000000-0000-0000-0000-000000000003"
    artifact_id = "40000000-0000-0000-0000-000000000004"
    find["status"] = "accepted"
    find["artifacts"] = [{
        "id": artifact_id,
        "kind": "review_reply",
        "title": "Lunch offer email",
        "preview": "A short preview",
        "body": "The complete owner-ready draft.\n\nPost this after review.",
        "s3_bucket": "drafts",
        "s3_key": f"tasks/{task_id}/review_reply.md",
        "created_at": "2026-08-02T20:02:00+00:00",
        "task_id": task_id,
        "idempotency_key": f"task:{task_id}:artifact:review_reply:v1",
    }]
    data["tasks"] = [{
        "id": task_id,
        "business_id": data["business"]["id"],
        "find_id": find["id"],
        "requested_by_account_id": None,
        "agent": "maker",
        "task_type": "maker.generate_draft",
        "status": "completed",
        "priority": 100,
        "resource_key": f"maker:find:{data['business']['id']}:{find['id']}",
        "approval_state": "approved",
        "attempt_count": 1,
        "dispatch_count": 1,
        "approved_at": "2026-08-02T20:00:00+00:00",
        "created_at": "2026-08-02T20:00:00+00:00",
        "updated_at": "2026-08-02T20:02:00+00:00",
        "started_at": "2026-08-02T20:01:00+00:00",
        "completed_at": "2026-08-02T20:02:00+00:00",
        "next_attempt_at": None,
        "lease_expires_at": None,
        "claimed_by": None,
        "workflow_execution_arn": "arn:aws:states:us-east-1:123:execution:maker:task",
        "output_artifact_id": artifact_id,
        "last_error": None,
        "input_data": {"title": find["title"]},
        "output_data": {"artifact_id": artifact_id},
        "events": [{
            "id": "event-1", "event_type": "task.completed",
            "actor_type": "worker", "actor_id": None,
            "data": {"output_artifact_id": artifact_id},
            "created_at": "2026-08-02T20:02:00+00:00",
        }],
        "tools": [{
            "id": "tool-1", "tool_name": "ses.send_review_email",
            "status": "succeeded", "started_at": "2026-08-02T20:02:01+00:00",
            "finished_at": "2026-08-02T20:02:02+00:00",
            "external_reference": "ses-message-id", "error": None,
            "input_data": {"recipient": "virtual.icfd@gmail.com"},
            "output_data": {"recipient": "virtual.icfd@gmail.com"},
        }],
    }]

    workspace = build_workspace(data)
    task = workspace["maker"]["tasks"][0]
    artifact = workspace["finds"][0]["artifacts"][0]

    assert task["id"] == task_id
    assert task["status"] == "completed"
    assert task["artifactId"] == artifact_id
    assert task["tools"][0]["externalReference"] == "ses-message-id"
    assert artifact["taskId"] == task_id
    assert artifact["databaseId"] == artifact_id
    assert artifact["body"].startswith("The complete owner-ready draft")


def test_live_task_receipt_queries_are_bounded_per_task():
    source = (Path(__file__).resolve().parents[1] / "src" / "brasstacks" /
              "workflow_snapshot.py").read_text(encoding="utf-8")

    assert "PARTITION BY task_id ORDER BY created_at DESC" in source
    assert "WHERE task_event_rank <= 20" in source
    assert "PARTITION BY task_id ORDER BY started_at DESC" in source
    assert "WHERE tool_execution_rank <= 10" in source
    assert "PARTITION BY tool_execution_id" in source
    assert "WHERE email_event_rank <= 20" in source


def test_workspace_exposes_owner_identity_and_email_for_operator_traceability():
    data = demo_data()
    data["owner"] = {
        "id": "50000000-0000-0000-0000-000000000005",
        "username": "rosa",
        "display_name": "Rosa Owner",
        "email": "peter.flp.2006@gmail.com",
        "last_login_at": "2026-08-03T11:30:00+00:00",
    }

    workspace = build_workspace(data)

    assert workspace["owner"] == {
        "id": "50000000-0000-0000-0000-000000000005",
        "username": "rosa",
        "name": "Rosa Owner",
        "email": "peter.flp.2006@gmail.com",
        "lastLoginAt": "2026-08-03T11:30:00+00:00",
    }


def test_workspace_projects_reopened_cycle_without_counting_archived_maker_work_as_current():
    data = demo_data()
    found = data["finds"][0]
    find_id = found["id"]
    task_id = "61000000-0000-0000-0000-000000000006"
    artifact_id = "62000000-0000-0000-0000-000000000006"
    reopened_at = "2026-08-02T20:10:00+00:00"

    found.update({
        "status": "proposed",
        "decision_cycle": 2,
        "decided_at": reopened_at,
        "reopened_at": reopened_at,
        "reopen_reason_code": "needs_revision",
        "reopen_reason_note": "Use a lower price.",
        "decision_events": [
            {
                "id": "63000000-0000-0000-0000-000000000006",
                "event_type": "owner.reopened",
                "decision_cycle": 2,
                "previous_status": "accepted",
                "new_status": "proposed",
                "actor_account_id": None,
                "reason_code": "needs_revision",
                "reason_note": "Use a lower price.",
                "source": "owner_reconsider",
                "data": {"previous_cycle": 1},
                "created_at": reopened_at,
            },
            {
                "id": "64000000-0000-0000-0000-000000000006",
                "event_type": "owner.accepted",
                "decision_cycle": 1,
                "previous_status": "proposed",
                "new_status": "accepted",
                "actor_account_id": None,
                "reason_code": None,
                "reason_note": None,
                "source": "owner_decision",
                "data": {},
                "created_at": "2026-08-02T20:00:00+00:00",
            },
        ],
        "artifacts": [{
            "id": artifact_id,
            "kind": "review_reply",
            "title": "Archived cycle-one draft",
            "preview": "The previous draft remains visible.",
            "body": "Complete archived draft",
            "s3_bucket": "drafts",
            "s3_key": f"tasks/{task_id}/review_reply.md",
            "created_at": "2026-08-02T20:02:00+00:00",
            "task_id": task_id,
            "idempotency_key": f"task:{task_id}:artifact:review_reply:v1",
            "decision_cycle": 1,
            "superseded_at": reopened_at,
        }],
    })
    data["tasks"] = [{
        "id": task_id,
        "business_id": data["business"]["id"],
        "find_id": find_id,
        "requested_by_account_id": None,
        "agent": "maker",
        "task_type": "maker.generate_draft",
        "status": "completed",
        "priority": 100,
        "resource_key": f"maker:find:{data['business']['id']}:{find_id}",
        "approval_state": "approved",
        "decision_cycle": 1,
        "attempt_count": 1,
        "dispatch_count": 1,
        "approved_at": "2026-08-02T20:00:00+00:00",
        "created_at": "2026-08-02T20:00:00+00:00",
        "updated_at": reopened_at,
        "started_at": "2026-08-02T20:01:00+00:00",
        "completed_at": "2026-08-02T20:02:00+00:00",
        "cancel_requested_at": None,
        "cancelled_at": None,
        "superseded_at": reopened_at,
        "next_attempt_at": None,
        "lease_expires_at": None,
        "claimed_by": None,
        "workflow_execution_arn": "arn:aws:states:us-east-1:123:execution:maker:cycle-1",
        "output_artifact_id": artifact_id,
        "last_error": None,
        "input_data": {"title": found["title"], "decision_cycle": 1},
        "output_data": {"artifact_id": artifact_id},
        "events": [],
        "tools": [{
            "id": "65000000-0000-0000-0000-000000000006",
            "tool_name": "ses.send_review_email",
            "status": "succeeded",
            "started_at": "2026-08-02T20:02:01+00:00",
            "finished_at": "2026-08-02T20:02:02+00:00",
            "external_reference": "ses-cycle-1",
            "error": None,
            "input_data": {"recipient": "owner@example.com"},
            "output_data": {"recipient": "owner@example.com"},
        }],
    }]

    workspace = build_workspace(data)
    projected = next(item for item in workspace["finds"] if item["databaseId"] == find_id)
    task = workspace["maker"]["tasks"][0]
    artifact = projected["artifacts"][0]

    assert projected["status"] == "proposed"
    assert projected["decisionCycle"] == 2
    assert projected["reopenedAt"] == reopened_at
    assert [event["type"] for event in projected["decisionEvents"]] == [
        "owner.reopened", "owner.accepted",
    ]
    assert task["decisionCycle"] == 1
    assert task["supersededAt"] == reopened_at
    assert artifact["decisionCycle"] == 1
    assert artifact["current"] is False
    assert workspace["maker"]["ready"] == 0
    assert workspace["maker"]["superseded"] == 1
    assert workspace["maker"]["emailSucceeded"] == 0


def test_an_estimated_ledger_row_reports_no_actual():
    """The Meter stores no `actual_daily_cents` for a verdict it could not
    measure. `_int(None)` is 0, and a 0 on this screen reads as a measurement
    that came back empty — a different fact from never having been measured."""
    data = demo_data()
    data["finds"][0]["verdict"] = "estimated"
    data["finds"][0]["actual_daily_cents"] = None
    data["finds"][0]["measured_at"] = "2026-08-02T06:00:00+00:00"

    first = build_workspace(data)["finds"][0]

    assert first["verdict"] == "estimated"
    assert first["actualDaily"] is None
    assert first["actualDailyTxt"] is None
