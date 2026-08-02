"""Step one of signup: the account, created before the business exists.

This endpoint exists so the password never has to survive a page navigation.
The alternative — two pages with the password parked in sessionStorage between
them — puts a plaintext credential in browser storage for as long as the owner
takes to fill in a form. These tests pin the properties that buys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from brasstacks.handlers import register as register_handler
from brasstacks.repository import InMemoryRepository

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
PASSWORD = "a long enough passphrase"


@pytest.fixture
def repo():
    return InMemoryRepository()


def call(repo, *, code="let-me-in", **body):
    if code is not None:
        body["inviteCode"] = code
    return register_handler.register(
        {"body": json.dumps(body)}, repo=repo, invite_code="let-me-in", now=NOW)


class TestRegister:
    def test_it_creates_an_account_and_signs_the_owner_in(self, repo):
        response = call(repo, username="sam", password=PASSWORD)
        body = json.loads(response["body"])

        assert response["statusCode"] == 201
        assert len(body["token"]) >= 32
        assert repo.find_account("sam") is not None

    def test_the_account_has_no_business_yet(self, repo):
        # The whole point of the split: the account exists first, and
        # onboarding attaches a business to it afterwards.
        body = json.loads(call(repo, username="sam", password=PASSWORD)["body"])

        assert body["business_id"] is None
        assert repo.find_account("sam")["business_id"] is None

    def test_the_token_is_immediately_usable(self, repo):
        from brasstacks.auth import token_fingerprint

        body = json.loads(call(repo, username="sam", password=PASSWORD)["body"])
        account = repo.account_for_session(
            token_fingerprint(body["token"]), now=NOW)

        assert account is not None
        assert account["business_id"] is None

    def test_the_password_is_never_stored_as_typed(self, repo):
        call(repo, username="sam", password=PASSWORD)

        assert PASSWORD not in json.dumps(repo._accounts)

    def test_the_password_never_comes_back(self, repo):
        response = call(repo, username="sam", password=PASSWORD)

        assert PASSWORD not in json.dumps(response)

    def test_a_short_password_creates_nothing(self, repo):
        response = call(repo, username="sam", password="short")

        assert response["statusCode"] == 400
        assert repo._accounts == {}

    def test_a_bad_username_creates_nothing(self, repo):
        response = call(repo, username="has space", password=PASSWORD)

        assert response["statusCode"] == 400
        assert repo._accounts == {}

    def test_a_taken_username_is_named_as_such(self, repo):
        # Unlike login, this one has to distinguish: the owner cannot pick
        # another username without being told this one is gone. Availability is
        # inherently public on any site with signup.
        call(repo, username="sam", password=PASSWORD)

        response = call(repo, username="sam", password="another long one")

        assert response["statusCode"] == 409
        assert "taken" in json.loads(response["body"])["error"]

    def test_a_wrong_invite_code_creates_nothing(self, repo):
        response = call(repo, code="guess", username="sam", password=PASSWORD)

        assert response["statusCode"] == 403
        assert repo._accounts == {}

    def test_a_missing_invite_code_creates_nothing(self, repo):
        response = call(repo, code=None, username="sam", password=PASSWORD)

        assert response["statusCode"] == 403
        assert repo._accounts == {}

    def test_a_body_that_is_not_json_is_the_callers_fault(self, repo):
        response = register_handler.register(
            {"body": "not json"}, repo=repo, invite_code="let-me-in", now=NOW)

        assert response["statusCode"] == 400

    def test_the_response_allows_the_hosted_site_to_read_it(self, repo):
        response = call(repo, username="sam", password=PASSWORD)

        assert response["headers"]["Access-Control-Allow-Origin"] == "*"
