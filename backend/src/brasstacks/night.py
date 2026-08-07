"""One night of the loop: Radar observes, the Analyst reasons, the Meter judges.

Also the local end-to-end harness. In production these three run as separate
Lambdas chained by EventBridge; here they run in sequence in one process, which is
what makes the whole loop testable and demonstrable without deploying anything.

    python -m brasstacks.night                      one night, today
    python -m brasstacks.night --nights 3 --step 10  three nights, ten days apart
    python -m brasstacks.night --accept-proposals    act on finds as the owner would

The --step flag exists because verify windows are one to four weeks. Advancing a
simulated night by a single day would never let the Meter reach a verdict, so a
demo run needs to move the calendar the way real time would.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from brasstacks.agents.analyst import AnalystResult, run_analyst
from brasstacks.agents.maker import MakerResult, next_undrafted_find, run_maker
from brasstacks.agents.meter import MeterResult, run_meter
from brasstacks.agents.radar import RadarResult, run_radar
from brasstacks.artifacts import ArtifactStore, build_artifact_store
from brasstacks.competitors import build_competitor_scout
from brasstacks.config import Settings
from brasstacks.outcomes import (
    NoOutcomeSource,
    OutcomeSource,
    build_outcome_source,
)
from brasstacks.providers import Embedder, Reasoner, build_embedder, build_reasoner
from brasstacks.repository import Repository
from brasstacks.signals import (
    CorpusSignalSource,
    SignalSource,
    TavilySignalSource,
    YelpSignalSource,
    offering_from_facts,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Overridable because the packaged Lambda does not have the repository's
#: directory shape, and deriving this from `parents[3]` would silently resolve
#: to the wrong place inside the image.
CORPUS_PATH = Path(
    os.environ.get("BRASSTACKS_CORPUS_PATH")
    or REPO_ROOT / "db" / "seed" / "observations.json"
)


@dataclass(frozen=True)
class NightResult:
    on: date
    radar: RadarResult
    analyst: AnalystResult
    meter: MeterResult
    maker: MakerResult | None = None


def run_night(
    *,
    repo: Repository,
    embedder: Embedder,
    reasoner: Reasoner,
    outcomes: OutcomeSource,
    business_id: str,
    today: date,
    sources: list[SignalSource],
    store: ArtifactStore | None = None,
    scout: Any | None = None,
    accept_proposals: bool = False,
    model_id: str | None = None,
    now: datetime | None = None,
    refuter: Reasoner | None = None,
    coster: Reasoner | None = None,
) -> NightResult:
    """Run the agents in order: observe, reason, do, measure.

    Order matters. Radar's observations are what the Analyst retrieves over. The
    Maker only ever drafts for finds the owner already accepted, so it runs
    after her decision rather than in anticipation of it. The Meter judges finds
    from *earlier* nights, so it runs last and its work is independent of
    tonight's find.

    `store` is optional so the loop still runs where S3 is not configured; the
    Maker is skipped rather than failing the night.

    `refuter` is the adversarial second opinion on the Analyst's own output. It
    is passed down rather than built here so it stays a `Reasoner` the caller
    chose — the same provider interface, so the whole check is swappable and
    fakeable, and so a test can hand it a reasoner that explodes.

    `coster` prices what each surviving move will cost the owner, and it is
    passed down for the same reasons. It never sees what the Analyst predicted
    the move would earn: revenue and cost are estimated independently so the
    second figure is not anchored on the first.
    """
    business = repo.get_business(business_id) if hasattr(repo, "get_business") else None
    # 02:00 UTC is the local harness stepping a simulated calendar, and it is
    # wrong for a real sweep. A signal that carries no time of its own is
    # stamped with this, and the Analyst is now told to convert a capture time
    # to local before reading anything as an outage — so a real 18:00 sweep
    # stamped 02:00 UTC reads as the previous evening, after closing, and the
    # model would rightly refuse the claim or assert the shop was shut. The
    # deployed handler passes the real instant; `today` keeps driving the
    # simulated nights.
    now = now or datetime.combine(today, time(hour=2), tzinfo=timezone.utc)

    radar = run_radar(
        repo=repo, embedder=embedder, business_id=business_id, sources=sources,
        now=now, business_name=(business or {}).get("name", ""),
        city=(business or {}).get("city"),
        scout=scout, today=today,
    )

    analyst = run_analyst(
        repo=repo, embedder=embedder, reasoner=reasoner, business_id=business_id,
        today=today, model_id=model_id, competitors=radar.competitors,
        refuter=refuter, coster=coster,
    )

    # Standing in for the owner tapping "do it now". Only ever used by the local
    # harness — in the product this is a human decision, and the Meter must not
    # be able to manufacture its own inputs.
    if accept_proposals and analyst.find_id:
        repo.set_find_status(analyst.find_id, status="accepted", decided_at=now)

    # Drafts what she has already said yes to — including, when the harness is
    # standing in for her above, tonight's find.
    maker = None
    if store is not None:
        maker = run_maker(
            repo=repo, reasoner=reasoner, store=store,
            business_id=business_id, model_id=model_id,
            find=next_undrafted_find(repo, business_id),
        )

    meter = run_meter(repo=repo, outcomes=outcomes, business_id=business_id,
                      today=today)

    return NightResult(on=today, radar=radar, analyst=analyst, meter=meter,
                       maker=maker)


def build_night_sources(settings: Any, anchor: datetime, *,
                        use_corpus: bool = False,
                        facts: Sequence[str] = ()) -> list[SignalSource]:
    """What a **deployed** night is allowed to observe.

    The opposite polarity to `_build_sources` below, and deliberately so. That
    one serves the local harness against the seeded demo tenant, where the
    committed corpus is the point. This one serves real businesses that signed
    themselves up, where the corpus is 127 hand-written reviews about a
    restaurant that does not exist.

    Feeding those to a real tenant would fill its memory with invented
    customers, and every find reasoned from them would cite fabricated
    evidence — the one thing PRODUCT.md says must never happen.

    So: live web signal when a key is configured, and the fiction only when
    something explicitly asks for it. When neither is available the night
    observes nothing, which is honest. It is not a reason to fall back to
    someone else's imaginary reviews.

    ``facts`` are this tenant's `business_fact` sentences, and they are what
    lets the web source ask about rivals — the offering ("What we sell is Korean
    BBQ.") is the one thing a competitor query cannot be built without. Passing
    them makes this per-tenant, which is why the handler now builds sources
    inside its loop rather than once for everybody, exactly as it already does
    for the competitor scout.
    """
    sources: list[SignalSource] = []
    if getattr(settings, "search_api_key", None):
        sources.append(TavilySignalSource(
            api_key=settings.search_api_key,
            offering=offering_from_facts(facts),
        ))
    if use_corpus:
        sources.append(CorpusSignalSource(CORPUS_PATH, anchor=anchor))
    return sources


def _build_sources(settings: Settings, anchor: datetime, *,
                   include_web: bool, include_yelp: bool = False) -> list[SignalSource]:
    """The committed corpus, plus live web search only when explicitly asked for.

    The corpus is the primary source, not a fallback: scraping review platforms
    violates their terms, so a committed dataset is the honest way to get
    review-shaped signals.

    Live web search is opt-in rather than automatic-when-a-key-exists, because
    the demo tenant is fictional. Searching for a business that does not exist
    returns generic food-industry content — trade-show videos, forum threads,
    years-old market reports — which pollutes the memory layer, costs embedding
    spend, and drags third-party trademarks into a demo where the rules bar them.
    Measured on the first live run: 40 of 40 web signals were irrelevant.

    Against a real business the same source is genuinely useful, which is why it
    stays in the codebase.
    """
    sources: list[SignalSource] = [CorpusSignalSource(CORPUS_PATH, anchor=anchor)]
    if include_web:
        if not settings.search_api_key:
            raise SystemExit("--web needs SEARCH_API_KEY set in .env")
        sources.append(TavilySignalSource(api_key=settings.search_api_key))
    if include_yelp:
        if not settings.yelp_api_key:
            raise SystemExit("--yelp needs YELP_API_KEY set in .env")
        # Off by default for two independent reasons: the demo tenant is
        # fictional and has no Yelp presence, and Yelp's terms cap retention at
        # 24 hours, so anything it contributes is deleted again the next night.
        sources.append(YelpSignalSource(api_key=settings.yelp_api_key))
    return sources


def main(argv: list[str] | None = None) -> int:
    # Every find carries an emoji and Windows consoles default to cp1252, which
    # cannot encode them. Reconfigure rather than strip the emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nights", type=int, default=1,
                        help="how many nights to run (default 1)")
    parser.add_argument("--step", type=int, default=1,
                        help="days between simulated nights (default 1)")
    parser.add_argument("--start", default=None,
                        help="first night's date, YYYY-MM-DD (default today)")
    parser.add_argument("--accept-proposals", action="store_true",
                        help="accept each proposed find, as the owner would")
    parser.add_argument("--yelp", action="store_true",
                        help="also pull Yelp reviews (off by default; the demo "
                             "tenant is fictional, and Yelp caps retention at 24h)")
    parser.add_argument("--web", action="store_true",
                        help="also pull live web signals (noisy for the fictional "
                             "demo tenant; see _build_sources)")
    args = parser.parse_args(argv)

    settings = Settings.load()
    if not settings.business_id:
        raise SystemExit(
            "BRASSTACKS_BUSINESS_ID is not set. Run scripts/seed.py first."
        )

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    start = date.fromisoformat(args.start) if args.start else date.today()
    embedder = build_embedder(settings)
    reasoner = build_reasoner(settings)
    store = build_artifact_store(settings)

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)
        # After the connection, not before: the scout searches around the
        # coordinates on the business row.
        scout = build_competitor_scout(settings, repo.get_business(settings.business_id))
        # Whatever the owner has reported through /finds/{id}/outcome. The
        # local harness judges against the same measurements the deployed
        # nightly run does, or there is no point rehearsing with it.
        outcomes = build_outcome_source(repo, settings.business_id)

        for index in range(args.nights):
            tonight = start + timedelta(days=index * args.step)
            anchor = datetime.combine(tonight, time(hour=2), tzinfo=timezone.utc)

            result = run_night(
                repo=repo,
                embedder=embedder,
                reasoner=reasoner,
                outcomes=outcomes,
                business_id=settings.business_id,
                today=tonight,
                sources=_build_sources(settings, anchor, include_web=args.web,
                                       include_yelp=args.yelp),
                store=store,
                scout=scout,
                accept_proposals=args.accept_proposals,
                model_id=settings.reasoning_model_id,
            )

            print(f"\n=== night {index + 1}/{args.nights} — {tonight.isoformat()} ===")
            print(f"  radar   {result.radar.note}")
            if result.analyst.find_id:
                note = [r for r in repo.recent_runs(settings.business_id, limit=8)
                        if r.run_id == result.analyst.run_id]
                print(f"  analyst {note[0].note if note else 'find proposed'}")
            elif result.analyst.error:
                print(f"  analyst FAILED — {result.analyst.error}")
            else:
                print(f"  analyst no find ({result.analyst.retrieved} retrieved)")
            if result.maker:
                print(f"  maker   {result.maker.note}")
            print(f"  meter   {result.meter.note}")

        summary = repo.ledger_summary(settings.business_id)
        rate = "n/a" if summary.hit_rate is None else f"{summary.hit_rate:.0%}"
        print(f"\nledger: {summary.verified_count} verified, {summary.miss_count} miss, "
              f"{summary.estimated_count} estimated -> {rate} hit rate, "
              f"${summary.verified_daily_cents / 100:,.2f}/day realized")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
