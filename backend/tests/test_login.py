"""The login endpoint.

Public, unauthenticated by definition, and the way into every tenant's data. The
tests that matter here are the refusals.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from brasstacks.auth import hash_password
from brasstacks.handlers import login as login_handler
from brasstacks.repository import InMemoryRepository

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
PASSWORD = "a long enough passphrase"


@pytest.fixture
def repo():
    repo = InMemoryRepository()
    business = repo.create_business(name="Asaka", category="restaurant")
    repo.create_account(business, username="sam",
                        password_hash=hash_password(PASSWORD))
    repo.business_id = business
    return repo


def event(**body):
    return {"body": json.dumps(body)}


def call(repo, **body):
    return login_handler.login(event(**body), repo=repo, now=NOW)


class TestLogin:
    def test_correct_credentials_return_a_token(self, repo):
        response = call(repo, username="sam", password=PASSWORD)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert body["business_id"] == repo.business_id
        assert len(body["token"]) >= 32

    def test_the_token_works_as_a_session(self, repo):
        from brasstacks.auth import token_fingerprint

        body = json.loads(call(repo, username="sam", password=PASSWORD)["body"])

        assert repo.business_for_session(
            token_fingerprint(body["token"]), now=NOW) == repo.business_id

    def test_the_username_is_matched_case_insensitively(self, repo):
        assert call(repo, username="  SAM ", password=PASSWORD)["statusCode"] == 200

    def test_a_wrong_password_is_refused(self, repo):
        response = call(repo, username="sam", password="wrong but long enough")

        assert response["statusCode"] == 401

    def test_an_unknown_user_is_refused_identically(self, repo):
        # Same status and same message as a wrong password. Distinguishing them
        # tells an attacker which usernames exist, which is the first half of
        # the work.
        wrong = call(repo, username="sam", password="wrong but long enough")
        unknown = call(repo, username="nobody", password=PASSWORD)

        assert unknown["statusCode"] == wrong["statusCode"] == 401
        assert json.loads(unknown["body"]) == json.loads(wrong["body"])

    def test_a_refusal_never_says_which_half_was_wrong(self, repo):
        body = json.loads(call(repo, username="nobody", password="x" * 20)["body"])

        text = json.dumps(body).lower()
        assert "password" not in text or "username" not in text.replace(
            "username or password", "")

    def test_a_missing_field_is_a_400_not_a_500(self, repo):
        assert call(repo, username="sam")["statusCode"] == 400
        assert call(repo, password=PASSWORD)["statusCode"] == 400

    def test_the_password_never_appears_in_the_response(self, repo):
        response = call(repo, username="sam", password=PASSWORD)

        assert PASSWORD not in json.dumps(response)

    def test_the_response_allows_the_hosted_site_to_read_it(self, repo):
        response = call(repo, username="sam", password=PASSWORD)

        assert response["headers"]["Access-Control-Allow-Origin"] == "*"


class TestPasswordlessAccounts:
    """Sign in with Google creates accounts with no password at all.

    `password_hash` is NULL for those (oauth_google.callback passes None, and
    schema.sql drops the NOT NULL). The handler substitutes a decoy hash so
    that a passwordless account costs the same scrypt work as any other and
    "this username uses Google" stays unmeasurable from outside.

    A substitute hash must never be a usable credential. The decoy's plaintext
    is a string literal in this public repo, so if it verifies, anyone who
    knows a username owns that tenant.
    """

    DECOY_PLAINTEXT = "decoy for constant work on unknown usernames"

    @pytest.fixture
    def google_repo(self):
        repo = InMemoryRepository()
        business = repo.create_business(name="Asaka", category="restaurant")
        repo.create_account(business, username="sam", password_hash=None)
        repo.business_id = business
        return repo

    def test_the_decoy_password_does_not_sign_in_a_google_account(self, google_repo):
        response = call(google_repo, username="sam",
                        password=self.DECOY_PLAINTEXT)

        assert response["statusCode"] == 401
        assert "token" not in json.loads(response["body"])

    def test_no_password_at_all_signs_in_a_google_account(self, google_repo):
        for attempt in ["", "None", "null", "x", PASSWORD]:
            response = call(google_repo, username="sam", password=attempt)
            assert response["statusCode"] in (400, 401), attempt

    def test_a_passwordless_account_still_costs_the_kdf(self, google_repo):
        """The refusal must not short-circuit before the scrypt call.

        Returning early on a null hash would make Google accounts measurably
        faster to reject than password accounts, which is the leak the decoy
        was introduced to close.
        """
        import brasstacks.handlers.login as module

        calls = []
        original = module.verify_password
        module.verify_password = lambda password, stored: (
            calls.append(stored) or original(password, stored))
        try:
            call(google_repo, username="sam", password="anything")
        finally:
            module.verify_password = original

        assert calls, "verify_password was skipped for a passwordless account"


class TestLogout:
    def test_logging_out_ends_the_session(self, repo):
        from brasstacks.auth import token_fingerprint

        token = json.loads(
            call(repo, username="sam", password=PASSWORD)["body"])["token"]

        login_handler.logout(
            {"headers": {"Authorization": f"Bearer {token}"}}, repo=repo)

        assert repo.business_for_session(token_fingerprint(token), now=NOW) is None

    def test_logging_out_twice_is_not_an_error(self, repo):
        response = login_handler.logout(
            {"headers": {"Authorization": "Bearer nonsense"}}, repo=repo)

        assert response["statusCode"] == 200
