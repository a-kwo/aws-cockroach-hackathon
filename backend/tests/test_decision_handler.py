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
    source = inspect.getsource(decision_handler.record_decision)
    assert "datetime.now(timezone.utc)" in source
    assert "decided_at=moment" in source
    assert '"decided_at": moment.isoformat()' in source

class FakeDecisionRepo:
    def __init__(self, account=None, error=None):
        self.account = account
        self.error = error
        self.session_lookup = None
        self.write = None

    def account_for_session(self, token_hash, *, now):
        self.session_lookup = (token_hash, now)
        return self.account

    def set_find_status(self, find_id, *, status, decided_at, business_id):
        if self.error:
            raise self.error
        self.write = {
            "find_id": find_id,
            "status": status,
            "decided_at": decided_at,
            "business_id": business_id,
        }


def authenticated_event(*, find_id="00000000-0000-0000-0000-000000000001", decision="approved"):
    payload = event(find_id=find_id, decision=decision)
    payload["headers"] = {"Authorization": "Bearer owner-session-token"}
    return payload


def test_record_decision_requires_a_session():
    repo = FakeDecisionRepo(account=None)
    result = decision_handler.record_decision(event(), repo=repo)
    assert result["statusCode"] == 401
    assert json.loads(result["body"])["error"] == "sign in first"
    assert repo.write is None


def test_record_decision_scopes_the_write_to_the_session_business():
    moment = datetime(2026, 8, 2, 19, 4, 5, tzinfo=timezone.utc)
    repo = FakeDecisionRepo(account={
        "account_id": "owner-1",
        "business_id": "business-from-session",
        "is_admin": False,
    })

    result = decision_handler.record_decision(
        authenticated_event(), repo=repo, now=moment)

    assert result["statusCode"] == 200
    assert repo.write == {
        "find_id": "00000000-0000-0000-0000-000000000001",
        "status": "accepted",
        "decided_at": moment,
        "business_id": "business-from-session",
    }
    body = json.loads(result["body"])
    assert body["decision"] == "approved"
    assert body["maker"] == "queued"


def test_record_decision_does_not_fall_back_to_the_seeded_config_tenant():
    source = inspect.getsource(decision_handler.record_decision)
    assert "account_for_session" in source
    assert 'account.get("business_id")' in source
    assert "settings.business_id" not in source


def test_decision_cors_allows_the_authorization_header():
    result = respond(200, {"ok": True})
    assert "Authorization" in result["headers"]["Access-Control-Allow-Headers"]
