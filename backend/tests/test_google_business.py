"""A real Maker action: owner-confirmed Google Business Profile publishing."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from brasstacks.auth import token_fingerprint
from brasstacks.connections import GOOGLE_BUSINESS_PROVIDER
from brasstacks.google_business import (
    BUSINESS_SCOPE,
    GoogleBusinessError,
    build_authorize_url,
    list_locations,
    publish_post,
)
from brasstacks.handlers.google_business import (
    GoogleBusinessConfig,
    callback,
    connect,
    publish,
    select_location,
    status,
)
from brasstacks.oauth import sign_state
from brasstacks.repository import EvidenceRef, InMemoryRepository

NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023
CONFIG = GoogleBusinessConfig(
    client_id="client-id",
    client_secret="client-secret",
    redirect_uri="https://api.example/v1/integrations/google-business/callback",
    state_secret="state-secret-long-enough",
    frontend_url="https://app.example",
    kms_key_id="alias/brasstacks-google-business",
)


class FakeCipher:
    def __init__(self):
        self.encrypted = []
        self.decrypted = []

    def encrypt(self, plaintext, *, business_id):
        self.encrypted.append((plaintext, business_id))
        return f"cipher:{business_id}:{plaintext}"

    def decrypt(self, ciphertext, *, business_id):
        self.decrypted.append((ciphertext, business_id))
        return ciphertext.rsplit(":", 1)[-1]


class GoogleTransport:
    def __init__(self, *, locations=1, uncertain=False):
        self.locations = locations
        self.uncertain = uncertain
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "oauth2.googleapis.com/token" in url:
            data = kwargs.get("data") or {}
            if data.get("grant_type") == "authorization_code":
                return {"access_token": "access-one", "refresh_token": "refresh-one",
                        "scope": BUSINESS_SCOPE}
            return {"access_token": "access-two"}
        if url.endswith("/v1/accounts"):
            return {"accounts": [{"name": "accounts/123"}]}
        if "/accounts/123/locations?" in url:
            rows = [
                {
                    "name": f"locations/{index}",
                    "title": f"Rosa's Location {index}",
                    "storefrontAddress": {
                        "addressLines": [f"{index} Main St"],
                        "locality": "Oakland", "administrativeArea": "CA",
                    },
                }
                for index in range(1, self.locations + 1)
            ]
            return {"locations": rows}
        if url.endswith("/localPosts"):
            if self.uncertain:
                raise GoogleBusinessError("check profile", uncertain=True)
            return {
                "name": "accounts/123/locations/1/localPosts/abc",
                "createTime": "2026-08-06T15:00:00Z",
                "searchUrl": "https://business.google.com/posts/abc",
                "state": "LIVE",
            }
        raise AssertionError(url)


def owner_repo():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    account_id = repo.create_account(
        business_id, username="owner", password_hash="unused",
    )
    token = "owner-session-token"
    repo.create_session(
        token_fingerprint(token), business_id=business_id,
        account_id=account_id, expires_at=NOW + timedelta(hours=1),
    )
    return repo, business_id, account_id, token


def event(path, token, *, body=None, method="POST", query=None):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": {"Authorization": f"Bearer {token}"},
        "body": json.dumps(body or {}),
        "queryStringParameters": query or {},
    }


def completed_google_post(repo, business_id, account_id):
    obs = repo.insert_observation(
        business_id, content="Customers ask about takeout", kind="review",
        embedding=VECTOR, observed_at=NOW,
    )
    find_id = repo.insert_find_with_evidence(
        business_id, title="Post takeout set", summary="Promote the set",
        rationale="Demand exists", move="Publish a Google Business post", emoji="↗",
        predicted_daily_cents=1600, confidence=.82,
        verify_after=date(2026, 8, 27), status="accepted",
        evidence=[EvidenceRef(obs, .9)], created_at=NOW,
    )
    task = repo.create_or_get_maker_task(
        business_id, find_id=find_id, requested_by_account_id=account_id,
    )
    claim = repo.claim_task(task.task_id, worker_id="maker")
    artifact_id = repo.insert_artifact(
        find_id=find_id, kind="review_reply", title="Takeout post",
        preview="Try our fixed-price takeout set.",
        body="Try our fixed-price takeout set. Order tonight.",
        summary="A ready-to-publish takeout post.",
        review_state="ready_for_review", task_id=task.task_id,
        idempotency_key=f"artifact:{task.task_id}", revision=1,
        metadata={
            "artifact_type": "google_business_post",
            "action_manifest": {
                "version": 1, "action_type": "google_business.publish_post",
                "state": "ready_for_approval", "requires_owner_confirmation": True,
                "content_source": "artifact.body",
            },
        },
    )
    repo.complete_task(
        task.task_id, claim_token=claim.claim_token,
        output_artifact_id=artifact_id,
    )
    return find_id, task.task_id, artifact_id


def connect_record(repo, business_id, account_id, cipher, *, locations=1):
    available = [
        {"accountName": "accounts/123", "locationName": f"locations/{i}",
         "title": f"Rosa's Location {i}", "address": f"{i} Main St"}
        for i in range(1, locations + 1)
    ]
    return repo.upsert_external_connection(
        business_id=business_id, provider=GOOGLE_BUSINESS_PROVIDER,
        account_id=account_id,
        token_ciphertext=cipher.encrypt("refresh-one", business_id=business_id),
        scopes=[BUSINESS_SCOPE], status="connected" if locations == 1 else "pending_selection",
        external_account_name="accounts/123" if locations == 1 else None,
        external_location_name="locations/1" if locations == 1 else None,
        display_name="Rosa's Location 1" if locations == 1 else None,
        metadata={"locations": available},
    )


def response_body(response):
    return json.loads(response["body"])


def test_authorize_url_requests_only_business_manage_and_offline_access():
    url = build_authorize_url(
        client_id="client", redirect_uri="https://api/callback", state="signed",
    )
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [BUSINESS_SCOPE]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent select_account"]


def test_location_discovery_keeps_safe_destination_fields_only():
    locations = list_locations("access", transport=GoogleTransport(locations=2))
    assert [item.title for item in locations] == ["Rosa's Location 1", "Rosa's Location 2"]
    assert locations[0].parent == "accounts/123/locations/1"
    assert "token" not in json.dumps([item.as_dict() for item in locations]).lower()


def test_publish_uses_the_exact_location_and_standard_post_payload():
    transport = GoogleTransport()
    receipt = publish_post(
        "access", account_name="accounts/123", location_name="locations/1",
        summary="A clean post", transport=transport,
    )
    method, url, kwargs = transport.calls[-1]
    assert method == "POST"
    assert url.endswith("accounts/123/locations/1/localPosts")
    assert kwargs["json_body"] == {
        "languageCode": "en", "summary": "A clean post", "topicType": "STANDARD",
    }
    assert receipt["externalReference"].endswith("localPosts/abc")


def test_connect_returns_a_signed_authorization_url_for_the_current_tenant():
    repo, business_id, _, token = owner_repo()
    response = connect(event(
        "/v1/integrations/google-business/connect", token, body={}
    ), repo=repo, config=CONFIG, now=NOW)
    assert response["statusCode"] == 200
    url = response_body(response)["authorizationUrl"]
    state_value = parse_qs(urlsplit(url).query)["state"][0]
    from brasstacks.oauth import verify_state
    claims = verify_state(state_value, secret=CONFIG.state_secret, now=NOW)
    assert claims["business_id"] == business_id
    assert claims["purpose"] == "google_business_connect"


def test_callback_encrypts_refresh_token_and_auto_selects_one_location():
    repo, business_id, account_id, _ = owner_repo()
    cipher = FakeCipher()
    state_value = sign_state({
        "purpose": "google_business_connect", "business_id": business_id,
        "account_id": account_id, "task_id": "task-1",
    }, secret=CONFIG.state_secret, now=NOW)
    response = callback({"queryStringParameters": {
        "state": state_value, "code": "code",
    }}, repo=repo, config=CONFIG, cipher=cipher, now=NOW,
       transport=GoogleTransport(locations=1))
    assert response["statusCode"] == 302
    assert "google_business=connected" in response["headers"]["Location"]
    connection = repo.get_external_connection(
        business_id, provider=GOOGLE_BUSINESS_PROVIDER,
    )
    assert connection.is_ready
    assert connection.display_name == "Rosa's Location 1"
    assert connection.token_ciphertext.startswith("cipher:")
    assert "refresh-one" not in response["headers"]["Location"]


def test_multiple_locations_require_an_explicit_owner_choice():
    repo, business_id, account_id, token = owner_repo()
    cipher = FakeCipher()
    state_value = sign_state({
        "purpose": "google_business_connect", "business_id": business_id,
        "account_id": account_id, "task_id": "",
    }, secret=CONFIG.state_secret, now=NOW)
    callback({"queryStringParameters": {"state": state_value, "code": "code"}},
             repo=repo, config=CONFIG, cipher=cipher, now=NOW,
             transport=GoogleTransport(locations=2))
    body = response_body(status(
        event("/v1/integrations/google-business/status", token, method="GET"),
        repo=repo, config=CONFIG, now=NOW,
    ))
    assert body["status"] == "pending_selection"
    assert len(body["locations"]) == 2

    chosen = response_body(select_location(event(
        "/v1/integrations/google-business/select", token,
        body={"accountName": "accounts/123", "locationName": "locations/2",
              "title": "Rosa's Location 2"},
    ), repo=repo, config=CONFIG, now=NOW))
    assert chosen["connected"] is True
    assert chosen["destination"]["title"] == "Rosa's Location 2"


def test_owner_confirmed_publish_creates_one_receipt_and_marks_find_live():
    repo, business_id, account_id, token = owner_repo()
    find_id, task_id, artifact_id = completed_google_post(
        repo, business_id, account_id,
    )
    cipher = FakeCipher()
    connect_record(repo, business_id, account_id, cipher)
    transport = GoogleTransport()
    request = event(
        f"/v1/tasks/{task_id}/actions/google-business/publish", token,
        body={"confirm": True, "artifact_id": artifact_id, "revision": 1},
    )
    first = publish(
        request, repo=repo, config=CONFIG, cipher=cipher, now=NOW,
        transport=transport,
    )
    second = publish(
        request, repo=repo, config=CONFIG, cipher=cipher, now=NOW,
        transport=transport,
    )
    assert first["statusCode"] == 200
    assert response_body(first)["externalUrl"].endswith("/abc")
    assert response_body(second)["reused"] is True
    assert len([call for call in transport.calls if call[1].endswith("/localPosts")]) == 1
    [receipt] = [r for r in repo.tool_executions(task_id) if r.tool_name.endswith("publish_post")]
    assert receipt.status == "succeeded"
    assert len(receipt.input_data["content_sha256"]) == 64
    assert receipt.input_data["content_characters"] == len(
        "Try our fixed-price takeout set. Order tonight."
    )
    assert receipt.output_data["requestFingerprint"] == receipt.input_data["content_sha256"]
    assert repo._finds[find_id].status == "live"
    assert repo._finds[find_id].execution_tool_id == receipt.execution_id


def test_publish_refuses_missing_confirmation_before_any_provider_call():
    repo, business_id, account_id, token = owner_repo()
    _, task_id, artifact_id = completed_google_post(repo, business_id, account_id)
    cipher = FakeCipher()
    connect_record(repo, business_id, account_id, cipher)
    transport = GoogleTransport()
    response = publish(event(
        f"/v1/tasks/{task_id}/actions/google-business/publish", token,
        body={"confirm": False, "artifact_id": artifact_id, "revision": 1},
    ), repo=repo, config=CONFIG, cipher=cipher, now=NOW, transport=transport)
    assert response["statusCode"] == 400
    assert transport.calls == []


def test_uncertain_provider_failure_is_recorded_and_not_retried():
    repo, business_id, account_id, token = owner_repo()
    _, task_id, artifact_id = completed_google_post(repo, business_id, account_id)
    cipher = FakeCipher()
    connect_record(repo, business_id, account_id, cipher)
    transport = GoogleTransport(uncertain=True)
    request = event(
        f"/v1/tasks/{task_id}/actions/google-business/publish", token,
        body={"confirm": True, "artifact_id": artifact_id, "revision": 1},
    )
    first = publish(request, repo=repo, config=CONFIG, cipher=cipher,
                    now=NOW, transport=transport)
    second = publish(request, repo=repo, config=CONFIG, cipher=cipher,
                     now=NOW, transport=transport)
    assert first["statusCode"] == 502
    assert response_body(first)["uncertain"] is True
    assert second["statusCode"] == 409
    assert len([call for call in transport.calls if call[1].endswith("/localPosts")]) == 1


def test_unconfigured_status_is_a_safe_setup_diagnostic_without_a_session():
    config = GoogleBusinessConfig(
        client_id=None, client_secret=None, redirect_uri=None,
        state_secret=None, frontend_url=None, kms_key_id=None,
    )
    response = status({}, repo=InMemoryRepository(), config=config, now=NOW)
    assert response["statusCode"] == 200
    assert response_body(response) == {
        "configured": False,
        "connected": False,
        "status": "not_configured",
        "locations": [],
    }


def test_meter_window_starts_when_google_confirms_publication():
    repo, business_id, account_id, token = owner_repo()
    # The owner may connect and publish days after Maker produced the draft.
    repo._sessions[token_fingerprint(token)]["expires_at"] = NOW + timedelta(days=30)
    find_id, task_id, artifact_id = completed_google_post(
        repo, business_id, account_id,
    )
    cipher = FakeCipher()
    connect_record(repo, business_id, account_id, cipher)
    launched = NOW + timedelta(days=5)
    response = publish(event(
        f"/v1/tasks/{task_id}/actions/google-business/publish", token,
        body={"confirm": True, "artifact_id": artifact_id, "revision": 1},
    ), repo=repo, config=CONFIG, cipher=cipher, now=launched,
       transport=GoogleTransport())
    assert response["statusCode"] == 200

    # The original 21-day window is preserved, but it begins at the provider
    # receipt instead of pretending the draft was already live five days ago.
    assert repo.due_finds(business_id, today=date(2026, 8, 27)) == []
    [due] = repo.due_finds(business_id, today=date(2026, 9, 1))
    assert due.find_id == find_id
    assert due.measurement_start == date(2026, 8, 11)
    assert due.verify_after == date(2026, 9, 1)
