"""Google Business Profile connection and publishing primitives.

The Maker model prepares content.  This module performs the constrained external
side effect only after the owner confirms an exact artifact revision and the
server selects a tenant-owned destination.  OAuth credentials are encrypted by
KMS and never enter a model prompt or browser response.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode

from brasstacks.oauth import AUTHORIZE_ENDPOINT, TOKEN_ENDPOINT

BUSINESS_SCOPE = "https://www.googleapis.com/auth/business.manage"
ACCOUNT_API = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
BUSINESS_INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
LOCAL_POST_API = "https://mybusiness.googleapis.com/v4"
TOOL_NAME = "google_business.publish_post"
MAX_POST_CHARS = 1500


class GoogleBusinessError(RuntimeError):
    """A safe, owner-facing failure.

    ``uncertain`` means the provider may have accepted the write before the
    connection failed.  The caller must not retry automatically.
    """

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class TokenCipher(Protocol):
    def encrypt(self, plaintext: str, *, business_id: str) -> str: ...
    def decrypt(self, ciphertext: str, *, business_id: str) -> str: ...


class AwsKmsTokenCipher:
    """Envelope the refresh token directly with a customer-managed KMS key."""

    PREFIX = "kms:v1:"

    def __init__(self, *, key_id: str, kms_client: Any) -> None:
        if not str(key_id or "").strip():
            raise GoogleBusinessError("Google Business token encryption is not configured")
        self.key_id = str(key_id)
        self.kms = kms_client

    @staticmethod
    def _context(business_id: str) -> dict[str, str]:
        return {"business_id": str(business_id), "provider": "google_business"}

    def encrypt(self, plaintext: str, *, business_id: str) -> str:
        if not plaintext:
            raise GoogleBusinessError("Google did not return a reusable connection token")
        result = self.kms.encrypt(
            KeyId=self.key_id,
            Plaintext=plaintext.encode("utf-8"),
            EncryptionContext=self._context(business_id),
        )
        blob = bytes(result["CiphertextBlob"])
        return self.PREFIX + base64.b64encode(blob).decode("ascii")

    def decrypt(self, ciphertext: str, *, business_id: str) -> str:
        raw = str(ciphertext or "")
        if not raw.startswith(self.PREFIX):
            raise GoogleBusinessError("The saved Google Business connection cannot be decrypted")
        try:
            blob = base64.b64decode(raw[len(self.PREFIX):], validate=True)
            result = self.kms.decrypt(
                CiphertextBlob=blob,
                EncryptionContext=self._context(business_id),
            )
            return bytes(result["Plaintext"]).decode("utf-8")
        except GoogleBusinessError:
            raise
        except Exception as exc:
            raise GoogleBusinessError(
                "The saved Google Business connection cannot be decrypted"
            ) from exc


@dataclass(frozen=True)
class GoogleLocation:
    account_name: str
    location_name: str
    title: str
    address: str | None = None

    @property
    def parent(self) -> str:
        location_id = self.location_name.rsplit("/", 1)[-1]
        return f"{self.account_name}/locations/{location_id}"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "accountName": self.account_name,
            "locationName": self.location_name,
            "title": self.title,
            "address": self.address,
        }


Transport = Callable[..., Mapping[str, Any]]


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    return f"{AUTHORIZE_ENDPOINT}?{urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': BUSINESS_SCOPE,
        'state': state,
        'access_type': 'offline',
        'include_granted_scopes': 'true',
        # A refresh token is required for a durable server-side connection.
        'prompt': 'consent select_account',
    })}"


def _http_json(method: str, url: str, *, headers: Mapping[str, str] | None = None,
               data: Mapping[str, Any] | None = None,
               json_body: Mapping[str, Any] | None = None,
               uncertain_write: bool = False) -> Mapping[str, Any]:
    import httpx

    try:
        response = httpx.request(
            method, url, headers=dict(headers or {}), data=data,
            json=json_body, timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            body = exc.response.json()
            detail = str(((body or {}).get("error") or {}).get("message") or "")
        except Exception:
            detail = ""
        safe = detail[:240] if detail else "Google Business rejected the request"
        raise GoogleBusinessError(safe, uncertain=False) from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise GoogleBusinessError(
            "Google Business did not confirm the request. Check the profile before trying again.",
            uncertain=uncertain_write,
        ) from exc
    except Exception as exc:
        raise GoogleBusinessError("Google Business returned an invalid response") from exc


def exchange_code(code: str, *, client_id: str, client_secret: str,
                  redirect_uri: str, transport: Transport | None = None) -> dict[str, Any]:
    call = transport or _http_json
    payload = call("POST", TOKEN_ENDPOINT, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    refresh_token = str(payload.get("refresh_token") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    if not refresh_token or not access_token:
        raise GoogleBusinessError(
            "Google did not return a reusable Business Profile connection. Reconnect and approve access."
        )
    return {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "scope": str(payload.get("scope") or BUSINESS_SCOPE),
    }


def refresh_access_token(refresh_token: str, *, client_id: str,
                         client_secret: str,
                         transport: Transport | None = None) -> str:
    call = transport or _http_json
    payload = call("POST", TOKEN_ENDPOINT, data={
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise GoogleBusinessError(
            "The Google Business connection has expired. Connect it again."
        )
    return token


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


def _address_text(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    lines = value.get("addressLines")
    parts: list[str] = []
    if isinstance(lines, list):
        parts.extend(str(item).strip() for item in lines if str(item).strip())
    for key in ("locality", "administrativeArea", "postalCode"):
        text = str(value.get(key) or "").strip()
        if text:
            parts.append(text)
    return ", ".join(parts) or None


def list_locations(access_token: str, *, transport: Transport | None = None) -> list[GoogleLocation]:
    call = transport or _http_json
    accounts_payload = call("GET", ACCOUNT_API, headers=_auth(access_token))
    accounts = accounts_payload.get("accounts")
    if not isinstance(accounts, list):
        accounts = []

    output: list[GoogleLocation] = []
    for account in accounts[:50]:
        if not isinstance(account, Mapping):
            continue
        account_name = str(account.get("name") or "").strip()
        if not account_name.startswith("accounts/"):
            continue
        page_token = ""
        for _ in range(10):
            query = {
                "readMask": "name,title,storefrontAddress",
                "pageSize": "100",
            }
            if page_token:
                query["pageToken"] = page_token
            url = f"{BUSINESS_INFO_API}/{account_name}/locations?{urlencode(query)}"
            payload = call("GET", url, headers=_auth(access_token))
            locations = payload.get("locations")
            if not isinstance(locations, list):
                locations = []
            for location in locations:
                if not isinstance(location, Mapping):
                    continue
                location_name = str(location.get("name") or "").strip()
                title = str(location.get("title") or "").strip()
                if not location_name or not title:
                    continue
                output.append(GoogleLocation(
                    account_name=account_name,
                    location_name=location_name,
                    title=title[:160],
                    address=_address_text(location.get("storefrontAddress")),
                ))
            page_token = str(payload.get("nextPageToken") or "").strip()
            if not page_token:
                break
    return output


def normalize_post_text(value: Any) -> str:
    text = "\n".join(line.rstrip() for line in str(value or "").strip().splitlines())
    if not text:
        raise GoogleBusinessError("The Maker draft is empty")
    if len(text) > MAX_POST_CHARS:
        raise GoogleBusinessError(
            f"The Maker draft is {len(text)} characters. Shorten it to {MAX_POST_CHARS} before publishing."
        )
    return text


def publish_post(access_token: str, *, account_name: str, location_name: str,
                 summary: str, language_code: str = "en",
                 transport: Transport | None = None) -> dict[str, Any]:
    call = transport or _http_json
    if not str(account_name).startswith("accounts/"):
        raise GoogleBusinessError("The selected Google Business account is invalid")
    if not str(location_name).startswith("locations/"):
        raise GoogleBusinessError("The selected Google Business location is invalid")
    location_id = str(location_name).rsplit("/", 1)[-1]
    parent = f"{account_name}/locations/{location_id}"
    url = f"{LOCAL_POST_API}/{parent}/localPosts"
    payload = call(
        "POST", url, headers={**_auth(access_token), "Content-Type": "application/json"},
        json_body={
            "languageCode": str(language_code or "en")[:10],
            "summary": normalize_post_text(summary),
            "topicType": "STANDARD",
        },
        uncertain_write=True,
    )
    name = str(payload.get("name") or "").strip()
    if not name:
        raise GoogleBusinessError(
            "Google Business did not return a publication receipt. Check the profile before trying again.",
            uncertain=True,
        )
    return {
        "externalReference": name,
        "externalUrl": str(payload.get("searchUrl") or "").strip() or None,
        "createTime": str(payload.get("createTime") or "").strip() or None,
        "state": str(payload.get("state") or "").strip() or None,
    }


__all__ = [
    "ACCOUNT_API", "BUSINESS_INFO_API", "BUSINESS_SCOPE", "LOCAL_POST_API",
    "MAX_POST_CHARS", "TOOL_NAME", "AwsKmsTokenCipher", "GoogleBusinessError",
    "GoogleLocation", "TokenCipher", "build_authorize_url", "exchange_code",
    "list_locations", "normalize_post_text", "publish_post", "refresh_access_token",
]
