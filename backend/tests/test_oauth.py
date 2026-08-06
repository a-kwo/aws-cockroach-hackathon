"""Sign in with Google, tested without a Google.

Everything here runs offline: the token endpoint is injected and the ID token is
built by hand. What these tests pin is the part that is actually ours to get
wrong — the state we hand Google and then trust back, the claims we refuse, and
the username we have to invent for someone who never chose one.

The signature on the ID token is deliberately *not* checked by the code under
test, and that is safe only because of where the token comes from. See the note
on `identity_from_id_token`. `test_a_token_minted_for_another_app_is_refused` is
the test that keeps the rest of that argument honest.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from brasstacks import oauth
from brasstacks.auth import validate_username

NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
SECRET = "a state signing secret"
CLIENT_ID = "1234.apps.googleusercontent.com"


def make_id_token(**claims) -> str:
    """A Google ID token with a plausible claim set and a junk signature.

    The signature is junk on purpose: nothing in this flow reads it, and a test
    that supplied a real one would be asserting something the code does not do.
    """
    body = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "108234598234598234598",
        "email": "sam@example.com",
        "email_verified": True,
        "name": "Sam Okafor",
        "exp": int((NOW + timedelta(minutes=30)).timestamp()),
    }
    body.update(claims)
    for empty in [key for key, value in body.items() if value is _ABSENT]:
        del body[empty]
    segment = base64.urlsafe_b64encode(
        json.dumps(body).encode("utf-8")).decode("ascii").rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{segment}.not-a-real-signature"


_ABSENT = object()


class TestState:
    """The state parameter is the only thing that survives the round trip to
    Google, so it is the only place the invite decision can be carried."""

    def test_a_signed_state_verifies_and_returns_its_claims(self):
        state = oauth.sign_state({"invited": True}, secret=SECRET, now=NOW)

        assert oauth.verify_state(
            state, secret=SECRET, now=NOW)["invited"] is True

    def test_a_tampered_payload_is_refused(self):
        # The whole point. If this passed, anyone could flip `invited` and walk
        # through the gate that exists to cap the nightly bill.
        state = oauth.sign_state({"invited": False}, secret=SECRET, now=NOW)
        forged = base64.urlsafe_b64encode(
            json.dumps({"invited": True, "iat": int(NOW.timestamp())}).encode()
        ).decode().rstrip("=")

        with pytest.raises(oauth.OAuthError):
            oauth.verify_state(f"{forged}.{state.split('.')[1]}",
                               secret=SECRET, now=NOW)

    def test_a_state_signed_with_another_secret_is_refused(self):
        state = oauth.sign_state({"invited": True}, secret="someone else's",
                                 now=NOW)

        with pytest.raises(oauth.OAuthError):
            oauth.verify_state(state, secret=SECRET, now=NOW)

    def test_an_expired_state_is_refused(self):
        state = oauth.sign_state({"invited": True}, secret=SECRET, now=NOW)

        with pytest.raises(oauth.OAuthError):
            oauth.verify_state(state, secret=SECRET,
                               now=NOW + oauth.STATE_TTL + timedelta(seconds=1))

    def test_a_state_is_still_good_just_inside_its_window(self):
        state = oauth.sign_state({"invited": True}, secret=SECRET, now=NOW)

        assert oauth.verify_state(
            state, secret=SECRET,
            now=NOW + oauth.STATE_TTL - timedelta(seconds=1))["invited"] is True

    @pytest.mark.parametrize("junk", ["", "no-dot", "a.b.c", "...", None, 7])
    def test_malformed_state_is_refused_rather_than_crashing(self, junk):
        with pytest.raises(oauth.OAuthError):
            oauth.verify_state(junk, secret=SECRET, now=NOW)

    def test_two_states_for_the_same_claims_differ(self):
        # A nonce, so the state is single-use in practice and two tabs mid-signin
        # cannot be confused for one another.
        first = oauth.sign_state({"invited": True}, secret=SECRET, now=NOW)
        second = oauth.sign_state({"invited": True}, secret=SECRET, now=NOW)

        assert first != second


class TestAuthorizeUrl:
    def test_it_points_at_google_and_carries_our_parameters(self):
        url = oauth.build_authorize_url(
            client_id=CLIENT_ID, redirect_uri="https://api.example/callback",
            state="signed-state")

        assert url.startswith(oauth.AUTHORIZE_ENDPOINT)
        assert "client_id=1234.apps.googleusercontent.com" in url
        assert "state=signed-state" in url
        assert "response_type=code" in url

    def test_it_asks_for_no_more_than_identity(self):
        # Scope creep here is a permissions dialog the owner has to read and a
        # token that can do more than sign someone in.
        url = oauth.build_authorize_url(
            client_id=CLIENT_ID, redirect_uri="https://api.example/callback",
            state="s")

        assert "scope=openid+email+profile" in url or "openid%20email%20profile" in url


class TestIdentity:
    def test_a_well_formed_token_yields_the_person(self):
        identity = oauth.identity_from_id_token(
            make_id_token(), client_id=CLIENT_ID, now=NOW)

        assert identity.subject == "108234598234598234598"
        assert identity.email == "sam@example.com"
        assert identity.display_name == "Sam Okafor"

    def test_a_token_minted_for_another_app_is_refused(self):
        # Without a signature check, the audience check is what stops a token
        # obtained by any other Google app from signing someone in here.
        with pytest.raises(oauth.OAuthError):
            oauth.identity_from_id_token(
                make_id_token(aud="9999.apps.googleusercontent.com"),
                client_id=CLIENT_ID, now=NOW)

    def test_a_token_from_another_issuer_is_refused(self):
        with pytest.raises(oauth.OAuthError):
            oauth.identity_from_id_token(
                make_id_token(iss="https://evil.example"),
                client_id=CLIENT_ID, now=NOW)

    def test_an_expired_token_is_refused(self):
        with pytest.raises(oauth.OAuthError):
            oauth.identity_from_id_token(
                make_id_token(), client_id=CLIENT_ID,
                now=NOW + timedelta(hours=2))

    def test_a_token_with_no_subject_is_refused(self):
        # The subject is the account key. Without it there is nothing stable to
        # attach the account to, and email is not a substitute — people change it.
        with pytest.raises(oauth.OAuthError):
            oauth.identity_from_id_token(
                make_id_token(sub=""), client_id=CLIENT_ID, now=NOW)

    def test_an_unverified_email_is_refused(self):
        # Google will hand out a token for an address the person has not proved
        # they own. Accepting it would let someone sign up as anybody.
        with pytest.raises(oauth.OAuthError):
            oauth.identity_from_id_token(
                make_id_token(email_verified=False), client_id=CLIENT_ID,
                now=NOW)

    @pytest.mark.parametrize("junk", ["", "one-part", "not.base64!!.sig", None])
    def test_a_malformed_token_is_refused_rather_than_crashing(self, junk):
        with pytest.raises(oauth.OAuthError):
            oauth.identity_from_id_token(junk, client_id=CLIENT_ID, now=NOW)


class TestUsernames:
    def test_it_derives_from_the_email_local_part(self):
        assert oauth.suggest_username(
            "Sam.Okafor@example.com", subject="1", taken=lambda _: False
        ) == "sam.okafor"

    def test_a_taken_username_gets_a_suffix(self):
        used = {"sam.okafor"}

        assert oauth.suggest_username(
            "sam.okafor@example.com", subject="1", taken=used.__contains__
        ) == "sam.okafor-2"

    def test_it_keeps_counting_past_the_first_collision(self):
        used = {"sam.okafor", "sam.okafor-2", "sam.okafor-3"}

        assert oauth.suggest_username(
            "sam.okafor@example.com", subject="1", taken=used.__contains__
        ) == "sam.okafor-4"

    def test_an_unusable_local_part_falls_back_to_the_subject(self):
        name = oauth.suggest_username(
            "!!!@example.com", subject="10823459", taken=lambda _: False)

        assert "10823459" in name

    def test_a_missing_email_still_produces_a_username(self):
        name = oauth.suggest_username(None, subject="10823459",
                                      taken=lambda _: False)

        assert validate_username(name) == name

    @pytest.mark.parametrize("email", [
        "sam@example.com", "a@example.com", "Sam.O'Kafor+tag@example.com",
        "x" * 200 + "@example.com", "!!!@example.com", "", None,
    ])
    def test_every_derived_username_is_one_the_rest_of_the_system_accepts(
            self, email):
        # Derived names bypass the signup form, so nothing else validates them.
        name = oauth.suggest_username(email, subject="99", taken=lambda _: False)

        assert validate_username(name) == name


class TestCodeExchange:
    def test_it_posts_the_authorization_code_to_google(self):
        seen = {}

        def transport(url, form):
            seen["url"], seen["form"] = url, form
            return {"id_token": make_id_token()}

        oauth.exchange_code(
            "the-code", client_id=CLIENT_ID, client_secret="shh",
            redirect_uri="https://api.example/callback", transport=transport)

        assert seen["url"] == oauth.TOKEN_ENDPOINT
        assert seen["form"]["code"] == "the-code"
        assert seen["form"]["grant_type"] == "authorization_code"
        assert seen["form"]["client_secret"] == "shh"

    def test_a_response_without_an_id_token_is_an_error(self):
        with pytest.raises(oauth.OAuthError):
            oauth.exchange_code(
                "the-code", client_id=CLIENT_ID, client_secret="shh",
                redirect_uri="https://api.example/callback",
                transport=lambda url, form: {"error": "invalid_grant"})
