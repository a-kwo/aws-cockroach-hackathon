"""Connect and publish to Google Business Profile with owner confirmation.

Routes served by one Lambda:

* POST /integrations/google-business/connect
* GET  /integrations/google-business/callback
* GET  /integrations/google-business/status
* POST /integrations/google-business/select
* POST /tasks/{task_id}/actions/google-business/publish

The callback never exposes OAuth tokens.  Maker never receives credentials or a
destination.  The exact artifact revision, tenant connection and owner approval
are revalidated immediately before the provider call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.connections import (
    CONNECTION_CONNECTED,
    CONNECTION_PENDING,
    GOOGLE_BUSINESS_PROVIDER,
)
from brasstacks.execution_schema import ensure_execution_schema
from brasstacks.task_schema import ensure_task_schema
from brasstacks.google_business import (
    TOOL_NAME,
    AwsKmsTokenCipher,
    GoogleBusinessError,
    TokenCipher,
    build_authorize_url,
    exchange_code,
    list_locations,
    normalize_post_text,
    publish_post,
    refresh_access_token,
)
from brasstacks.handlers.login import bearer_token
from brasstacks.oauth import OAuthError, sign_state, verify_state
from brasstacks.repository import RepositoryError
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Cache-Control": "no-store",
}

_TASK_PATH = re.compile(r"/tasks/([^/]+)/actions/google-business/publish/?$")


@dataclass(frozen=True)
class GoogleBusinessConfig:
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    state_secret: str | None
    frontend_url: str | None
    kms_key_id: str | None

    @property
    def configured(self) -> bool:
        return all([
            self.client_id, self.client_secret, self.redirect_uri,
            self.state_secret, self.frontend_url, self.kms_key_id,
        ])

    @property
    def site(self) -> str:
        return str(self.frontend_url or "").rstrip("/")

    @classmethod
    def from_env(cls) -> "GoogleBusinessConfig":
        return cls(
            client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or None,
            client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or None,
            redirect_uri=os.environ.get("GOOGLE_BUSINESS_REDIRECT_URI") or None,
            state_secret=os.environ.get("GOOGLE_OAUTH_STATE_SECRET") or None,
            frontend_url=os.environ.get("BRASSTACKS_SITE_URL") or None,
            kms_key_id=os.environ.get("GOOGLE_BUSINESS_TOKEN_KEY_ID") or None,
        )


def respond(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS),
            "body": json.dumps(dict(body), default=str)}


def redirect(location: str) -> dict[str, Any]:
    return {"statusCode": 302, "headers": {
        "Location": location, "Cache-Control": "no-store",
    }, "body": ""}


def _path(event: Any) -> str:
    raw = (event or {}).get("rawPath")
    if not raw:
        raw = (((event or {}).get("requestContext") or {}).get("http") or {}).get("path")
    return str(raw or "")


def _method(event: Any) -> str:
    value = (((event or {}).get("requestContext") or {}).get("http") or {}).get("method")
    return str(value or (event or {}).get("httpMethod") or "GET").upper()


def _query(event: Any) -> Mapping[str, Any]:
    return (event or {}).get("queryStringParameters") or {}


def _json_body(event: Any) -> dict[str, Any]:
    raw = (event or {}).get("body")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("send a JSON body") from exc
    if not isinstance(payload, dict):
        raise ValueError("send a JSON body")
    return payload


def _session(event: Any, *, repo: Any, now: datetime) -> tuple[str, str]:
    token = bearer_token(event)
    account = (
        repo.account_for_session(token_fingerprint(token), now=now)
        if token else None
    )
    if account is None:
        raise PermissionError("sign in again")
    business_id = str(account.get("business_id") or "")
    account_id = str(account.get("account_id") or "")
    if not business_id:
        raise PermissionError("finish business setup before connecting Google Business")
    return business_id, account_id


def _safe_connection(connection: Any) -> dict[str, Any]:
    if connection is None:
        return {
            "configured": True, "connected": False,
            "status": "not_connected", "destination": None, "locations": [],
        }
    locations = connection.metadata.get("locations")
    if not isinstance(locations, list):
        locations = []
    safe_locations = [
        {
            "accountName": str(row.get("accountName") or ""),
            "locationName": str(row.get("locationName") or ""),
            "title": str(row.get("title") or "Google Business Profile"),
            "address": str(row.get("address") or "") or None,
        }
        for row in locations if isinstance(row, Mapping)
    ]
    return {
        "configured": True,
        "connected": bool(connection.is_ready),
        "status": connection.status,
        "destination": ({
            "title": connection.display_name,
            "accountName": connection.external_account_name,
            "locationName": connection.external_location_name,
        } if connection.is_ready else None),
        "locations": safe_locations,
        "lastError": connection.last_error,
        "connectedAt": connection.connected_at.isoformat() if connection.connected_at else None,
    }


def status(event: Any, *, repo: Any, config: GoogleBusinessConfig,
           now: datetime | None = None) -> dict[str, Any]:
    if not config.configured:
        return respond(200, {"configured": False, "connected": False,
                             "status": "not_configured", "locations": []})
    moment = now or datetime.now(timezone.utc)
    try:
        business_id, _ = _session(event, repo=repo, now=moment)
    except PermissionError as exc:
        return respond(401, {"error": str(exc)})
    return respond(200, _safe_connection(repo.get_external_connection(
        business_id, provider=GOOGLE_BUSINESS_PROVIDER)))


def connect(event: Any, *, repo: Any, config: GoogleBusinessConfig,
            now: datetime | None = None) -> dict[str, Any]:
    if not config.configured:
        return respond(503, {"error": "Google Business publishing is not configured"})
    moment = now or datetime.now(timezone.utc)
    try:
        business_id, account_id = _session(event, repo=repo, now=moment)
        payload = _json_body(event)
    except PermissionError as exc:
        return respond(401, {"error": str(exc)})
    except ValueError as exc:
        return respond(400, {"error": str(exc)})
    task_id = str(payload.get("task_id") or "").strip()
    if task_id:
        task = repo.get_task(task_id, business_id=business_id)
        if task is None:
            return respond(404, {"error": "Maker task is no longer available"})
    state = sign_state({
        "purpose": "google_business_connect",
        "business_id": business_id,
        "account_id": account_id,
        "task_id": task_id,
    }, secret=str(config.state_secret), now=moment)
    return respond(200, {"authorizationUrl": build_authorize_url(
        client_id=str(config.client_id), redirect_uri=str(config.redirect_uri),
        state=state)})


def callback(event: Any, *, repo: Any, config: GoogleBusinessConfig,
             cipher: TokenCipher, now: datetime | None = None,
             transport: Any = None) -> dict[str, Any]:
    if not config.configured:
        return respond(503, {"error": "Google Business publishing is not configured"})
    moment = now or datetime.now(timezone.utc)
    query = _query(event)
    try:
        claims = verify_state(query.get("state"), secret=str(config.state_secret), now=moment)
        if claims.get("purpose") != "google_business_connect":
            raise OAuthError("that Google Business connection is not valid")
        business_id = str(claims.get("business_id") or "")
        account_id = str(claims.get("account_id") or "")
        task_id = str(claims.get("task_id") or "")
        account = repo.find_account_by_id(account_id)
        if account is None or account.get("business_id") != business_id:
            raise OAuthError("that Google Business connection is not valid")
        if query.get("error"):
            suffix = urlencode({"task": task_id, "google_business": "cancelled"})
            return redirect(f"{config.site}/app/?{suffix}")
        code = str(query.get("code") or "").strip()
        if not code:
            raise OAuthError("Google Business did not complete the connection")
        tokens = exchange_code(
            code, client_id=str(config.client_id),
            client_secret=str(config.client_secret),
            redirect_uri=str(config.redirect_uri), transport=transport,
        )
        locations = list_locations(tokens["access_token"], transport=transport)
        if not locations:
            raise GoogleBusinessError(
                "No Business Profile locations were available for that Google account"
            )
        ciphertext = cipher.encrypt(tokens["refresh_token"], business_id=business_id)
        selected = locations[0] if len(locations) == 1 else None
        connection = repo.upsert_external_connection(
            business_id=business_id, provider=GOOGLE_BUSINESS_PROVIDER,
            account_id=account_id, token_ciphertext=ciphertext,
            scopes=tuple(tokens["scope"].split()),
            status=CONNECTION_CONNECTED if selected else CONNECTION_PENDING,
            external_account_name=selected.account_name if selected else None,
            external_location_name=selected.location_name if selected else None,
            display_name=selected.title if selected else None,
            metadata={"locations": [item.as_dict() for item in locations]},
        )
        outcome = "connected" if connection.is_ready else "choose_location"
        suffix = urlencode({"task": task_id, "google_business": outcome})
        return redirect(f"{config.site}/app/?{suffix}")
    except (OAuthError, GoogleBusinessError, RepositoryError) as exc:
        task_id = str(locals().get("task_id") or "")
        # Fixed code in the URL; provider details remain server-side.
        suffix = urlencode({"task": task_id, "google_business": "error"})
        return redirect(f"{config.site}/app/?{suffix}")


def select_location(event: Any, *, repo: Any, config: GoogleBusinessConfig,
                    now: datetime | None = None) -> dict[str, Any]:
    if not config.configured:
        return respond(503, {"error": "Google Business publishing is not configured"})
    moment = now or datetime.now(timezone.utc)
    try:
        business_id, _ = _session(event, repo=repo, now=moment)
        payload = _json_body(event)
        account_name = str(payload.get("accountName") or "").strip()
        location_name = str(payload.get("locationName") or "").strip()
        title = str(payload.get("title") or "Google Business Profile").strip()
        if not account_name or not location_name:
            raise ValueError("choose a Google Business location")
        connection = repo.select_external_connection(
            business_id, provider=GOOGLE_BUSINESS_PROVIDER,
            account_name=account_name, location_name=location_name,
            display_name=title,
        )
        return respond(200, _safe_connection(connection))
    except PermissionError as exc:
        return respond(401, {"error": str(exc)})
    except (ValueError, RepositoryError) as exc:
        return respond(400, {"error": str(exc)})


def _current_artifact(repo: Any, *, task: Any, artifact_id: str, revision: int) -> Any:
    if task.status != "completed" or not task.output_artifact_id:
        raise RepositoryError("Maker must finish the current draft before publishing")
    if str(task.output_artifact_id) != artifact_id:
        raise RepositoryError("Review the latest Maker revision before publishing")
    artifacts = repo.get_artifacts(str(task.find_id))
    artifact = next((row for row in artifacts if row.artifact_id == artifact_id), None)
    if artifact is None or artifact.superseded_at is not None:
        raise RepositoryError("Review the latest Maker revision before publishing")
    if artifact.task_id != task.task_id or int(artifact.revision) != int(revision):
        raise RepositoryError("Review the latest Maker revision before publishing")
    if artifact.review_state != "ready_for_review":
        raise RepositoryError("Answer Maker's questions before publishing")
    manifest = artifact.metadata.get("action_manifest")
    if not isinstance(manifest, Mapping) or manifest.get("action_type") != TOOL_NAME:
        raise RepositoryError("This Maker draft is not a Google Business post")
    if artifact.metadata.get("artifact_type") != "google_business_post":
        raise RepositoryError("This Maker draft is not a Google Business post")
    return artifact


def publish(event: Any, *, repo: Any, config: GoogleBusinessConfig,
            cipher: TokenCipher, now: datetime | None = None,
            transport: Any = None) -> dict[str, Any]:
    if not config.configured:
        return respond(503, {"error": "Google Business publishing is not configured"})
    moment = now or datetime.now(timezone.utc)
    try:
        business_id, account_id = _session(event, repo=repo, now=moment)
        payload = _json_body(event)
        if payload.get("confirm") is not True:
            raise ValueError("Confirm the exact post and destination before publishing")
        task_match = _TASK_PATH.search(_path(event))
        task_id = str(task_match.group(1) if task_match else payload.get("task_id") or "")
        artifact_id = str(payload.get("artifact_id") or "").strip()
        revision = int(payload.get("revision") or 0)
        if not task_id or not artifact_id or revision < 1:
            raise ValueError("The Maker draft reference is incomplete")
        task = repo.get_task(task_id, business_id=business_id)
        if task is None:
            return respond(404, {"error": "Maker task is no longer available"})
        artifact = _current_artifact(
            repo, task=task, artifact_id=artifact_id, revision=revision,
        )
        try:
            post_text = normalize_post_text(artifact.body)
        except GoogleBusinessError as exc:
            return respond(400, {"error": str(exc)})
        request_fingerprint = hashlib.sha256(post_text.encode("utf-8")).hexdigest()
        connection = repo.get_external_connection(
            business_id, provider=GOOGLE_BUSINESS_PROVIDER,
        )
        if connection is None or not connection.is_ready:
            return respond(409, {"error": "Connect and choose a Google Business Profile first"})

        key = f"task:{task_id}:tool:{TOOL_NAME}:artifact:{artifact_id}:v{revision}"
        receipt = repo.start_tool_execution(
            task_id=task_id, business_id=business_id, tool_name=TOOL_NAME,
            idempotency_key=key,
            input_data={
                "artifact_id": artifact_id, "revision": revision,
                "destination": connection.display_name,
                "account_name": connection.external_account_name,
                "location_name": connection.external_location_name,
                "approved_by_account_id": account_id,
                "approved_at": moment.isoformat(),
                "content_sha256": request_fingerprint,
                "content_characters": len(post_text),
            },
        )
        if not receipt.created:
            if receipt.status == "succeeded":
                return respond(200, {
                    "status": "succeeded", "reused": True,
                    "receiptId": receipt.execution_id,
                    **dict(receipt.output_data),
                })
            return respond(409, {
                "error": (
                    "A publication attempt is still being checked. Review the Google profile before trying again."
                    if receipt.status == "running"
                    else "This revision already has a failed publication receipt. Create a new revision before retrying."
                ),
                "receiptId": receipt.execution_id,
            })

        repo.record_task_event(
            task_id, event_type="tool.owner_confirmed", actor_type="owner",
            actor_id=account_id,
            data={"tool_execution_id": receipt.execution_id,
                  "artifact_id": artifact_id, "revision": revision,
                  "destination": connection.display_name},
        )
        try:
            refresh_token = cipher.decrypt(
                connection.token_ciphertext, business_id=business_id,
            )
            access_token = refresh_access_token(
                refresh_token, client_id=str(config.client_id),
                client_secret=str(config.client_secret), transport=transport,
            )
            result = publish_post(
                access_token,
                account_name=str(connection.external_account_name),
                location_name=str(connection.external_location_name),
                summary=post_text, transport=transport,
            )
        except GoogleBusinessError as exc:
            finished = repo.finish_tool_execution(
                receipt.execution_id, status="failed", error=str(exc),
                output_data={"uncertain": bool(exc.uncertain)},
            )
            return respond(502 if exc.uncertain else 400, {
                "error": str(exc), "uncertain": bool(exc.uncertain),
                "receiptId": finished.execution_id,
            })

        output = {
            **result,
            "destination": connection.display_name,
            "artifactId": artifact_id,
            "revision": revision,
            "executedAt": moment.isoformat(),
            "requestFingerprint": request_fingerprint,
        }
        finished = repo.finish_tool_execution(
            receipt.execution_id, status="succeeded",
            output_data=output, external_reference=result["externalReference"],
        )
        repo.mark_find_executed(
            str(task.find_id), business_id=business_id,
            tool_execution_id=finished.execution_id, executed_at=moment,
        )
        return respond(200, {
            "status": "succeeded", "reused": False,
            "receiptId": finished.execution_id, **output,
        })
    except PermissionError as exc:
        return respond(401, {"error": str(exc)})
    except (ValueError, RepositoryError) as exc:
        return respond(400, {"error": str(exc)})


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    if _method(event) == "OPTIONS":
        return respond(204, {})
    hydrate_environment()
    settings = Settings.load()
    config = GoogleBusinessConfig.from_env()

    import boto3
    import psycopg
    from brasstacks.repository_pg import PostgresRepository

    path = _path(event).rstrip("/")
    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        # external_connection references the task receipt tables. Keep the
        # request-time bootstrap ordered for a safe rolling deployment even if
        # the CI migration job is delayed.
        ensure_task_schema(conn)
        ensure_execution_schema(conn)
        repo = PostgresRepository(conn)

        # Status and connect remain useful setup diagnostics before OAuth or KMS
        # configuration is complete. Construct the cipher only for routes that
        # can actually read or write a refresh token.
        cipher = None
        if path.endswith("/integrations/google-business/callback") or _TASK_PATH.search(path):
            if not config.configured:
                return respond(503, {"error": "Google Business publishing is not configured"})
            cipher = AwsKmsTokenCipher(
                key_id=str(config.kms_key_id), kms_client=boto3.client("kms"),
            )

        if path.endswith("/integrations/google-business/callback"):
            return callback(event, repo=repo, config=config, cipher=cipher)
        if path.endswith("/integrations/google-business/connect"):
            return connect(event, repo=repo, config=config)
        if path.endswith("/integrations/google-business/status"):
            return status(event, repo=repo, config=config)
        if path.endswith("/integrations/google-business/select"):
            return select_location(event, repo=repo, config=config)
        if _TASK_PATH.search(path):
            return publish(event, repo=repo, config=config, cipher=cipher)
        return respond(404, {"error": "no such endpoint"})


__all__ = [
    "GoogleBusinessConfig", "callback", "connect", "handler", "publish",
    "respond", "select_location", "status",
]
