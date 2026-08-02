"""The nightly run, as a Lambda.

EventBridge Scheduler wakes this once a night. It is what makes the loop
autonomous rather than a button, which is the claim the product rests on.

One function rather than three chained ones. `run_night()` already sequences
Radar → Analyst → Maker → Meter and is covered by the offline suite; splitting
it across three Lambdas would move that ordering into infrastructure, where it
is less tested, and buy cross-invoke IAM and three new failure modes for
nothing. A night takes single-digit minutes against a fifteen-minute ceiling.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from typing import Any

from brasstacks.artifacts import build_artifact_store
from brasstacks.competitors import build_competitor_scout
from brasstacks.config import Settings
from brasstacks.night import build_night_sources, run_night
from brasstacks.outcomes import NoOutcomeSource
from brasstacks.providers import build_embedder, build_reasoner
from brasstacks.secrets import hydrate_environment


def summarise(result: Any) -> dict[str, Any]:
    """The night, small enough to read in a CloudWatch log line.

    The Analyst's error is reported rather than swallowed: a night that
    retrieved plenty and then failed to reason looks identical to a quiet night
    unless the error survives into the summary.
    """
    maker = getattr(result, "maker", None)
    return {
        "radar": result.radar.note,
        "analyst": {
            "find_id": result.analyst.find_id,
            "retrieved": result.analyst.retrieved,
            "error": result.analyst.error,
        },
        "maker": maker.note if maker else None,
        "meter": result.meter.note,
    }


#: Tenants served per scheduled run. Each costs a Tavily search, ~50 embeddings
#: and a Claude call, so this is a spend control rather than pagination: signup
#: is public behind a shared invite code, and an afternoon of curious sign-ups
#: must not be able to multiply the nightly bill without limit. Raising it is a
#: deliberate decision about money.
MAX_TENANTS_PER_NIGHT = 10


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    today = date.today()
    anchor = datetime.combine(today, time(hour=2), tzinfo=timezone.utc)

    embedder = build_embedder(settings)
    reasoner = build_reasoner(settings)
    store = build_artifact_store(settings)
    sources = build_night_sources(
        settings, anchor,
        # Live web signal for a real tenant; the committed corpus only if
        # BRASSTACKS_USE_CORPUS is set. Tenants sign themselves up now, and
        # db/seed/observations.json is fiction — importing it as a real
        # business's memory would have every find citing invented evidence.
        use_corpus=bool(os.environ.get("BRASSTACKS_USE_CORPUS")),
    )

    nights: list[dict[str, Any]] = []
    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)

        tenants = repo.active_business_ids(limit=MAX_TENANTS_PER_NIGHT)
        if not tenants and settings.business_id:
            # A cluster provisioned before `status` existed. One configured id
            # is still a night rather than a silent no-op.
            tenants = [settings.business_id]

        for business_id in tenants:
            business = repo.get_business(business_id)
            try:
                result = run_night(
                    repo=repo,
                    embedder=embedder,
                    reasoner=reasoner,
                    store=store,
                    # Per tenant: the coordinates the scout searches around live
                    # on the business row, so one scout for every tenant would
                    # point them all at the same street.
                    scout=build_competitor_scout(settings, business),
                    outcomes=NoOutcomeSource(),
                    business_id=business_id,
                    today=today,
                    sources=sources,
                    model_id=settings.reasoning_model_id,
                )
            except Exception as e:
                # One tenant's night must not cost the others theirs. The error
                # is reported rather than swallowed — a run that failed and a
                # run that found nothing look identical otherwise.
                nights.append({
                    "business_id": business_id,
                    "name": (business or {}).get("name"),
                    "error": f"{type(e).__name__}: {e}",
                })
                continue

            nights.append({
                "business_id": business_id,
                "name": (business or {}).get("name"),
                **summarise(result),
            })

    return {"tenants": len(nights), "nights": nights}
