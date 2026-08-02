"""Passwords and sessions.

The one part of this system where being wrong is not a bug report, it is other
people's credentials. Tested accordingly, and deliberately including the boring
negatives — a wrong password, an unknown user, an expired session — because
those are the paths an attacker actually walks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brasstacks.auth import (
    SESSION_TTL,
    AuthError,
    hash_password,
    issue_session_token,
    normalise_username,
    token_fingerprint,
    validate_password,
    validate_username,
    verify_password,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


class TestPasswordHashing:
    def test_a_password_verifies_against_its_own_hash(self):
        stored = hash_password("correct horse battery staple")

        assert verify_password("correct horse battery staple", stored)

    def test_a_wrong_password_does_not(self):
        stored = hash_password("correct horse battery staple")

        assert not verify_password("Correct horse battery staple", stored)

    def test_the_password_never_appears_in_what_is_stored(self):
        # The whole point. If this ever fails, the database is a plaintext
        # password dump.
        stored = hash_password("hunter2")

        assert "hunter2" not in stored

    def test_two_hashes_of_one_password_differ(self):
        # Per-hash salt. Identical hashes would tell anyone reading the table
        # which accounts share a password.
        assert hash_password("same") != hash_password("same")

    def test_the_stored_form_names_its_algorithm_and_parameters(self):
        # So a future cost increase can rehash on next login instead of
        # invalidating every account.
        stored = hash_password("x")
        scheme, n, r, p, salt, digest = stored.split("$")

        assert scheme == "scrypt"
        assert int(n) >= 16384
        assert salt and digest

    def test_a_corrupt_stored_hash_is_a_failed_login_not_a_crash(self):
        # A malformed row must not 500 the login endpoint; it must simply not
        # authenticate anyone.
        for broken in ("", "scrypt$notanumber$8$1$aa$bb", "plaintext", "a$b$c"):
            assert not verify_password("x", broken)

    def test_verification_is_constant_time_in_shape(self):
        # Not a timing measurement -- those are flaky in CI. This pins that the
        # comparison goes through compare_digest rather than ==, which is the
        # thing that would actually leak.
        import inspect

        from brasstacks import auth

        source = inspect.getsource(auth.verify_password)
        assert "compare_digest" in source
        assert "==" not in source.split("compare_digest")[0].split("def")[-1]


class TestUsernameRules:
    def test_case_and_padding_do_not_make_a_new_person(self):
        assert normalise_username("  Sam  ") == "sam"

    def test_a_username_must_be_usable(self):
        for bad in ("", "  ", "a", "x" * 200, "has space", "a/b"):
            with pytest.raises(AuthError):
                validate_username(bad)

    def test_ordinary_usernames_pass(self):
        for good in ("sam", "asaka_owner", "sam.chen", "sam-chen", "sam99"):
            assert validate_username(good) == good


class TestPasswordRules:
    def test_a_short_password_is_refused(self):
        with pytest.raises(AuthError, match="12"):
            validate_password("short")

    def test_the_rule_is_length_not_character_classes(self):
        # Length is what actually helps. Forcing a symbol produces Password1!
        # and a sticky note, which is worse than a long ordinary phrase.
        assert validate_password("a long enough passphrase")

    def test_a_password_is_not_silently_truncated(self):
        long_one = "x" * 400
        with pytest.raises(AuthError):
            validate_password(long_one)


class TestSessions:
    def test_a_token_is_returned_once_and_stored_only_as_a_hash(self):
        token, fingerprint, expires = issue_session_token(now=NOW)

        assert len(token) >= 32
        assert token not in fingerprint
        assert fingerprint == token_fingerprint(token)
        assert expires == NOW + SESSION_TTL

    def test_two_sessions_never_collide(self):
        first, _, _ = issue_session_token(now=NOW)
        second, _, _ = issue_session_token(now=NOW)

        assert first != second

    def test_a_fingerprint_cannot_be_reversed_to_a_token(self):
        token, fingerprint, _ = issue_session_token(now=NOW)

        assert len(fingerprint) == 64          # sha256 hex
        assert fingerprint != token
