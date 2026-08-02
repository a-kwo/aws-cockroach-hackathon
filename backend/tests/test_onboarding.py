"""Signing a real business up, and pointing the agents at its street.

Two things happen here that happen nowhere else in the system: a stranger's
input creates rows, and a typed address decides where money gets spent looking
for competitors. Both are tested accordingly.

The invite code is not security theatre. `/ask` is throttled but can only read;
this endpoint writes to the database and spends Bedrock embedding calls per
signup, from a URL printed in a public repository.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)

from brasstacks.onboarding import (
    GEOCODE_URL,
    GeocodeResult,
    PlacesGeocoder,
    build_profile_facts,
    geocode_or_none,
)

GEOCODE_BODY = {
    "places": [{
        "id": "ChIJ_asaka",
        "formattedAddress": "1 Capitol Sq, Columbus, OH 43215, USA",
        "location": {"latitude": 39.9612, "longitude": -82.9988},
        "displayName": {"text": "Asaka"},
    }],
}


class StubHttp:
    def __init__(self, body=GEOCODE_BODY, fail=False):
        self._body, self._fail = body, fail
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._fail:
            raise RuntimeError("places is down")
        return StubResponse(self._body)


class StubResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

class TestGeocoder:
    def test_turns_a_typed_address_into_coordinates(self):
        http = StubHttp()
        geocoder = PlacesGeocoder(api_key="k", client=http)

        result = geocoder.locate("Asaka, Columbus OH")

        assert result == GeocodeResult(
            latitude=39.9612, longitude=-82.9988,
            formatted="1 Capitol Sq, Columbus, OH 43215, USA",
            matched_name="Asaka", place_id="ChIJ_asaka")

    def test_uses_text_search_rather_than_the_geocoding_api(self):
        # The Geocoding API is a separate Google product and is not enabled on
        # this project; Places is. Text Search also finds the *business* rather
        # than an address, so the coordinates are the business's own.
        http = StubHttp()
        PlacesGeocoder(api_key="k", client=http).locate("Asaka, Columbus OH")

        assert http.calls[0]["url"] == GEOCODE_URL
        assert http.calls[0]["json"]["textQuery"] == "Asaka, Columbus OH"
        assert http.calls[0]["headers"]["X-Goog-Api-Key"] == "k"

    def test_an_address_google_cannot_place_is_not_a_crash(self):
        http = StubHttp(body={"places": []})

        assert PlacesGeocoder(api_key="k", client=http).locate("asdfgh") is None

    def test_geocode_or_none_absorbs_an_outage(self):
        # A signup must not fail because Google is down. The business is created
        # without coordinates and the scout simply finds nobody until it has
        # them — losing competitor context, never the tenant.
        geocoder = PlacesGeocoder(api_key="k", client=StubHttp(fail=True))

        assert geocode_or_none(geocoder, "Columbus, OH") is None

    def test_no_geocoder_configured_is_not_an_error(self):
        assert geocode_or_none(None, "Columbus, OH") is None


# ---------------------------------------------------------------------------
# The profile, turned into retrievable memory
# ---------------------------------------------------------------------------

class TestFactSourceMatchesTheSchema:
    """The gap that let a 500 reach production.

    `business_fact.source` is a database ENUM. InMemoryRepository stores any
    string happily, so the whole unit suite passed while the deployed function
    failed on `invalid input value for enum fact_source: "onboarding"`. Reading
    the enum out of schema.sql is the only version of this test that could have
    caught it.
    """

    def _schema_enum(self):
        import re
        from pathlib import Path

        from brasstacks.config import REPO_ROOT

        sql = (Path(REPO_ROOT) / "db" / "schema.sql").read_text(encoding="utf-8")
        block = re.search(
            r"CREATE TYPE IF NOT EXISTS fact_source AS ENUM\s*\((.*?)\)",
            sql, re.S).group(1)
        return set(re.findall(r"'([a-z_]+)'", block))

    def test_the_source_we_write_is_a_value_the_database_accepts(self):
        from brasstacks.onboarding import FACT_SOURCE

        assert FACT_SOURCE in self._schema_enum()


class TestProfileFacts:
    PROFILE = {
        "owner": {"name": "Sam", "email": "sam@example.com"},
        "business": {"name": "Nonna's", "category": "restaurant",
                     "categoryLabel": "Restaurant or café",
                     "location": "Columbus, OH", "website": "https://nonnas.test"},
        "buyers": {"segments": ["families", "office workers"],
                   "offers": ["wood-fired pizza", "weekend brunch"],
                   "channels": ["walk-in", "instagram"]},
        "objective": "fill midweek tables",
    }

    def test_every_answer_becomes_a_retrievable_fact(self):
        facts = build_profile_facts(self.PROFILE)
        joined = " ".join(facts)

        assert "families" in joined
        assert "wood-fired pizza" in joined
        assert "fill midweek tables" in joined
        assert "instagram" in joined

    def test_facts_are_sentences_not_key_value_dumps(self):
        # They are embedded and retrieved by cosine similarity against
        # hypothesis-shaped queries. "segments: families" retrieves nothing.
        for fact in build_profile_facts(self.PROFILE):
            assert fact[0].isupper(), fact
            assert fact.endswith("."), fact
            # "segments: families" is the shape to avoid. A colon inside a URL
            # is fine — it is the "key: value" form that retrieves nothing.
            assert ": " not in fact, fact

    def test_an_empty_section_produces_no_fact(self):
        thin = {"business": {"name": "Nonna's", "category": "restaurant"},
                "buyers": {"segments": [], "offers": [], "channels": []},
                "objective": ""}

        facts = build_profile_facts(thin)

        assert all(f.strip() for f in facts)
        assert not any("nothing" in f.lower() for f in facts)

    def test_the_owners_email_never_becomes_a_fact(self):
        # Facts are embedded into a vector index and surface in prompts. An
        # email address has no analytical value and every privacy cost.
        joined = " ".join(build_profile_facts(self.PROFILE))

        assert "sam@example.com" not in joined


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------

def an_event(profile, *, code="let-me-in"):
    body = dict(profile)
    if code is not None:
        body["inviteCode"] = code
    return {"body": json.dumps(body)}


@pytest.fixture
def handler_env(monkeypatch):
    monkeypatch.setenv("BRASSTACKS_INVITE_CODE", "let-me-in")
    return monkeypatch


class TestHandler:
    PROFILE = TestProfileFacts.PROFILE

    def _call(self, event, repo=None, geocoder=None):
        """Onboarding is step two now: it requires the session /register issued."""
        from brasstacks.auth import hash_password, issue_session_token
        from brasstacks.handlers import onboarding
        from brasstacks.providers import FakeEmbedder
        from brasstacks.repository import InMemoryRepository

        repo = repo if repo is not None else InMemoryRepository()
        account = repo.create_account(None, username="sam",
                                      password_hash=hash_password("x" * 20))
        token, fingerprint, expires = issue_session_token(now=NOW)
        repo.create_session(fingerprint, business_id=None,
                            account_id=account, expires_at=expires)
        event = dict(event)
        event["headers"] = {"Authorization": f"Bearer {token}"}
        return onboarding.onboard(
            event,
            repo=repo,
            embedder=FakeEmbedder(),
            geocoder=geocoder,
            now=NOW,
        )

    def test_creates_the_business_and_returns_its_id(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        response = self._call(an_event(self.PROFILE), repo=repo)
        body = json.loads(response["body"])

        assert response["statusCode"] == 201
        assert body["business_id"] in repo._businesses
        assert repo._businesses[body["business_id"]]["name"] == "Nonna's"

    def test_the_profile_becomes_embedded_facts(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        self._call(an_event(self.PROFILE), repo=repo)

        assert len(repo._facts) >= 4

    def test_the_owners_rule_is_written(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        profile = dict(self.PROFILE, ownerRules=["Never discount below cost."])
        self._call(an_event(profile), repo=repo)

        assert any("discount" in r.rule for r in repo.get_owner_rules(
            list(repo._businesses)[0]))

    def test_the_business_is_located_by_name_and_address_together(self):
        # Text Search resolves a business best when given both. The address
        # alone lands on a building; the name alone is ambiguous. Now that the
        # form has them as separate fields, the query joins them here rather
        # than relying on the owner to type both into one box.
        seen = []

        class Spy:
            def locate(self, address):
                seen.append(address)
                return None

        self._call(an_event(self.PROFILE), geocoder=Spy())

        assert seen == ["Nonna's, Columbus, OH"]

    def test_a_geocoded_address_lands_on_the_business(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        geocoder = PlacesGeocoder(api_key="k", client=StubHttp())
        body = json.loads(self._call(an_event(self.PROFILE), repo=repo,
                                     geocoder=geocoder)["body"])

        stored = repo._businesses[body["business_id"]]
        assert stored["latitude"] == 39.9612
        assert stored["longitude"] == -82.9988

    def test_a_business_with_no_name_is_rejected(self):
        thin = dict(self.PROFILE,
                    business=dict(self.PROFILE["business"], name="  "))
        response = self._call(an_event(thin))

        assert response["statusCode"] == 400
        assert "name" in json.loads(response["body"])["error"].lower()

    def test_a_body_that_is_not_json_is_the_callers_fault(self):
        response = self._call({"body": "not json"})

        assert response["statusCode"] == 400

    def test_a_failed_write_is_not_swallowed_into_a_half_built_tenant(self):
        # The handler runs inside a transaction, so a database error must
        # propagate for the rollback to happen. Swallowing it here would leave
        # a business row with no facts and no rules -- which is exactly what
        # the first live signup left behind.
        from brasstacks.repository import InMemoryRepository

        class Breaks(InMemoryRepository):
            def insert_business_fact(self, *a, **k):
                raise RuntimeError("invalid input value for enum fact_source")

        with pytest.raises(RuntimeError):
            self._call(an_event(self.PROFILE), repo=Breaks())

    def test_the_response_allows_the_hosted_site_to_read_it(self):
        # The board is served from CloudFront and the API from API Gateway, so
        # without CORS this fails in a browser and works in curl.
        response = self._call(an_event(self.PROFILE))

        assert response["headers"]["Access-Control-Allow-Origin"] == "*"


class TestOnboardingRequiresASession:
    """Step two of signup. Reachable only by someone /register signed in."""

    PROFILE = TestProfileFacts.PROFILE

    def _bare(self, event, repo):
        from brasstacks.handlers import onboarding
        from brasstacks.providers import FakeEmbedder

        return onboarding.onboard(event, repo=repo, embedder=FakeEmbedder(),
                                  now=NOW)

    def test_no_token_creates_nothing(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        response = self._bare(an_event(self.PROFILE), repo)

        assert response["statusCode"] == 401
        assert repo._businesses == {}

    def test_an_unknown_token_creates_nothing(self):
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        event = dict(an_event(self.PROFILE))
        event["headers"] = {"Authorization": "Bearer not-a-real-token"}

        assert self._bare(event, repo)["statusCode"] == 401
        assert repo._businesses == {}

    def test_the_business_is_attached_to_the_signed_in_account(self):
        from brasstacks.auth import hash_password, issue_session_token
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        account = repo.create_account(None, username="sam",
                                      password_hash=hash_password("x" * 20))
        token, fingerprint, expires = issue_session_token(now=NOW)
        repo.create_session(fingerprint, business_id=None,
                            account_id=account, expires_at=expires)
        event = dict(an_event(self.PROFILE))
        event["headers"] = {"Authorization": f"Bearer {token}"}

        body = json.loads(self._bare(event, repo)["body"])

        assert repo.find_account("sam")["business_id"] == body["business_id"]
        # The live session moves with it, so the board loads signed in.
        assert repo.business_for_session(fingerprint, now=NOW) == body["business_id"]

    def test_a_second_workspace_on_one_account_is_refused(self):
        # Reloading the finished form would otherwise create a second tenant
        # and orphan the first.
        from brasstacks.auth import hash_password, issue_session_token
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        existing = repo.create_business(name="Already", category="restaurant")
        account = repo.create_account(existing, username="sam",
                                      password_hash=hash_password("x" * 20))
        token, fingerprint, expires = issue_session_token(now=NOW)
        repo.create_session(fingerprint, business_id=existing,
                            account_id=account, expires_at=expires)
        event = dict(an_event(self.PROFILE))
        event["headers"] = {"Authorization": f"Bearer {token}"}

        assert self._bare(event, repo)["statusCode"] == 409
        assert len(repo._businesses) == 1
