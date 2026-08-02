"""Accounts and sessions at the memory layer.

Exercised against InMemoryRepository here; the same contract runs against a real
cluster under `pytest -m integration`. The negatives matter more than the
positives in this file — an unknown username, an expired session and a token for
a deleted business are the paths someone probing the login endpoint walks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brasstacks.auth import hash_password, issue_session_token
from brasstacks.repository import InMemoryRepository, RepositoryError

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def business(repo):
    return repo.create_business(name="Asaka", category="restaurant")


class TestAccounts:
    def test_an_account_can_be_found_by_its_username(self, repo, business):
        repo.create_account(business, username="sam",
                            password_hash=hash_password("a long enough one"))

        found = repo.find_account("sam")

        assert found["business_id"] == business
        assert found["username"] == "sam"

    def test_an_unknown_username_is_none_not_an_error(self, repo):
        # The login handler needs to spend the same work either way; raising
        # here would make "no such user" a different code path and a timing
        # signal.
        assert repo.find_account("nobody") is None

    def test_a_username_cannot_be_taken_twice(self, repo, business):
        other = repo.create_business(name="Other", category="restaurant")
        repo.create_account(business, username="sam",
                            password_hash=hash_password("a long enough one"))

        with pytest.raises(RepositoryError, match="taken"):
            repo.create_account(other, username="sam",
                                password_hash=hash_password("another long one"))

    def test_the_stored_hash_comes_back_for_verification(self, repo, business):
        stored = hash_password("a long enough one")
        repo.create_account(business, username="sam", password_hash=stored)

        assert repo.find_account("sam")["password_hash"] == stored


class TestSessions:
    def _login(self, repo, business, *, now=NOW):
        account = repo.create_account(
            business, username="sam", password_hash=hash_password("a long one"))
        token, fingerprint, expires = issue_session_token(now=now)
        repo.create_session(fingerprint, business_id=business,
                            account_id=account, expires_at=expires)
        return token, fingerprint

    def test_a_live_session_resolves_to_its_business(self, repo, business):
        _, fingerprint = self._login(repo, business)

        assert repo.business_for_session(fingerprint, now=NOW) == business

    def test_an_expired_session_resolves_to_nothing(self, repo, business):
        _, fingerprint = self._login(repo, business)

        later = NOW + timedelta(days=15)
        assert repo.business_for_session(fingerprint, now=later) is None

    def test_an_unknown_token_resolves_to_nothing(self, repo, business):
        self._login(repo, business)

        assert repo.business_for_session("not-a-real-fingerprint", now=NOW) is None

    def test_logging_out_ends_the_session(self, repo, business):
        _, fingerprint = self._login(repo, business)

        repo.delete_session(fingerprint)

        assert repo.business_for_session(fingerprint, now=NOW) is None


class TestActiveBusinesses:
    def test_a_new_business_is_active(self, repo, business):
        assert repo.active_business_ids() == [business]

    def test_a_paused_business_gets_no_night(self, repo, business):
        repo.set_business_status(business, status="paused")

        assert repo.active_business_ids() == []

    def test_the_list_is_capped(self, repo):
        # Every active tenant costs a search, ~50 embeddings and a Claude call
        # per night. The cap is what stops an afternoon of curious signups
        # multiplying the nightly bill.
        for i in range(5):
            repo.create_business(name=f"B{i}", category="restaurant")

        assert len(repo.active_business_ids(limit=3)) == 3


class TestSessionsWithoutABusiness:
    """An operator account has no business, and neither does an owner between
    /register and finishing onboarding. Both must resolve to nothing rather than
    to the string "None", which is truthy and would be queried as a tenant id."""

    def test_a_session_with_no_business_resolves_to_none(self, repo):
        account = repo.create_account(None, username="admin",
                                      password_hash=hash_password("x"))
        token, fingerprint, expires = issue_session_token(now=NOW)
        repo.create_session(fingerprint, business_id=None,
                            account_id=account, expires_at=expires)

        resolved = repo.business_for_session(fingerprint, now=NOW)

        assert resolved is None
        assert resolved != "None"

    def test_the_account_lookup_agrees(self, repo):
        account = repo.create_account(None, username="admin",
                                      password_hash=hash_password("x"))
        token, fingerprint, expires = issue_session_token(now=NOW)
        repo.create_session(fingerprint, business_id=None,
                            account_id=account, expires_at=expires)

        assert repo.account_for_session(fingerprint, now=NOW)["business_id"] is None
        assert repo.find_account("admin")["business_id"] is None
