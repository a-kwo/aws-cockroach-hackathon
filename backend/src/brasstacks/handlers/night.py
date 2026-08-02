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

from datetime import date, datetime, time, timezone
from typing import Any

from brasstacks.artifacts import build_artifact_store
from brasstacks.competitors import build_competitor_scout
from brasstacks.config import Settings
from brasstacks.night import CORPUS_PATH, run_night
from brasstacks.outcomes import NoOutcomeSource
from brasstacks.providers import build_embedder, build_reasoner
from brasstacks.secrets import hydrate_environment
from brasstacks.signals import CorpusSignalSource


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


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    if not settings.business_id:
        raise RuntimeError(
            "BRASSTACKS_BUSINESS_ID is not set. The scheduled run has no "
            "tenant to work for."
        )

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    today = date.today()
    anchor = datetime.combine(today, time(hour=2), tzinfo=timezone.utc)

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)
        # Built here rather than from settings alone: the coordinates the scout
        # searches around live on the business row, so the tenant has to be
        # known before the scout can exist.
        scout = build_competitor_scout(settings, repo.get_business(settings.business_id))
        result = run_night(
            repo=repo,
            embedder=build_embedder(settings),
            reasoner=build_reasoner(settings),
            store=build_artifact_store(settings),
            scout=scout,
            outcomes=NoOutcomeSource(),
            business_id=settings.business_id,
            today=today,
            # The committed corpus only. Live web search is opt-in even
            # locally, because the demo tenant is fictional and searching for
            # it returns noise that pollutes the memory layer.
            sources=[CorpusSignalSource(CORPUS_PATH, anchor=anchor)],
            model_id=settings.reasoning_model_id,
        )

    return summarise(result)
