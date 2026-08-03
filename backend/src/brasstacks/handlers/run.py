"""The owner asking for a night, now.

The nightly schedule is the product's spine, but a business that signed up this
afternoon should not have to wait until 6 AM to see whether any of this works —
and an owner who has decided on everything the agents found should not have to
wait for a schedule that may be switched off.

**Asynchronous, and it has to be.** API Gateway caps an HTTP integration at 30
seconds; a night runs 60-90. A synchronous trigger would return 504 while the
work carried on invisibly, and the owner would press it again. So this validates,
fires the night with `InvocationType="Event"`, and returns 202 immediately. The
board then polls the workflow endpoint until finds appear.

**It spends money, so it is guarded three ways.** A night costs a Tavily search,
roughly fifty Bedrock embeddings and a Claude call:

* The caller must hold a session, and gets a night only for *their own* business
  — a business id in the request body would let anyone spend against any tenant.
* A night already in flight is returned rather than started again, which is what
  a double-click and an impatient reload would otherwise do.
* One night's worth of recommendations per day. Not one ever.

**Why a cooldown rather than the lifetime guard it replaces.** The original rule
was "this business has already had a night", which was right while this was the
last step of onboarding and wrong the moment an owner acted on everything they
were given. Yellow Cow Korean BBQ accepted all three of its finds, the nightly
schedule was disabled, and the account had no path to a fourth recommendation
from any direction. A cooldown keeps the spend bounded without making the
product terminal.

**Why the in-flight check is bounded too.** A Lambda killed by a timeout cannot
close its own `agent_run` row, so `running` is not by itself proof that anything
is running. An unbounded check turned one abandoned row into a permanent refusal.
Past `STALE_RUN_AFTER` the row is treated as debris; `brasstacks.agent_runs`
closes it in-process wherever that is still possible.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token, respond
from brasstacks.secrets import hydrate_environment

#: Set on this function by the template so it knows what to invoke.
NIGHT_FUNCTION_VAR = "BRASSTACKS_NIGHT_FUNCTION"

#: A night runs 60-90 seconds. Well past that, an open run row is debris left
#: by a process that died rather than work anyone is waiting on.
STALE_RUN_AFTER = timedelta(minutes=10)

#: One night's recommendations per day per tenant. Manual presses are capped
#: here; the 6 AM schedule is unaffected.
NIGHT_COOLDOWN = timedelta(hours=24)


def _fresh(started_at: datetime | None, moment: datetime) -> bool:
    """Is an open run recent enough to be believed?

    A missing or unparseable start is treated as stale. The failure mode of
    guessing "fresh" is an owner locked out of their own agents; the failure
    mode of guessing "stale" is one duplicated night.
    """
    if started_at is None:
        return False
    return moment - started_at < STALE_RUN_AFTER


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
    if runs and runs[0].status == "running" and _fresh(runs[0].started_at, moment):
        return respond(202, {"status": "running",
                             "message": "the agents are already working"})

    # Whether a night HAPPENED is the wrong question; whether it produced
    # anything, and how recently, is the right one. A business whose night
    # failed — a truncated model response, a search outage — has seen no
    # recommendation and waits for nothing. The failure is recorded on the
    # agent_run row either way.
    latest_find = repo.latest_find_created_at(business_id)
    if latest_find is not None and moment - latest_find < NIGHT_COOLDOWN:
        return respond(200, {
            "status": "cooldown",
            "message": "tonight's moves are already in. The agents look again "
                       "tomorrow.",
            "nextNightAt": (latest_find + NIGHT_COOLDOWN).isoformat(),
        })

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
