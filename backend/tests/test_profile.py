"""Editable owner profiles are tenant-scoped durable memory, not prompt text."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from brasstacks.auth import issue_session_token
from brasstacks.handlers.profile import read_profile, update_profile
from brasstacks.profile import ProfileError, normalise_profile
from brasstacks.providers import FakeEmbedder
from brasstacks.repository import InMemoryRepository

NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def _setup(*, email="peter.flp.2006@gmail.com"):
    repo = InMemoryRepository()
    business_id = repo.create_business(
        name="Rosa's Trattoria", category="restaurant", city="Columbus, OH"
    )
    account_id = repo.create_account(
        business_id, username="rosa", password_hash="not-used"
    )
    repo.update_account_profile(account_id, display_name="Rosa", email=email)
    repo.update_business_profile(
        business_id,
        name="Rosa's Trattoria",
        category="restaurant",
        city="Columbus, OH",
        profile_data={
            "version": 2,
            "business": {"categoryLabel": "Restaurant or café", "website": None},
            "buyers": {"segments": ["families"], "offers": ["pasta"], "channels": ["Google"]},
            "objective": "fill weekday lunch seats",
        },
    )
    token, fingerprint, expires = issue_session_token(now=NOW)
    repo.create_session(
        fingerprint, business_id=business_id,
        account_id=account_id, expires_at=expires,
    )
    event = {"headers": {"Authorization": f"Bearer {token}"}}
    return repo, business_id, account_id, event


def _profile(email="owner@example.com"):
    return {
        "owner": {"name": "Peter Pan", "email": email},
        "business": {
            "name": "Rosa's Trattoria",
            "category": "restaurant_cafe",
            "categoryLabel": "Restaurant or café",
            "location": "Columbus, OH",
            "website": "rosas.example.com",
        },
        "buyers": {
            "segments": ["families", "office workers"],
            "offers": ["pasta", "private events"],
            "channels": ["Google", "walk-ins"],
        },
        "objective": "grow weekday lunch",
        "ownerRules": ["Ask before publishing."],
    }


def test_profile_validation_normalises_contact_and_lists():
    profile = normalise_profile(_profile("  Peter.FLP.2006@GMAIL.COM  "))

    assert profile["owner"]["email"] == "peter.flp.2006@gmail.com"
    assert profile["business"]["website"] == "https://rosas.example.com"
    assert profile["buyers"]["segments"] == ["families", "office workers"]


def test_profile_validation_rejects_bad_email():
    try:
        normalise_profile(_profile("not-an-email"))
    except ProfileError as exc:
        assert "email" in str(exc).lower()
    else:  # pragma: no cover - a failed test is clearer than pytest.raises here
        raise AssertionError("invalid email was accepted")


def test_get_returns_only_the_signed_in_owners_profile():
    repo, business_id, account_id, event = _setup()

    response = read_profile(event, repo=repo, now=NOW)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["profile"]["business"]["id"] == business_id
    assert body["profile"]["owner"]["email"] == "peter.flp.2006@gmail.com"
    assert body["modelTokens"] == 0


def test_update_persists_email_and_refreshes_only_managed_profile_facts():
    repo, business_id, account_id, event = _setup()
    # A fact learned from owner chat must survive an ordinary profile edit.
    unrelated = repo.insert_business_fact(
        business_id,
        fact="The owner cannot add weekend staff.",
        source="owner_chat",
        embedding=[1.0] + [0.0] * 1023,
    )
    payload = _profile("peter.flp.2006@gmail.com")
    event = {**event, "body": json.dumps(payload)}
    embedder = FakeEmbedder()

    response = update_profile(event, repo=repo, embedder=embedder, now=NOW)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert repo.find_account("rosa")["email"] == "peter.flp.2006@gmail.com"
    assert body["profile"]["business"]["name"] == "Rosa's Trattoria"
    assert body["factsStored"] == len(embedder.embedded)
    assert all("peter.flp.2006@gmail.com" not in fact for fact in embedder.embedded)
    assert repo._facts[unrelated]["superseded_by"] is None
    assert any(row.get("profile_managed") for row in repo._facts.values())


def test_update_cannot_cross_tenants():
    repo, own_business, _, event = _setup()
    other = repo.create_business(name="Other", category="retail", city="Boston")
    repo.create_account(other, username="other", password_hash="x")
    before = repo.get_business(other)
    event = {**event, "body": json.dumps(_profile("owner@example.com"))}

    response = update_profile(
        event, repo=repo, embedder=FakeEmbedder(),
        now=NOW, geocoder=None,
    )

    assert response["statusCode"] == 200
    assert repo.get_business(own_business)["name"] == "Rosa's Trattoria"
    assert repo.get_business(other) == before
    assert repo.find_account("other")["email"] is None


def test_profile_requires_a_valid_session():
    repo, *_ = _setup()
    response = read_profile({"headers": {}}, repo=repo, now=NOW)
    assert response["statusCode"] == 401
