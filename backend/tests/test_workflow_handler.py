from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import brasstacks.handlers.workflow as workflow


SESSION_BUSINESS = "00000000-0000-0000-0000-000000000009"
SIGNED_IN = {"headers": {"authorization": "Bearer a-live-token"}}


class FakeCursor:
    """Answers the one query the handler makes before reading workspaces:
    business_for_session."""

    def __init__(self, business_id):
        self._business_id = business_id

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return (self._business_id,) if self._business_id else None


class FakeConnection:
    #: None means "no live session for that token".
    session_business = SESSION_BUSINESS

    def cursor(self):
        return FakeCursor(self.session_business)

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakePsycopg:
    class Error(Exception):
        pass

    def __init__(self):
        self.calls = []

    def connect(self, url, autocommit=True):
        self.calls.append((url, autocommit))
        return FakeConnection()


def test_business_ids_use_the_explicit_operator_allowlist():
    ids = workflow.configured_business_ids(
        SimpleNamespace(business_id="00000000-0000-0000-0000-000000000001"),
        {"BRASSTACKS_OPERATOR_BUSINESS_IDS": (
            "00000000-0000-0000-0000-000000000002,"
            "00000000-0000-0000-0000-000000000003"
        )},
    )
    assert ids == (
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    )


def test_business_ids_fall_back_to_the_demo_tenant():
    assert workflow.configured_business_ids(
        SimpleNamespace(business_id="00000000-0000-0000-0000-000000000001"), {}
    ) == ("00000000-0000-0000-0000-000000000001",)


def test_business_ids_reject_an_invalid_uuid():
    with pytest.raises(ValueError, match="UUID"):
        workflow.configured_business_ids(
            SimpleNamespace(business_id=None),
            {"BRASSTACKS_OPERATOR_BUSINESS_IDS": "not-an-id"},
        )


def test_a_config_failure_does_not_echo_its_details(monkeypatch):
    """A 500 must not narrate the infrastructure to whoever caused it.

    Settings and secrets errors name parameter paths and expected environment
    variables — a map of the deployment, served to any unauthenticated caller
    who manages to arrive while configuration is broken. The detail belongs in
    the log; the caller gets a sentence.
    """
    def boom():
        raise RuntimeError(
            "could not read /brasstacks/COCKROACH_DATABASE_URL from ssm")

    monkeypatch.setattr(workflow, "hydrate_environment", boom)
    response = workflow.handler(SIGNED_IN)

    assert response["statusCode"] == 500
    error = json.loads(response["body"])["error"].lower()
    for fragment in ("ssm", "cockroach", "/brasstacks", "database_url"):
        assert fragment not in error


def test_response_exposes_etag_for_conditional_refreshes():
    out = workflow.respond(200, {"workspaces": []}, etag='"abc"')
    assert out["headers"]["ETag"] == '"abc"'
    assert out["headers"]["Access-Control-Expose-Headers"] == "ETag"
    assert out["headers"]["Cache-Control"] == "no-cache, max-age=0"


def test_handler_returns_a_live_snapshot(monkeypatch):
    fake_psycopg = FakePsycopg()
    monkeypatch.setattr(workflow, "get_psycopg", lambda: fake_psycopg)
    monkeypatch.setattr(workflow, "hydrate_environment", lambda: 0)
    monkeypatch.setattr(
        workflow.Settings,
        "load",
        classmethod(lambda cls: SimpleNamespace(
            cockroach_url="postgresql://cluster",
            business_id="00000000-0000-0000-0000-000000000001",
        )),
    )
    monkeypatch.setattr(
        workflow,
        "load_workspaces",
        lambda conn, ids: [{"business": {"id": ids[0], "name": "Rosa's"}}],
    )

    out = workflow.handler(dict(SIGNED_IN), None)
    payload = json.loads(out["body"])

    assert out["statusCode"] == 200
    assert payload["source"] == "cockroachdb"
    assert payload["readMode"] == "sql-only"
    assert payload["modelTokens"] == 0
    assert payload["workspaces"][0]["business"]["id"] == SESSION_BUSINESS
    assert payload["workspaces"][0]["business"]["name"] == "Rosa's"
    assert out["headers"]["ETag"].startswith('"')
    assert fake_psycopg.calls == [("postgresql://cluster", True)]


def test_handler_returns_304_when_the_snapshot_has_not_changed(monkeypatch):
    fake_psycopg = FakePsycopg()
    monkeypatch.setattr(workflow, "get_psycopg", lambda: fake_psycopg)
    monkeypatch.setattr(workflow, "hydrate_environment", lambda: 0)
    monkeypatch.setattr(
        workflow.Settings,
        "load",
        classmethod(lambda cls: SimpleNamespace(
            cockroach_url="postgresql://cluster",
            business_id="00000000-0000-0000-0000-000000000001",
        )),
    )
    monkeypatch.setattr(
        workflow,
        "load_workspaces",
        lambda conn, ids: [{"business": {"id": ids[0], "name": "Rosa's"}}],
    )

    first = workflow.handler(dict(SIGNED_IN), None)
    etag = first["headers"]["ETag"]
    second = workflow.handler({"headers": {**SIGNED_IN["headers"],
                                  "if-none-match": etag}}, None)

    assert second["statusCode"] == 304
    assert second["body"] == ""
    assert second["headers"]["ETag"] == etag


# ---------------------------------------------------------------------------
# The board belongs to whoever is signed in
# ---------------------------------------------------------------------------

def _configured(monkeypatch, fake_psycopg):
    monkeypatch.setattr(workflow, "get_psycopg", lambda: fake_psycopg)
    monkeypatch.setattr(workflow, "hydrate_environment", lambda: 0)
    monkeypatch.setattr(
        workflow.Settings, "load",
        classmethod(lambda cls: SimpleNamespace(
            cockroach_url="postgresql://cluster", business_id=None)))
    monkeypatch.setattr(
        workflow, "load_workspaces",
        lambda conn, ids: [{"business": {"id": ids[0], "name": "Theirs"}}])


def test_no_session_reads_nothing(monkeypatch):
    """An unauthenticated caller used to get whatever tenant the SSM allowlist
    named — which, once businesses sign themselves up, is someone else's."""
    fake = FakePsycopg()
    _configured(monkeypatch, fake)

    out = workflow.handler({"headers": {}}, None)

    assert out["statusCode"] == 401
    assert fake.calls == [], "it should not even open a connection"


def test_an_expired_or_unknown_token_reads_nothing(monkeypatch):
    fake = FakePsycopg()
    _configured(monkeypatch, fake)
    monkeypatch.setattr(FakeConnection, "session_business", None)

    out = workflow.handler(dict(SIGNED_IN), None)

    assert out["statusCode"] == 401


def test_the_tenant_comes_from_the_session_not_the_request(monkeypatch):
    """A business id in the request would let any signed-in owner read any
    other tenant's finds, revenue and business facts."""
    fake = FakePsycopg()
    _configured(monkeypatch, fake)

    event = dict(SIGNED_IN)
    event["queryStringParameters"] = {
        "business_id": "00000000-0000-0000-0000-0000000000ff"}
    payload = json.loads(workflow.handler(event, None)["body"])

    assert payload["workspaces"][0]["business"]["id"] == SESSION_BUSINESS
