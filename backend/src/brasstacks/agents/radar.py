"""Radar — the agent that observes, and remembers.

Radar does no reasoning. It reads the night, embeds what it finds, and writes it
to memory. Interpretation is the Analyst's job. Keeping them separate means the
corpus accumulates regardless of whether any given night produced a good idea.

Every source is best-effort. Signal sources are the least reliable part of the
system, and a search API outage should cost us that source's observations, not
the night's run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from brasstacks.agent_runs import closing_run
from brasstacks.providers import Embedder, EmbeddingError
from brasstacks.repository import Repository, content_hash
from brasstacks.signals import (
    RawSignal,
    SignalSource,
    classify_statement,
    clean_observation_text,
)

DEFAULT_LIMIT_PER_SOURCE = 50


@dataclass(frozen=True)
class RadarResult:
    run_id: str
    observed: int
    stored: int
    duplicates: int
    failed_sources: tuple[str, ...]
    #: Rows deleted to honour a source's retention licence. Surfaced in the note
    #: so the audit trail shows compliance happening rather than implying it.
    expired: int = 0
    #: Scraped rows that were nothing but page furniture once cleaned. In the
    #: note because an ingest rule that silently eats a corpus is worse than no
    #: ingest rule — this is the number an operator checks when a tenant's
    #: memory stops growing.
    dropped: int = 0
    #: Tonight's street, passed through to the Analyst and **never stored**.
    #: Google's Places terms permit keeping place_id and nothing else, so this
    #: is the one thing Radar observes and does not commit to memory. Pinned by
    #: test_competitors.TestRadarIntegration.
    competitors: tuple = ()

    @property
    def note(self) -> str:
        parts = [f"{self.observed} observed", f"{self.stored} new"]
        if self.dropped:
            parts.append(f"{self.dropped} dropped as page furniture")
        if self.duplicates:
            parts.append(f"{self.duplicates} already known")
        if self.expired:
            parts.append(f"{self.expired} expired past their retention window")
        if self.failed_sources:
            parts.append(f"sources failed: {', '.join(self.failed_sources)}")
        return "; ".join(parts)


def _collect(sources: Sequence[SignalSource], *, business_name: str,
             city: str | None, limit: int) -> tuple[list[RawSignal], list[str]]:
    signals: list[RawSignal] = []
    failed: list[str] = []
    for source in sources:
        try:
            signals.extend(source.fetch(business_name=business_name, city=city,
                                        limit=limit))
        except Exception:
            # Intentionally broad: a source can fail in any number of ways
            # (HTTP, JSON, rate limit) and none of them should end the night.
            failed.append(getattr(source, "name", type(source).__name__))
    return signals, failed


def _enforce_retention(repo: Repository, business_id: str,
                       sources: Sequence[SignalSource],
                       now: datetime) -> int:
    """Delete anything a source is no longer licensed to hold.

    Runs every night, for every source that declares a window — not only when
    that source returned something. Yelp being unreachable, or the business
    having no Yelp presence, does not extend the licence on rows already stored.

    Only sources actually configured for this run are purged: deleting Yelp rows
    on a night nobody asked for Yelp would be a surprising side effect.
    """
    expired = 0
    for source in sources:
        hours = getattr(source, "retention_hours", None)
        if not hours:
            continue
        expired += repo.purge_observations(
            business_id,
            source_name=getattr(source, "name", ""),
            older_than=now - timedelta(hours=hours),
        )
    return expired


def _usable(signals: Sequence[RawSignal], *, business_name: str = "",
            address: str | None = None) -> tuple[list[RawSignal], int]:
    """Clean, then drop blanks and collapse duplicates, then embed.

    Order is the point. Hygiene runs *before* the hash, or a repaired and an
    unrepaired capture of one page are two different rows and dedup never sees
    them — which is how one Grubhub storefront became the three "independent
    captures" that find 7c4a9124 cited. It also runs before the embedder,
    because roughly 40% of every live corpus was navigation and carousels and
    we were paying Titan to remember it.

    Only rows carrying a ``source_url`` are cleaned. Those are the ones scraped
    off somebody else's page. The committed corpus and owner uploads have no URL,
    are ours, and are not second-guessed here.

    Titan also rejects empty input, and embedding the same text twice in one
    batch is wasted spend on a row the database would reject anyway.
    """
    seen: set[str] = set()
    usable: list[RawSignal] = []
    dropped = 0
    for signal in signals:
        content = signal.content or ""
        if signal.source_url and content.strip():
            cleaned = clean_observation_text(
                content, business_name=business_name, address=address,
                source_url=signal.source_url)
            if cleaned != content:
                content = cleaned
                signal = replace(signal, content=content)
                if not content.strip():
                    dropped += 1
        if not content.strip():
            continue
        digest = content_hash(content)
        if digest in seen:
            continue
        seen.add(digest)
        usable.append(signal)
    return usable, dropped


def _insert(repo: Repository, business_id: str, *, statement_type: str,
            **fields: Any) -> str | None:
    """Write one observation with the label Radar gave it.

    This was a signature-sniffing shim for one round, while the column existed
    and the write path did not accept the keyword. The consequence of that gap
    is the reason the shim is worth a note: Radar classified every row, the
    shim silently took the branch that dropped the answer, and the feature
    reported as shipped while every row went to the database NULL. The test
    covering it passed against a subclass written in the test file purely to
    have the signature the real repositories lacked.
    """
    return repo.insert_observation(business_id, statement_type=statement_type,
                                   **fields)


def run_radar(
    *,
    repo: Repository,
    embedder: Embedder,
    business_id: str,
    sources: Sequence[SignalSource],
    now: datetime | None = None,
    limit_per_source: int = DEFAULT_LIMIT_PER_SOURCE,
    business_name: str = "",
    city: str | None = None,
    scout: Any | None = None,
    today: date | None = None,
) -> RadarResult:
    run_id = repo.start_run(business_id, agent="radar")
    with closing_run(repo, run_id):
        return _radar_sweep(
            run_id=run_id, repo=repo, embedder=embedder, business_id=business_id,
            sources=sources, now=now, limit_per_source=limit_per_source,
            business_name=business_name, city=city, scout=scout, today=today,
        )


def _radar_sweep(
    *,
    run_id: str,
    repo: Repository,
    embedder: Embedder,
    business_id: str,
    sources: Sequence[SignalSource],
    now: datetime | None,
    limit_per_source: int,
    business_name: str,
    city: str | None,
    scout: Any | None,
    today: date | None,
) -> RadarResult:
    observed_at_default = now or datetime.now(timezone.utc)

    # The one thing Radar looks at and does not remember. Google's Places terms
    # permit storing place_id and nothing else, so this deliberately bypasses
    # the signal → embed → insert path that everything else here travels. Best
    # effort, like every outside-world call: losing tonight's street costs the
    # Analyst context, never the night.
    competitors: tuple = ()
    if scout is not None:
        try:
            competitors = tuple(scout.scan(on=today or observed_at_default.date()))
        except Exception:
            competitors = ()

    signals, failed = _collect(sources, business_name=business_name, city=city,
                               limit=limit_per_source)
    # `city` carries the whole street address for tenants that signed up through
    # the deployed onboarding flow, and a plain city name for the seeded one.
    # Hygiene copes with either; it simply never runs the address test when
    # there is no street number to run it against.
    candidates, dropped = _usable(signals, business_name=business_name,
                                  address=city)

    stored = 0

    # An embedding outage ends the sweep. `closing_run` in the caller marks the
    # run failed on the way out — an inner handler here would only duplicate it.
    if candidates:
        vectors = embedder.embed([s.content for s in candidates])
        for signal, vector in zip(candidates, vectors):
            observation_id = _insert(
                repo,
                business_id,
                statement_type=classify_statement(signal.content),
                content=signal.content,
                kind=signal.kind,
                embedding=vector,
                observed_at=signal.observed_at or observed_at_default,
                source_name=signal.source_name,
                source_url=signal.source_url,
                subject=signal.subject,
                rating=signal.rating,
                run_id=run_id,
            )
            if observation_id is not None:
                stored += 1

    duplicates = len(signals) - dropped - stored
    expired = _enforce_retention(repo, business_id, sources, observed_at_default)

    # A night where every source failed observed nothing, which is a real
    # failure. A night where one of several failed is a normal partial night —
    # the note carries the detail either way, for the audit trail.
    everything_failed = bool(failed) and not candidates
    result = RadarResult(
        run_id=run_id,
        observed=len(signals),
        stored=stored,
        duplicates=duplicates,
        failed_sources=tuple(failed),
        expired=expired,
        dropped=dropped,
        competitors=competitors,
    )
    repo.finish_run(
        run_id,
        status="failed" if everything_failed else "ok",
        error=f"all sources failed: {', '.join(failed)}" if everything_failed else None,
        note=result.note,
    )
    return result

