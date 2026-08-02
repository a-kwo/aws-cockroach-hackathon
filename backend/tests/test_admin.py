"""The operator view's gate.

This endpoint returns every active tenant's finds, revenue figures and business
facts in one response. It is the one place in the system where a single request
crosses tenant boundaries on purpose, so the refusals are the interesting tests.

Hiding the tab in the page is not a gate — anyone can open devtools. The gate is
here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from brasstacks.auth import hash_password, issue_session_token
from brasstacks.handlers.admin import list_workspaces
from brasstacks.repository import InMemoryRepository

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def account(repo, username, *, admin=False, business="Asaka"):
    business_id = repo.create_business(name=business, category="restaurant")
    account_id = repo.create_account(business_id, username=username,
                                     password_hash=hash_password("x"))
    if admin:
        repo.set_account_admin(username, is_admin=True)
    token, fingerprint, expires = issue_session_token(now=NOW)
    repo.create_session(fingerprint, business_id=business_id,
                        account_id=account_id, expires_at=expires)
    return token, business_id


@pytest.fixture
def repo():
    return InMemoryRepository()


def call(repo, token, *, loader=None, now=NOW):
    event = {"headers": {"Authorization": f"Bearer {token}"}} if token else {}
    return list_workspaces(
        event, repo=repo,
        load=loader or (lambda ids: [{"business": {"id": i}} for i in ids]),
        now=now)


class TestAdminGate:
    def test_an_admin_sees_every_active_tenant(self, repo):
        token, mine = account(repo, "operator", admin=True)
        theirs = repo.create_business(name="Nonna's", category="restaurant")

        response = call(repo, token)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        ids = {w["business"]["id"] for w in body["workspaces"]}
        assert ids == {mine, theirs}

    def test_an_ordinary_owner_is_refused(self, repo):
        """The whole point. This owner has a perfectly valid session — it just
        does not entitle them to anyone else's revenue figures."""
        token, _ = account(repo, "sam")
        repo.create_business(name="Someone Else", category="restaurant")

        response = call(repo, token)

        assert response["statusCode"] == 403
        assert "workspaces" not in json.loads(response["body"])

    def test_no_session_is_refused(self, repo):
        assert call(repo, None)["statusCode"] == 401

    def test_an_unknown_token_is_refused(self, repo):
        account(repo, "operator", admin=True)

        assert call(repo, "not-a-real-token")["statusCode"] == 401

    def test_an_expired_session_is_refused(self, repo):
        token, _ = account(repo, "operator", admin=True)

        later = NOW + timedelta(days=15)
        assert call(repo, token, now=later)["statusCode"] == 401

    def test_revoking_admin_takes_effect_on_the_next_request(self, repo):
        """Not in two weeks when the session happens to expire. The flag is read
        from the account, not copied onto the session at login."""
        token, _ = account(repo, "operator", admin=True)
        assert call(repo, token)["statusCode"] == 200

        repo.set_account_admin("operator", is_admin=False)

        assert call(repo, token)["statusCode"] == 403

    def test_a_paused_tenant_is_not_listed(self, repo):
        token, mine = account(repo, "operator", admin=True)
        paused = repo.create_business(name="Paused", category="restaurant")
        repo.set_business_status(paused, status="paused")

        body = json.loads(call(repo, token)["body"])

        assert {w["business"]["id"] for w in body["workspaces"]} == {mine}

    def test_the_response_says_it_read_only_sql(self, repo):
        # Same disclosure the owner-facing workflow endpoint makes: this view is
        # SQL over CockroachDB, with no model in the loop.
        token, _ = account(repo, "operator", admin=True)

        body = json.loads(call(repo, token)["body"])

        assert body["source"] == "cockroachdb"
        assert body["readMode"] == "sql-only"
        assert body["modelTokens"] == 0
