"""Signup, as a Lambda behind API Gateway.

The one endpoint in this system that lets a stranger write to the database. Two
consequences shape it:

* **It costs money per call.** Every signup embeds the profile through Bedrock.
  `/ask` is throttled but can only read; this creates rows, from a URL printed
  in a public repository. Hence the invite code — not security theatre, a spend
  control.
* **It must be all-or-nothing in effect.** Validation and authorisation happen
  before anything is created, so a rejected request leaves no orphan business
  row behind for someone to find later and wonder about.
"""

from __future__ import annotations

import json
import os
from typing import Any

from datetime import datetime, timezone

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.onboarding import (
    FACT_SOURCE,
    PlacesGeocoder,
    build_profile_facts,
    geocode_or_none,
    validate,
)
from brasstacks.profile import ProfileError, business_profile_data, normalise_profile
from brasstacks.profile_schema import ensure_profile_schema
from brasstacks.secrets import hydrate_environment

CORS_HEADERS = {
    "Content-Type": "application/json",
    # The board is served from CloudFront and this from API Gateway — different
    # origins. Without these the signup form fails in a browser and works in
    # curl, which is the most confusing possible way for it to break.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,OPTIONS",
}

#: Owner rules the form does not ask about but every tenant should start with.
DEFAULT_OWNER_RULES = ("Require owner approval before executing any recommendation.",)


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS),
            "body": json.dumps(body)}


def parse_body(event: Any) -> dict:
    raw = (event or {}).get("body")
    if raw is None:
        raise ValueError("send a JSON body")
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"body is not valid JSON ({e})") from None
    else:
        payload = raw
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    return payload


def onboard(event: Any, *, repo: Any, embedder: Any, geocoder: Any = None,
            invite_code: str | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """The whole of signup, with its dependencies handed in.

    Separated from `handler` so the logic is testable without AWS, a database
    or a network — the same split every other agent in this project uses.
    """
    try:
        profile = normalise_profile(parse_body(event))
    except (ValueError, ProfileError) as e:
        return respond(400, {"error": str(e)})

    # The session from /register. The invite code was checked there, and this
    # endpoint is unreachable without having passed it — re-checking would only
    # ask the browser to keep the code around for a second page.
    token = bearer_token(event)
    account = repo.account_for_session(
        token_fingerprint(token), now=now or datetime.now(timezone.utc)
    ) if token else None
    if account is None:
        return respond(401, {"error": "sign in first"})

    if account.get("business_id"):
        # One business per account. Without this, reloading the finished form
        # would quietly create a second tenant and orphan the first.
        return respond(409, {"error": "this account already has a workspace"})

    problem = validate(profile)
    if problem:
        return respond(400, {"error": problem})

    business = profile["business"]

    # Name and address together. Text Search resolves a business far better
    # given both — the address alone lands on a building, the name alone is
    # ambiguous across a metro. The form collects them separately, so joining
    # them is this layer's job.
    name = str(business["name"]).strip()
    address = str(business.get("location") or "").strip()
    query = f"{name}, {address}" if address else name
    located = geocode_or_none(geocoder, query)

    business_id = repo.create_business(
        name=name,
        category=str(business["category"]).strip(),
        city=address or None,
        latitude=located.latitude if located else None,
        longitude=located.longitude if located else None,
    )

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

    facts = build_profile_facts(profile)
    vectors = embedder.embed(facts) if facts else []
    repo.replace_business_profile_facts(
        business_id, facts=facts, embeddings=vectors, source=FACT_SOURCE,
    )

    rules = [str(r).strip() for r in (profile.get("ownerRules") or []) if str(r).strip()]
    for rule in (rules or list(DEFAULT_OWNER_RULES)):
        repo.insert_owner_rule(business_id, rule=rule)

    # Last, so a failure anywhere above rolls back rather than pointing the
    # owner's account at a business with no profile behind it. This also moves
    # their live session onto the new business, so they are already signed in
    # to it when the board loads.
    repo.attach_business(account["account_id"], business_id=business_id)

    return respond(201, {
        "business_id": business_id,
        "facts_stored": len(facts),
        # Echoed so the form can show the owner what the agents will actually
        # search around, rather than what they typed. Text Search is fuzzy, so
        # this is how a wrong match becomes visible instead of silent.
        "located": located.formatted if located else None,
        "matched": located.matched_name if located else None,
    })


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    method = str(((event or {}).get("requestContext") or {})
                 .get("http", {}).get("method") or "POST").upper()
    raw_path = str((event or {}).get("rawPath") or "")
    if method == "OPTIONS":
        return respond(204, {})

    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.providers import build_embedder
    from brasstacks.repository_pg import PostgresRepository

    geocoder = (PlacesGeocoder(api_key=settings.google_maps_api_key)
                if settings.google_maps_api_key else None)

    # /profile shares the onboarding Lambda: it uses the same profile schema,
    # geocoder, embedder and transaction boundary without creating another
    # container image merely to expose two authenticated methods.
    if raw_path.rstrip("/").endswith("/profile"):
        from brasstacks.handlers.profile import read_profile, update_profile

        with psycopg.connect(settings.cockroach_url) as conn:
            ensure_profile_schema(conn)
            repo = PostgresRepository(conn)
            if method == "GET":
                return read_profile(event, repo=repo)
            if method == "PUT":
                return update_profile(
                    event, repo=repo, embedder=build_embedder(settings),
                    geocoder=geocoder,
                )
            return respond(405, {"error": "method not allowed"})

    # Deliberately NOT autocommit. Signup is several writes that only mean
    # something together; psycopg commits on clean exit and rolls back on error.
    with psycopg.connect(settings.cockroach_url) as conn:
        ensure_profile_schema(conn)
        return onboard(
            event,
            repo=PostgresRepository(conn),
            embedder=build_embedder(settings),
            geocoder=geocoder,
            invite_code=os.environ.get("BRASSTACKS_INVITE_CODE") or None,
        )


__all__ = ["handler", "onboard", "parse_body", "respond"]
