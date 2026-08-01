import json
import inspect
from datetime import datetime, timezone

import pytest

from brasstacks.handlers import decision as decision_handler
from brasstacks.handlers.decision import UI_TO_DB, parse_request, respond


def event(find_id="00000000-0000-0000-0000-000000000001", decision="approved"):
    return {
        "pathParameters": {"find_id": find_id},
        "body": json.dumps({"decision": decision}),
    }


def test_parse_approved_decision():
    assert parse_request(event()) == (
        "00000000-0000-0000-0000-000000000001",
        "approved",
    )


def test_parse_rejected_decision():
    _, decision = parse_request(event(decision="rejected"))
    assert decision == "rejected"
    assert UI_TO_DB[decision] == "rejected"


@pytest.mark.parametrize("decision", ["", "later", "accept", "yes"])
def test_rejects_unknown_decision(decision):
    with pytest.raises(ValueError, match="approved.*rejected"):
        parse_request(event(decision=decision))


def test_requires_find_id():
    payload = event()
    payload["pathParameters"] = {}
    with pytest.raises(ValueError, match="find_id"):
        parse_request(payload)


def test_response_includes_cors():
    result = respond(200, {"ok": True})
    assert result["statusCode"] == 200
    assert result["headers"]["Access-Control-Allow-Origin"] == "*"
    assert json.loads(result["body"]) == {"ok": True}


def test_success_receipt_can_carry_the_authoritative_decision_time():
    decided_at = datetime(2026, 8, 1, 18, 3, 4, tzinfo=timezone.utc)
    result = respond(200, {
        "find_id": "00000000-0000-0000-0000-000000000001",
        "decision": "approved",
        "decided_at": decided_at.isoformat(),
    })
    body = json.loads(result["body"])
    assert body["decided_at"] == "2026-08-01T18:03:04+00:00"


def test_handler_uses_one_server_timestamp_for_the_write_and_receipt():
    source = inspect.getsource(decision_handler.handler)
    assert "datetime.now(timezone.utc)" in source
    assert "decided_at=decided_at" in source
    assert '"decided_at": decided_at.isoformat()' in source
