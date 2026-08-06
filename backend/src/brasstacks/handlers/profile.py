"""Authenticated owner profile reads and updates.

The profile is deterministic application state, not an LLM conversation.
Contact information stays on ``owner_account`` and never enters embeddings. The
business brief is stored as structured JSON and re-embedded as a bounded set of
sentence-shaped facts only when the owner saves a change.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.onboarding import FACT_SOURCE, PlacesGeocoder, build_profile_facts, geocode_or_none
from brasstacks.profile import (
    ProfileError,
    business_profile_data,
    normalise_profile,
    profile_from_record,
)
from brasstacks.profile_schema import ensure_profile_schema
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
    "Cache-Control": "no-store",
}


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS),
            "body": json.dumps(body, default=str)}


def _account(event: Any, repo: Any, *, now: datetime) -> dict[str, Any] | None:
    token = bearer_token(event)
    return repo.account_for_session(token_fingerprint(token), now=now) if token else None


def read_profile(event: Any, *, repo: Any,
                 now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    account = _account(event, repo, now=moment)
    if account is None:
        return respond(401, {"error": "sign in first"})
    business_id = account.get("business_id")
    if not business_id:
        return respond(409, {"error": "finish business setup first"})

    record = repo.get_owner_profile(account["account_id"], business_id=business_id)
    if record is None:
        return respond(404, {"error": "profile is not available"})
    rules = [item.rule for item in repo.get_owner_rules(business_id)]
    return respond(200, {
        "profile": profile_from_record(record, owner_rules=rules),
        "source": "cockroachdb",
        "modelTokens": 0,
    })


def update_profile(event: Any, *, repo: Any, embedder: Any, geocoder: Any = None,
                   now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    account = _account(event, repo, now=moment)
    if account is None:
        return respond(401, {"error": "sign in first"})
    business_id = account.get("business_id")
    if not business_id:
        return respond(409, {"error": "finish business setup first"})

    raw = (event or {}).get("body")
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        # The profile editor does not know about menus and sends no `menu`
        # key. Rebuilding profile_data from that body verbatim would wipe a
        # scanned menu — and its facts — the first time the owner changed
        # something unrelated, while the menu observations stayed in the
        # corpus. Absent means "not editing that"; an explicit null clears it.
        if isinstance(payload, dict) and "menu" not in payload:
            existing = repo.get_owner_profile(
                account["account_id"], business_id=business_id
            )
            stored = (existing or {}).get("profile_data")
            if isinstance(stored, dict) and stored.get("menu"):
                payload = {**payload, "menu": stored["menu"]}
        profile = normalise_profile(payload)
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError, ProfileError) as exc:
        message = str(exc) if str(exc) else "send a valid profile"
        return respond(400, {"error": message})

    business = profile["business"]
    query = f"{business['name']}, {business['location']}"
    located = geocode_or_none(geocoder, query)
    facts = build_profile_facts(profile)
    vectors = embedder.embed(facts) if facts else []

    repo.update_account_profile(
        account["account_id"], display_name=profile["owner"]["name"],
        email=profile["owner"]["email"],
    )
    repo.update_business_profile(
        business_id,
        name=business["name"], category=business["category"],
        city=business["location"], profile_data=business_profile_data(profile),
        latitude=located.latitude if located else None,
        longitude=located.longitude if located else None,
    )
    repo.replace_business_profile_facts(
        business_id, facts=facts, embeddings=vectors, source=FACT_SOURCE,
    )

    record = repo.get_owner_profile(account["account_id"], business_id=business_id)
    if record is None:  # defensive: both update statements just succeeded
        return respond(500, {"error": "profile could not be reloaded"})
    rules = [item.rule for item in repo.get_owner_rules(business_id)]
    return respond(200, {
        "profile": profile_from_record(record, owner_rules=rules),
        "factsStored": len(facts),
        "located": located.formatted if located else None,
        "matched": located.matched_name if located else None,
        # Profile saves use embeddings, not a reasoning model. Distinguish the
        # two so Memory Engine does not imply an LLM was called.
        "reasoningModelTokens": 0,
    })


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    method = str(((event or {}).get("requestContext") or {})
                 .get("http", {}).get("method") or "GET").upper()
    if method == "OPTIONS":
        return respond(204, {})
    if method not in {"GET", "PUT"}:
        return respond(405, {"error": "use GET or PUT"})

    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.providers import build_embedder
    from brasstacks.repository_pg import PostgresRepository

    geocoder = (PlacesGeocoder(api_key=settings.google_maps_api_key)
                if settings.google_maps_api_key else None)
    # GET is a SQL-only read. PUT is one transaction covering contact fields,
    # business metadata and the replacement profile facts.
    with psycopg.connect(settings.cockroach_url, autocommit=(method == "GET")) as conn:
        ensure_profile_schema(conn)
        repo = PostgresRepository(conn)
        if method == "GET":
            return read_profile(event, repo=repo)
        return update_profile(
            event, repo=repo, embedder=build_embedder(settings), geocoder=geocoder,
        )


__all__ = ["handler", "read_profile", "respond", "update_profile"]
