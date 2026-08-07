"""The nightly run, as a Lambda.

EventBridge Scheduler wakes this once a night. It is what makes the loop
autonomous rather than a button, which is the claim the product rests on.

The scheduled run handles Radar → Analyst → Meter. Maker is event-driven in the
deployed product: Do it wakes a dedicated worker immediately, and a lightweight
reconciliation sweep drains any accepted backlog. Keeping Maker out of the
night prevents the two Lambda functions from drafting the same find at once.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from typing import Any

from brasstacks.competitors import build_competitor_scout
from brasstacks.config import Settings
from brasstacks.night import build_night_sources, run_night
from brasstacks.outcomes import build_outcome_source
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
    # The adversarial second opinion on what the Analyst just wrote. A separate
    # instance rather than the same object: `last_usage` is per-client state and
    # sharing one would make the Analyst's token receipt report the refuter's
    # call. Same provider, so this costs nothing but the call itself — and if it
    # fails, `refute_finds` publishes the deck without its dollar figures rather
    # than blanking the board.
    refuter = build_reasoner(settings)
    # Live web signal for a real tenant; the committed corpus only if
    # BRASSTACKS_USE_CORPUS is set. Tenants sign themselves up now, and
    # db/seed/observations.json is fiction — importing it as a real business's
    # memory would have every find citing invented evidence.
    #
    # Built per tenant inside the loop below rather than once here: the web
    # source's query plan now depends on what this business sells, and one
    # shared source would ask every tenant's questions about the first one's
    # menu. Same reason the competitor scout is already built per tenant.
    use_corpus = bool(os.environ.get("BRASSTACKS_USE_CORPUS"))

    nights: list[dict[str, Any]] = []
    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)

        # A named tenant runs alone. That is the on-demand path: an owner who
        # just finished onboarding presses a button and waits for their first
        # night, and making them wait behind nine other businesses' model calls
        # would be minutes of nothing.
        requested = str((event or {}).get("business_id") or "").strip()
        if requested:
            if repo.get_business(requested) is None:
                raise RuntimeError(f"no such business {requested}")
            tenants = [requested]
        else:
            tenants = repo.active_business_ids(limit=MAX_TENANTS_PER_NIGHT)
            if not tenants and settings.business_id:
                # A cluster provisioned before `status` existed. One configured
                # id is still a night rather than a silent no-op.
                tenants = [settings.business_id]

        for business_id in tenants:
            business = repo.get_business(business_id)
            # Best-effort: a tenant with no facts yet still gets the reviews,
            # experience and listing queries — it loses only the rival one.
            try:
                facts = repo.get_business_facts(business_id)
            except Exception:
                facts = []
            sources = build_night_sources(
                settings, anchor, use_corpus=use_corpus, facts=facts,
            )
            try:
                result = run_night(
                    repo=repo,
                    embedder=embedder,
                    reasoner=reasoner,
                    refuter=refuter,
                    # The deployed Maker owns all drafting. The local run_night
                    # harness can still pass a store when testing the complete
                    # loop in one process.
                    store=None,
                    # Per tenant: the coordinates the scout searches around live
                    # on the business row, so one scout for every tenant would
                    # point them all at the same street.
                    scout=build_competitor_scout(settings, business),
                    # Per tenant, and read fresh each night: this is what the
                    # owner reported through /finds/{id}/outcome. Without it
                    # the Meter has no measurements at all and every verdict it
                    # can ever reach is an estimate.
                    outcomes=build_outcome_source(repo, business_id),
                    business_id=business_id,
                    today=today,
                    sources=sources,
                    model_id=settings.reasoning_model_id,
                    # The real instant, not the simulated 02:00. A signal with
                    # no time of its own is stamped with this, and the Analyst
                    # now reads capture times against opening hours before it
                    # will call anything broken.
                    now=datetime.now(timezone.utc),
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
