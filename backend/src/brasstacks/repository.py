"""The memory layer's interface, and an in-memory implementation of it.

Agents talk to a Repository, never to SQL. They decide *what* to remember and
*what* to ask; this layer decides *how* storage and retrieval behave — which
index the vector search engages, that a duplicate observation is absorbed rather
than raised, that a find and its evidence are written atomically.

Two implementations satisfy this interface: ``InMemoryRepository`` here, and
``PostgresRepository`` in ``repository_pg``. Both are exercised by the same
contract tests, so the fake cannot drift from real behaviour.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

#: Statuses whose outcome the Meter is allowed to judge. A find the owner never
#: acted on has no outcome to measure.
JUDGEABLE_STATUSES = ("accepted", "live")


class RepositoryError(RuntimeError):
    """A memory-layer operation violated an invariant or failed."""


# ---------------------------------------------------------------------------
# Value types — agents see these, never raw rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Retrieved:
    """An observation returned by vector search, with its retrieval metadata."""

    observation_id: str
    content: str
    kind: str
    similarity: float
    rank: int
    observed_at: datetime
    source_name: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class EvidenceRef:
    """An observation the Analyst cited, and how close retrieval scored it."""

    observation_id: str
    similarity: float


@dataclass(frozen=True)
class StoredEvidence:
    observation_id: str
    similarity: float
    rank: int
    content: str | None = None


@dataclass(frozen=True)
class DueFind:
    """A find whose verify window has elapsed and which has no verdict yet."""

    find_id: str
    title: str
    predicted_daily_cents: int
    verify_after: date
    created_at: datetime


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    agent: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class LedgerSummary:
    verified_count: int
    estimated_count: int
    miss_count: int
    verified_daily_cents: int
    hit_rate: float | None

    @property
    def judged_count(self) -> int:
        return self.verified_count + self.miss_count


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

@runtime_checkable
class Repository(Protocol):
    def create_business(self, *, name: str, category: str, city: str | None = ...,
                        goal_monthly_cents: int | None = ...) -> str: ...

    def start_run(self, business_id: str, *, agent: str,
                  model_id: str | None = ...) -> str: ...

    def finish_run(self, run_id: str, *, status: str, error: str | None = ...,
                   note: str | None = ...) -> None: ...

    def recent_runs(self, business_id: str, *, limit: int) -> list[RunRecord]: ...

    def insert_observation(self, business_id: str, *, content: str, kind: str,
                           embedding: Sequence[float], observed_at: datetime,
                           source_name: str | None = ..., source_url: str | None = ...,
                           subject: str | None = ..., rating: float | None = ...,
                           run_id: str | None = ...) -> bool: ...

    def count_observations(self, business_id: str) -> int: ...

    def search_observations(self, business_id: str, query_embedding: Sequence[float],
                            *, limit: int) -> list[Retrieved]: ...

    def insert_find_with_evidence(
        self, business_id: str, *, title: str, rationale: str, move: str,
        emoji: str, predicted_daily_cents: int, confidence: float,
        verify_after: date, evidence: Sequence[EvidenceRef],
        status: str = ..., run_id: str | None = ...,
    ) -> str: ...

    def get_find_evidence(self, find_id: str) -> list[StoredEvidence]: ...

    def count_finds(self, business_id: str) -> int: ...

    def due_finds(self, business_id: str, *, today: date) -> list[DueFind]: ...

    def insert_ledger_entry(
        self, business_id: str, *, find_id: str, verdict: str,
        predicted_daily_cents: int, actual_daily_cents: int,
        period_start: date, period_end: date, method: str,
        note: str | None = ..., run_id: str | None = ...,
    ) -> str: ...

    def ledger_summary(self, business_id: str) -> LedgerSummary: ...


# ---------------------------------------------------------------------------
# Shared helpers — behaviour both implementations must agree on
# ---------------------------------------------------------------------------

def content_hash(content: str) -> str:
    """Dedup key for an observation.

    Normalizing whitespace and case means a review re-scraped with different
    spacing is recognised as the same content. Both implementations must use
    this, or dedup differs between fake and real.
    """
    normalized = " ".join(content.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def compute_hit_rate(verified: int, miss: int) -> float | None:
    """Verified over judged. ``None`` when nothing has been judged.

    Estimates are excluded: an estimate is not yet a win or a loss, and counting
    it either way would misstate the published record. Returning None rather
    than 0.0 matters too — 0% reads as total failure, "no verdicts yet" does not.
    """
    judged = verified + miss
    if judged == 0:
        return None
    return verified / judged


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------

@dataclass
class _Observation:
    observation_id: str
    business_id: str
    content: str
    kind: str
    embedding: list[float]
    observed_at: datetime
    content_hash: str
    source_name: str | None = None
    source_url: str | None = None
    subject: str | None = None
    rating: float | None = None
    run_id: str | None = None


@dataclass
class _Find:
    find_id: str
    business_id: str
    title: str
    rationale: str
    move: str
    emoji: str
    predicted_daily_cents: int
    confidence: float
    verify_after: date
    status: str
    created_at: datetime
    run_id: str | None = None
    evidence: list[StoredEvidence] = field(default_factory=list)


@dataclass
class _LedgerEntry:
    entry_id: str
    business_id: str
    find_id: str
    verdict: str
    predicted_daily_cents: int
    actual_daily_cents: int
    period_start: date
    period_end: date
    method: str
    note: str | None = None


class InMemoryRepository:
    """Offline stand-in for the real memory layer.

    Implements the same contract, including the parts that are database
    constraints in Postgres — per-business dedup, one verdict per find per
    period, evidence referential integrity — so agent tests exercise real
    semantics rather than a permissive fiction.
    """

    def __init__(self) -> None:
        self._businesses: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, RunRecord] = {}
        self._observations: list[_Observation] = []
        self._finds: dict[str, _Find] = {}
        self._ledger: list[_LedgerEntry] = []
        self._clock = 0

    _EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _now(self) -> datetime:
        # Monotonic synthetic clock: ordering assertions must not depend on
        # wall-clock resolution, which on Windows is coarse enough that two
        # rows created in the same millisecond can tie.
        self._clock += 1
        return self._EPOCH + timedelta(seconds=self._clock)

    # -- businesses ------------------------------------------------------
    def create_business(self, *, name: str, category: str, city: str | None = None,
                        goal_monthly_cents: int | None = None) -> str:
        business_id = str(uuid.uuid4())
        self._businesses[business_id] = {
            "name": name, "category": category, "city": city,
            "goal_monthly_cents": goal_monthly_cents,
        }
        return business_id

    # -- runs ------------------------------------------------------------
    def start_run(self, business_id: str, *, agent: str,
                  model_id: str | None = None) -> str:
        run_id = str(uuid.uuid4())
        self._runs[run_id] = RunRecord(
            run_id=run_id, agent=agent, status="running", started_at=self._now(),
        )
        return run_id

    def finish_run(self, run_id: str, *, status: str, error: str | None = None,
                   note: str | None = None) -> None:
        existing = self._runs.get(run_id)
        if existing is None:
            raise RepositoryError(f"unknown run {run_id}")
        self._runs[run_id] = RunRecord(
            run_id=run_id, agent=existing.agent, status=status,
            started_at=existing.started_at, finished_at=self._now(),
            error=error, note=note,
        )

    def recent_runs(self, business_id: str, *, limit: int) -> list[RunRecord]:
        runs = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    # -- observations ----------------------------------------------------
    def insert_observation(self, business_id: str, *, content: str, kind: str,
                           embedding: Sequence[float], observed_at: datetime,
                           source_name: str | None = None,
                           source_url: str | None = None,
                           subject: str | None = None, rating: float | None = None,
                           run_id: str | None = None) -> bool:
        digest = content_hash(content)
        # Dedup is scoped per business: two restaurants can share a phrase.
        for existing in self._observations:
            if existing.business_id == business_id and existing.content_hash == digest:
                return False
        self._observations.append(_Observation(
            observation_id=str(uuid.uuid4()), business_id=business_id,
            content=content, kind=kind, embedding=list(embedding),
            observed_at=observed_at, content_hash=digest, source_name=source_name,
            source_url=source_url, subject=subject, rating=rating, run_id=run_id,
        ))
        return True

    def count_observations(self, business_id: str) -> int:
        return sum(1 for o in self._observations if o.business_id == business_id)

    def search_observations(self, business_id: str, query_embedding: Sequence[float],
                            *, limit: int) -> list[Retrieved]:
        scored = [
            (cosine_similarity(query_embedding, o.embedding), o)
            for o in self._observations if o.business_id == business_id
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            Retrieved(
                observation_id=o.observation_id, content=o.content, kind=o.kind,
                similarity=similarity, rank=rank, observed_at=o.observed_at,
                source_name=o.source_name, subject=o.subject,
            )
            for rank, (similarity, o) in enumerate(scored[:limit])
        ]

    # -- finds -----------------------------------------------------------
    def insert_find_with_evidence(
        self, business_id: str, *, title: str, rationale: str, move: str,
        emoji: str, predicted_daily_cents: int, confidence: float,
        verify_after: date, evidence: Sequence[EvidenceRef],
        status: str = "proposed", run_id: str | None = None,
    ) -> str:
        if not evidence:
            raise RepositoryError(
                "a find must cite at least one observation as evidence — a "
                "recommendation with no traceable source cannot be defended"
            )

        known = {o.observation_id for o in self._observations
                 if o.business_id == business_id}
        for ref in evidence:
            if ref.observation_id not in known:
                # Mirrors the real FK: nothing is written if any ref is bad.
                raise RepositoryError(
                    f"evidence references unknown observation {ref.observation_id}"
                )

        find_id = str(uuid.uuid4())
        self._finds[find_id] = _Find(
            find_id=find_id, business_id=business_id, title=title,
            rationale=rationale, move=move, emoji=emoji,
            predicted_daily_cents=predicted_daily_cents, confidence=confidence,
            verify_after=verify_after, status=status, created_at=self._now(),
            run_id=run_id,
            evidence=[
                StoredEvidence(observation_id=ref.observation_id,
                               similarity=ref.similarity, rank=rank)
                for rank, ref in enumerate(evidence)
            ],
        )
        return find_id

    def get_find_evidence(self, find_id: str) -> list[StoredEvidence]:
        found = self._finds.get(find_id)
        if found is None:
            raise RepositoryError(f"unknown find {find_id}")
        contents = {o.observation_id: o.content for o in self._observations}
        return [
            StoredEvidence(e.observation_id, e.similarity, e.rank,
                           contents.get(e.observation_id))
            for e in sorted(found.evidence, key=lambda e: e.rank)
        ]

    def count_finds(self, business_id: str) -> int:
        return sum(1 for f in self._finds.values() if f.business_id == business_id)

    def due_finds(self, business_id: str, *, today: date) -> list[DueFind]:
        judged = {entry.find_id for entry in self._ledger}
        due = [
            f for f in self._finds.values()
            if f.business_id == business_id
            and f.status in JUDGEABLE_STATUSES
            and f.verify_after <= today
            and f.find_id not in judged
        ]
        due.sort(key=lambda f: (f.verify_after, f.created_at))
        return [
            DueFind(find_id=f.find_id, title=f.title,
                    predicted_daily_cents=f.predicted_daily_cents,
                    verify_after=f.verify_after, created_at=f.created_at)
            for f in due
        ]

    # -- ledger ----------------------------------------------------------
    def insert_ledger_entry(
        self, business_id: str, *, find_id: str, verdict: str,
        predicted_daily_cents: int, actual_daily_cents: int,
        period_start: date, period_end: date, method: str,
        note: str | None = None, run_id: str | None = None,
    ) -> str:
        for entry in self._ledger:
            if (entry.find_id == find_id and entry.period_start == period_start
                    and entry.period_end == period_end):
                # Mirrors ledger_period_idx: the ledger is append-only per
                # window, so a recorded miss can never be overwritten.
                raise RepositoryError(
                    f"find {find_id} already has a verdict for "
                    f"{period_start}..{period_end}"
                )
        entry_id = str(uuid.uuid4())
        self._ledger.append(_LedgerEntry(
            entry_id=entry_id, business_id=business_id, find_id=find_id,
            verdict=verdict, predicted_daily_cents=predicted_daily_cents,
            actual_daily_cents=actual_daily_cents, period_start=period_start,
            period_end=period_end, method=method, note=note,
        ))
        return entry_id

    def ledger_summary(self, business_id: str) -> LedgerSummary:
        entries = [e for e in self._ledger if e.business_id == business_id]
        verified = [e for e in entries if e.verdict == "verified"]
        return LedgerSummary(
            verified_count=len(verified),
            estimated_count=sum(1 for e in entries if e.verdict == "estimated"),
            miss_count=sum(1 for e in entries if e.verdict == "miss"),
            verified_daily_cents=sum(e.actual_daily_cents for e in verified),
            hit_rate=compute_hit_rate(
                len(verified), sum(1 for e in entries if e.verdict == "miss")),
        )
