"""The endpoint an owner reports a measured outcome through.

It is the only writer of the only table that can produce a verified verdict, so
the refusals matter as much as the happy path:

  * the business is taken from the session, never from the request — a tenant id
    in the body would let anyone write measurements onto anyone's ledger;
  * money arrives as a decimal string and is parsed to integer cents here, so
    the browser never does arithmetic on money;
  * a figure for a find the caller does not own is refused with the same message
    as one that does not exist.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.auth import token_fingerprint
from brasstacks.handlers.outcome import (
    parse_amount_cents,
    record_outcome,
    respond,
)
from brasstacks.repository import EvidenceRef, InMemoryRepository

NOW = datetime(2026, 8, 7, 19, 4, 5, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023


def owner_repo(*, status="accepted"):
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Yellow Cow", category="restaurant")
    account_id = repo.create_account(business_id, username="owner",
                                     password_hash="not-used")
    token = "owner-session-token"
    repo.create_session(token_fingerprint(token), business_id=business_id,
                        account_id=account_id, expires_at=NOW + timedelta(days=1))
    observation_id = repo.insert_observation(
        business_id, content="Lunch demand nearby", kind="review",
        embedding=VECTOR, observed_at=NOW - timedelta(days=20))
    find_id = repo.insert_find_with_evidence(
        business_id, title="Weekday lunch set", rationale="r", move="m",
        emoji="x", predicted_daily_cents=2300, confidence=0.8,
        verify_after=date(2026, 8, 20), status=status,
        evidence=[EvidenceRef(observation_id, 0.9)])
    return repo, business_id, account_id, token, find_id


def event(token, *, find_id, body):
    return {
        "pathParameters": {"find_id": find_id},
        "headers": {"Authorization": f"Bearer {token}"},
        "body": json.dumps(body),
    }


# ------------------------------------------------------------------ money in

class TestParsingTheAmount:
    """The browser sends what the owner typed. It never sends cents it worked
    out itself, because `12.34 * 100` is 1233.9999999999998 in JavaScript."""

    def test_a_plain_dollar_string(self):
        assert parse_amount_cents({"amount": "300"}) == 30000

    def test_cents_are_exact(self):
        # The float route gives 1233.9999999999998. Decimal gives 1234.
        assert parse_amount_cents({"amount": "12.34"}) == 1234

    def test_typed_currency_and_separators_survive(self):
        assert parse_amount_cents({"amount": "$1,200.75"}) == 120075

    def test_zero_is_a_measurement_not_an_absence(self):
        assert parse_amount_cents({"amount": "0"}) == 0

    def test_a_loss_keeps_its_sign(self):
        assert parse_amount_cents({"amount": "-45.50"}) == -4550

    def test_explicit_cents_are_taken_as_given(self):
        assert parse_amount_cents({"amountCents": 4550}) == 4550

    def test_a_float_of_cents_is_refused(self):
        with pytest.raises(ValueError):
            parse_amount_cents({"amountCents": 45.5})

    def test_a_bool_is_not_an_amount(self):
        with pytest.raises(ValueError):
            parse_amount_cents({"amountCents": True})

    def test_words_are_refused(self):
        with pytest.raises(ValueError):
            parse_amount_cents({"amount": "about three hundred"})

    def test_an_empty_amount_is_refused(self):
        with pytest.raises(ValueError):
            parse_amount_cents({"amount": "   "})

    def test_a_missing_amount_is_refused(self):
        with pytest.raises(ValueError):
            parse_amount_cents({})

    def test_an_absurd_figure_is_refused_rather_than_stored(self):
        # A slipped keyboard should not put $9,999,999,999 on the ledger.
        with pytest.raises(ValueError):
            parse_amount_cents({"amount": "9999999999"})


# ------------------------------------------------------------------ the route

class TestRecordingAnOutcome:
    def test_a_reported_week_is_stored_against_the_session_business(self):
        repo, business_id, account_id, token, find_id = owner_repo()

        response = record_outcome(
            event(token, find_id=find_id,
                  body={"amount": "210", "basis": "week",
                        "note": "Counted from the till."}),
            repo=repo, now=NOW)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["findId"] == find_id
        assert body["amountCents"] == 21000
        assert body["basis"] == "week"
        assert body["dailyCents"] == 3000

        [stored] = repo.find_outcome_reports(business_id)
        assert stored.amount_cents == 21000
        assert stored.daily_cents == 3000
        assert stored.note == "Counted from the till."
        # Who said so, for the audit trail.
        assert stored.reported_by_account_id == account_id

    def test_a_reported_zero_is_stored(self):
        # This is how a miss gets published. It must not be read as "no answer".
        repo, business_id, _, token, find_id = owner_repo()

        response = record_outcome(
            event(token, find_id=find_id, body={"amount": "0", "basis": "week"}),
            repo=repo, now=NOW)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["dailyCents"] == 0
        assert [r.amount_cents for r in repo.find_outcome_reports(business_id)] == [0]

    def test_the_daily_basis_needs_no_conversion(self):
        repo, business_id, _, token, find_id = owner_repo()
        record_outcome(
            event(token, find_id=find_id, body={"amount": "30", "basis": "day"}),
            repo=repo, now=NOW)
        [stored] = repo.find_outcome_reports(business_id)
        assert stored.daily_cents == 3000

    def test_signing_in_is_required(self):
        repo, _, _, _, find_id = owner_repo()
        payload = event("", find_id=find_id,
                        body={"amount": "210", "basis": "week"})
        payload.pop("headers")

        response = record_outcome(payload, repo=repo, now=NOW)
        assert response["statusCode"] == 401

    def test_an_unknown_token_is_refused(self):
        repo, business_id, _, _, find_id = owner_repo()
        response = record_outcome(
            event("not-a-real-token", find_id=find_id,
                  body={"amount": "210", "basis": "week"}),
            repo=repo, now=NOW)

        assert response["statusCode"] == 401
        assert repo.find_outcome_reports(business_id) == []

    def test_an_expired_session_is_refused(self):
        repo, business_id, _, token, find_id = owner_repo()
        response = record_outcome(
            event(token, find_id=find_id,
                  body={"amount": "210", "basis": "week"}),
            repo=repo, now=NOW + timedelta(days=30))

        assert response["statusCode"] == 401
        assert repo.find_outcome_reports(business_id) == []

    def test_another_tenants_find_is_refused_without_confirming_it_exists(self):
        repo, _, _, token, _ = owner_repo()
        rival = repo.create_business(name="Rival", category="restaurant")
        observation_id = repo.insert_observation(
            rival, content="theirs", kind="review", embedding=VECTOR,
            observed_at=NOW)
        theirs = repo.insert_find_with_evidence(
            rival, title="t", rationale="r", move="m", emoji="x",
            predicted_daily_cents=100, confidence=0.5,
            verify_after=date(2026, 8, 20), status="accepted",
            evidence=[EvidenceRef(observation_id, 0.9)])

        response = record_outcome(
            event(token, find_id=theirs,
                  body={"amount": "210", "basis": "week"}),
            repo=repo, now=NOW)

        assert response["statusCode"] == 409
        assert repo.find_outcome_reports(rival) == []

    def test_a_move_the_owner_never_accepted_has_nothing_to_report(self):
        repo, business_id, _, token, find_id = owner_repo(status="proposed")
        response = record_outcome(
            event(token, find_id=find_id,
                  body={"amount": "210", "basis": "week"}),
            repo=repo, now=NOW)

        assert response["statusCode"] == 409
        assert repo.find_outcome_reports(business_id) == []

    def test_an_unknown_period_is_a_bad_request(self):
        repo, business_id, _, token, find_id = owner_repo()
        response = record_outcome(
            event(token, find_id=find_id,
                  body={"amount": "210", "basis": "fortnight"}),
            repo=repo, now=NOW)

        assert response["statusCode"] == 400
        assert repo.find_outcome_reports(business_id) == []

    def test_a_missing_period_is_a_bad_request(self):
        repo, _, _, token, find_id = owner_repo()
        response = record_outcome(
            event(token, find_id=find_id, body={"amount": "210"}),
            repo=repo, now=NOW)
        assert response["statusCode"] == 400

    def test_a_body_that_is_not_json_is_a_bad_request(self):
        repo, _, _, token, find_id = owner_repo()
        payload = event(token, find_id=find_id, body={})
        payload["body"] = "not json"
        assert record_outcome(payload, repo=repo, now=NOW)["statusCode"] == 400

    def test_a_missing_find_id_is_a_bad_request(self):
        repo, _, _, token, _ = owner_repo()
        payload = event(token, find_id="", body={"amount": "1", "basis": "day"})
        payload["pathParameters"] = {}
        assert record_outcome(payload, repo=repo, now=NOW)["statusCode"] == 400

    def test_an_overlong_note_is_refused(self):
        repo, _, _, token, find_id = owner_repo()
        response = record_outcome(
            event(token, find_id=find_id,
                  body={"amount": "210", "basis": "week", "note": "x" * 500}),
            repo=repo, now=NOW)
        assert response["statusCode"] == 400

    def test_a_correction_is_a_new_row_not_an_edit(self):
        repo, business_id, _, token, find_id = owner_repo()
        record_outcome(event(token, find_id=find_id,
                             body={"amount": "210", "basis": "week"}),
                       repo=repo, now=NOW)
        record_outcome(event(token, find_id=find_id,
                             body={"amount": "280", "basis": "week"}),
                       repo=repo, now=NOW + timedelta(hours=1))

        history = repo.find_outcome_history(business_id, find_id=find_id)
        assert [r.amount_cents for r in history] == [28000, 21000]

    def test_nothing_sensitive_or_cacheable_comes_back(self):
        headers = respond(200, {"ok": True})["headers"]
        assert headers["Cache-Control"] == "no-store"
        assert "Authorization" in headers["Access-Control-Allow-Headers"]
