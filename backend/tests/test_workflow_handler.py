from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import brasstacks.handlers.workflow as workflow


class FakeConnection:
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

    out = workflow.handler({"headers": {}}, None)
    payload = json.loads(out["body"])

    assert out["statusCode"] == 200
    assert payload["source"] == "cockroachdb"
    assert payload["readMode"] == "sql-only"
    assert payload["modelTokens"] == 0
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

    first = workflow.handler({"headers": {}}, None)
    etag = first["headers"]["ETag"]
    second = workflow.handler({"headers": {"if-none-match": etag}}, None)

    assert second["statusCode"] == 304
    assert second["body"] == ""
    assert second["headers"]["ETag"] == etag
