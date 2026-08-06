"""The three legs of Sign in with Google, end to end and offline.

    /auth/google/start     -> redirect to Google, invite checked first
    /auth/google/callback  -> Google redirects back; account found or created
    /auth/google/complete  -> the browser trades a one-time code for a session

Why three and not two. The callback is a browser redirect, so anything it
returns lands in a URL — and a URL goes into history, into the Referer header of
the next request, and into whatever logs the hop through. CLAUDE.md already
rejected capability URLs for exactly this. So the callback creates no session at
all: it stores a short-lived one-time code and the page POSTs that back for the
real token. `test_the_callback_creates_no_session` is the test that keeps it
that way.

The invite gate is the other thing under test. It is a spend control — every
active tenant costs a search, ~50 embeddings and a Claude call every night — so
an OAuth button that skipped it would be a hole in the budget, not just in the
policy.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from brasstacks import oauth
from brasstacks.auth import token_fingerprint
from brasstacks.handlers import oauth_google
from brasstacks.repository import InMemoryRepository
from test_oauth import CLIENT_ID, make_id_token

NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
INVITE = "let-me-in"

CONFIG = oauth_google.GoogleOAuthConfig(
    client_id=CLIENT_ID,
    client_secret="shh",
    redirect_uri="https://api.example/v1/auth/google/callback",
    state_secret="a state signing secret",
    frontend_url="https://brasstacks.example",
)


@pytest.fixture
def repo():
    return InMemoryRepository()


def start(*, invite=INVITE, config=CONFIG):
    query = {"invite": invite} if invite is not None else {}
    return oauth_google.start({"queryStringParameters": query}, config=config,
                              invite_code=INVITE, now=NOW)


def good_state():
    return oauth.sign_state({"invited": True}, secret=CONFIG.state_secret,
                            now=NOW)


def callback(repo, *, state=None, code="the-code", now=NOW, query=None,
             id_token=None, config=CONFIG):
    params = {"state": state if state is not None else good_state(),
              "code": code}
    if query is not None:
        params = query
    return oauth_google.callback(
        {"queryStringParameters": params}, repo=repo, config=config, now=now,
        transport=lambda url, form: {"id_token": id_token or make_id_token()})


def handoff_code(response):
    """Pull the one-time code out of the redirect the callback issued."""
    fragment = urlsplit(response["headers"]["Location"]).fragment
    return parse_qs(fragment)["code"][0]


def complete(repo, code, *, now=NOW):
    return oauth_google.complete({"body": json.dumps({"code": code})},
                                 repo=repo, now=now)


class TestStart:
    def test_it_redirects_to_google(self):
        response = start()

        assert response["statusCode"] == 302
        assert response["headers"]["Location"].startswith(
            oauth.AUTHORIZE_ENDPOINT)

    def test_the_state_it_sends_is_one_we_can_verify_later(self):
        state = parse_qs(
            urlsplit(start()["headers"]["Location"]).query)["state"][0]

        assert oauth.verify_state(
            state, secret=CONFIG.state_secret, now=NOW)["invited"] is True

    def test_a_wrong_invite_code_never_reaches_google(self):
        response = start(invite="guess")

        assert response["statusCode"] == 403
        assert "Location" not in response["headers"]

    def test_a_missing_invite_code_never_reaches_google(self):
        response = start(invite=None)

        assert response["statusCode"] == 403

    def test_it_is_not_offered_when_google_is_not_configured(self):
        # Before the OAuth client exists in the Google console there is nothing
        # to redirect to, and a 500 from a half-set environment reads like a bug.
        response = start(config=oauth_google.GoogleOAuthConfig(
            client_id=None, client_secret=None, redirect_uri=None,
            state_secret=None, frontend_url=None))

        assert response["statusCode"] == 404

    def test_the_redirect_is_not_cached(self):
        assert "no-store" in start()["headers"]["Cache-Control"]


class TestCallback:
    def test_it_creates_an_account_on_first_sign_in(self, repo):
        response = callback(repo)

        assert response["statusCode"] == 302
        assert repo.find_account_by_provider("google",
                                             "108234598234598234598") is not None

    def test_the_account_it_creates_has_no_password(self, repo):
        # Nothing to guess, and nothing to leak. The column is nullable for this.
        callback(repo)
        account = repo.find_account_by_provider("google", "108234598234598234598")

        assert account["password_hash"] is None

    def test_it_keeps_the_verified_email_and_name(self, repo):
        callback(repo)
        account = repo.find_account_by_provider("google", "108234598234598234598")

        assert account["email"] == "sam@example.com"
        assert account["display_name"] == "Sam Okafor"

    def test_signing_in_twice_does_not_make_a_second_account(self, repo):
        callback(repo)
        callback(repo)

        assert len(repo._accounts) == 1

    def test_a_second_person_with_a_taken_name_still_gets_an_account(self, repo):
        callback(repo)
        callback(repo, id_token=make_id_token(sub="99", email="sam@other.com"))

        assert len(repo._accounts) == 2
        assert repo.find_account_by_provider("google", "99") is not None

    def test_the_callback_creates_no_session(self, repo):
        # The reason this endpoint hands back a one-time code instead of a
        # token: whatever it returns ends up in a URL. See the module docstring.
        response = callback(repo)

        assert repo._sessions == {}
        assert "token" not in response["headers"]["Location"].lower()

    def test_it_sends_the_one_time_code_in_the_fragment(self, repo):
        # A fragment is not sent to the server on the next hop and does not
        # reach access logs. A query string is both.
        location = urlsplit(callback(repo)["headers"]["Location"])

        assert location.query == ""
        assert "code=" in location.fragment

    def test_a_forged_state_creates_nothing(self, repo):
        response = callback(repo, state="forged.state")

        assert response["statusCode"] == 400
        assert repo._accounts == {}

    def test_an_expired_state_creates_nothing(self, repo):
        response = callback(repo, now=NOW + oauth.STATE_TTL + timedelta(days=1))

        assert response["statusCode"] == 400
        assert repo._accounts == {}

    def test_a_cancelled_sign_in_goes_back_to_the_form(self, repo):
        # Google sends ?error=access_denied when the person clicks Cancel. That
        # is not a failure worth a stack trace or a dead end.
        response = callback(repo, query={"error": "access_denied"})

        assert response["statusCode"] == 302
        assert "/register/" in response["headers"]["Location"]
        assert repo._accounts == {}


class TestComplete:
    def test_it_trades_the_code_for_a_session(self, repo):
        code = handoff_code(callback(repo))

        body = json.loads(complete(repo, code)["body"])

        assert len(body["token"]) >= 32
        assert repo.account_for_session(
            token_fingerprint(body["token"]), now=NOW) is not None

    def test_a_fresh_account_still_has_no_business(self, repo):
        # Same as /register: OAuth gets you an account, onboarding gets you a
        # business. The page uses this to decide where to send the owner.
        code = handoff_code(callback(repo))

        assert json.loads(complete(repo, code)["body"])["business_id"] is None

    def test_the_code_works_exactly_once(self, repo):
        code = handoff_code(callback(repo))
        complete(repo, code)

        assert complete(repo, code)["statusCode"] == 401

    def test_an_expired_code_is_refused(self, repo):
        code = handoff_code(callback(repo))

        response = complete(repo, code,
                            now=NOW + oauth.HANDOFF_TTL + timedelta(seconds=1))

        assert response["statusCode"] == 401
        assert repo._sessions == {}

    def test_an_unknown_code_is_refused(self, repo):
        assert complete(repo, "never-issued")["statusCode"] == 401

    def test_it_reports_the_business_once_onboarding_attached_one(self, repo):
        first = handoff_code(callback(repo))
        complete(repo, first)
        account = repo.find_account_by_provider("google", "108234598234598234598")
        business_id = repo.create_business(name="Cafe", category="restaurant_cafe")
        repo.attach_business(account["id"], business_id=business_id)

        second = handoff_code(callback(repo))

        assert json.loads(
            complete(repo, second)["body"])["business_id"] == business_id


class TestDispatch:
    """One Lambda serves all three routes, so the path is load-bearing."""

    @pytest.mark.parametrize("event,expected", [
        ({"rawPath": "/v1/auth/google/start"}, "/v1/auth/google/start"),
        ({"rawPath": "/v1/auth/google/start/"}, "/v1/auth/google/start/"),
        ({"requestContext": {"http": {"path": "/v1/auth/google/callback"}}},
         "/v1/auth/google/callback"),
        ({}, ""),
        (None, ""),
    ])
    def test_it_finds_the_path_in_either_shape(self, event, expected):
        assert oauth_google.request_path(event) == expected

    def test_an_unrouted_path_is_a_404_not_a_crash(self):
        response = oauth_google.handler({"rawPath": "/v1/auth/google/nope"})

        assert response["statusCode"] == 404


class TestPasswordLoginAgainstAnOAuthAccount:
    def test_no_password_signs_in_an_account_that_has_none(self, repo):
        """A null password hash must never verify.

        `verify_password` already returns False for anything malformed, so this
        is defence in depth rather than a fix — but the failure it guards is
        total account takeover, which is worth a test that says so.
        """
        from brasstacks.handlers import login as login_handler

        callback(repo)
        username = repo.find_account_by_provider(
            "google", "108234598234598234598")["username"]

        for attempt in ["", "password", "None", "null"]:
            response = login_handler.login(
                {"body": json.dumps({"username": username,
                                     "password": attempt})},
                repo=repo, now=NOW)

            assert response["statusCode"] in (400, 401)
        assert repo._sessions == {}

    def test_it_still_pays_for_the_hash_it_does_not_have(self, repo, monkeypatch):
        """A null password hash must cost the same as a real one.

        `verify_password` refuses a null hash instantly, and instantly is a
        measurable difference — it would make "this username signs in with
        Google" readable from outside. The decoy hash covers the case.
        """
        from brasstacks.handlers import login as login_handler

        callback(repo)
        username = repo.find_account_by_provider(
            "google", "108234598234598234598")["username"]

        seen = []
        monkeypatch.setattr(
            login_handler, "verify_password",
            lambda password, stored: seen.append(stored) or False)

        login_handler.login(
            {"body": json.dumps({"username": username, "password": "x"})},
            repo=repo, now=NOW)

        assert seen and seen[0] is not None
        assert seen[0].startswith("scrypt$")
