"""The owner asking for their first night, now.

The nightly schedule is the product's spine, but a business that signed up this
afternoon should not have to wait until 6 AM to see whether any of this works.
This is the button at the end of onboarding.

**Asynchronous, and it has to be.** API Gateway caps an HTTP integration at 30
seconds; a night runs 60-90. A synchronous trigger would return 504 while the
work carried on invisibly, and the owner would press it again. So this validates,
fires the night with `InvocationType="Event"`, and returns 202 immediately. The
board then polls the workflow endpoint until finds appear.

**It spends money, so it is guarded three ways.** A night costs a Tavily search,
roughly fifty Bedrock embeddings and a Claude call:

* The caller must hold a session, and gets a night only for *their own* business
  — a business id in the request body would let anyone spend against any tenant.
* One first run per business. Repeated presses return the run already in flight
  rather than starting a second one, which is what a double-click and an
  impatient reload would otherwise do.
* The nightly schedule remains the only unattended path.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token, respond
from brasstacks.secrets import hydrate_environment

#: Set on this function by the template so it knows what to invoke.
NIGHT_FUNCTION_VAR = "BRASSTACKS_NIGHT_FUNCTION"


def start_night(event: Any, *, repo: Any, invoker: Any,
                night_function: str | None = None,
                now: datetime | None = None) -> dict[str, Any]:
    """Authorise and fire one night. Dependencies handed in, as everywhere here.

    `invoker` is anything with `.invoke(FunctionName=..., InvocationType=...,
    Payload=...)` — the boto3 Lambda client in production, a fake in tests.
    """
    moment = now or datetime.now(timezone.utc)

    token = bearer_token(event)
    account = repo.account_for_session(
        token_fingerprint(token), now=moment) if token else None
    if account is None:
        return respond(401, {"error": "sign in first"})

    business_id = account.get("business_id")
    if not business_id:
        # Registered but never finished onboarding: there is no business for
        # the agents to work for yet.
        return respond(409, {"error": "finish setting up your business first"})

    # Deliberately NOT read from the request body. Taking a tenant id from the
    # caller would let any signed-in owner spend model calls against any other
    # business in the cluster.
    runs = repo.recent_runs(business_id, limit=1)
    if runs:
        run = runs[0]
        if run.status == "running":
            return respond(202, {"status": "running",
                                 "message": "the agents are already working"})
        return respond(200, {"status": "done",
                             "message": "this business has already had a night"})

    if not night_function:
        return respond(503, {"error": "the night runner is not configured"})

    invoker.invoke(
        FunctionName=night_function,
        InvocationType="Event",           # fire and forget; see the module docstring
        Payload=json.dumps({"business_id": business_id}).encode("utf-8"),
    )
    return respond(202, {
        "status": "started",
        # The board polls the workflow endpoint; this is roughly how long
        # before there is anything to see.
        "expected_seconds": 90,
    })


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    import boto3
    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        return start_night(
            event,
            repo=PostgresRepository(conn),
            invoker=boto3.client("lambda"),
            night_function=os.environ.get(NIGHT_FUNCTION_VAR),
        )


__all__ = ["handler", "start_night", "NIGHT_FUNCTION_VAR"]
