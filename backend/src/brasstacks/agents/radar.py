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
from dataclasses import dataclass
from datetime import datetime, timezone

from brasstacks.providers import Embedder, EmbeddingError
from brasstacks.repository import Repository, content_hash
from brasstacks.signals import RawSignal, SignalSource

DEFAULT_LIMIT_PER_SOURCE = 50


@dataclass(frozen=True)
class RadarResult:
    run_id: str
    observed: int
    stored: int
    duplicates: int
    failed_sources: tuple[str, ...]

    @property
    def note(self) -> str:
        parts = [f"{self.observed} observed", f"{self.stored} new"]
        if self.duplicates:
            parts.append(f"{self.duplicates} already known")
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


def _usable(signals: Sequence[RawSignal]) -> list[RawSignal]:
    """Drop blanks and collapse duplicates before spending money on embeddings.

    Titan rejects empty input, and embedding the same text twice in one batch is
    wasted spend on a row the database would reject anyway.
    """
    seen: set[str] = set()
    usable: list[RawSignal] = []
    for signal in signals:
        if not signal.content or not signal.content.strip():
            continue
        digest = content_hash(signal.content)
        if digest in seen:
            continue
        seen.add(digest)
        usable.append(signal)
    return usable


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
) -> RadarResult:
    run_id = repo.start_run(business_id, agent="radar")
    observed_at_default = now or datetime.now(timezone.utc)

    signals, failed = _collect(sources, business_name=business_name, city=city,
                               limit=limit_per_source)
    candidates = _usable(signals)

    stored = 0
    within_batch_duplicates = len(signals) - len(candidates)

    try:
        if candidates:
            vectors = embedder.embed([s.content for s in candidates])
            for signal, vector in zip(candidates, vectors):
                observation_id = repo.insert_observation(
                    business_id,
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
    except EmbeddingError as e:
        repo.finish_run(run_id, status="failed", error=str(e))
        raise

    duplicates = len(signals) - stored

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
    )
    repo.finish_run(
        run_id,
        status="failed" if everything_failed else "ok",
        error=f"all sources failed: {', '.join(failed)}" if everything_failed else None,
        note=result.note,
    )
    return result
