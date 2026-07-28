"""CockroachDB implementation of the memory layer.

Every SQL statement in the project lives here. Agents call the Repository
interface and never see a query, so changing the distance operator, adding a
recency filter, or moving dedup from a unique index to an application check
touches this file and nothing else.

Verified by the same contract tests as ``InMemoryRepository`` — run them against
a live cluster with ``pytest -m integration``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import psycopg

from brasstacks.repository import (
    JUDGEABLE_STATUSES,
    DueFind,
    EvidenceRef,
    FindSummary,
    LedgerSummary,
    OwnerRule,
    RepositoryError,
    Retrieved,
    RunRecord,
    StoredEvidence,
    compute_hit_rate,
    content_hash,
)


def _vector_literal(embedding: Sequence[float]) -> str:
    """Render an embedding as a CockroachDB VECTOR literal.

    psycopg has no native adapter for the VECTOR type, so it is passed as a
    string and cast in SQL with ``::VECTOR``.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class PostgresRepository:
    """Repository backed by a CockroachDB connection.

    The connection's transaction mode is the caller's business. Methods that must
    be atomic use an explicit savepoint (``with self._conn.transaction()``) so
    they behave correctly whether the caller runs in autocommit or manages a
    surrounding transaction.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # -- businesses ------------------------------------------------------
    def create_business(self, *, name: str, category: str, city: str | None = None,
                        goal_monthly_cents: int | None = None) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO business (name, category, city, goal_monthly_cents)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (name, category, city, goal_monthly_cents),
            )
            return str(cur.fetchone()[0])

    def get_business(self, business_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, category, city, region, goal_monthly_cents, goal_note
                FROM business WHERE id = %s
                """,
                (business_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ("id", "name", "category", "city", "region",
                "goal_monthly_cents", "goal_note")
        return {k: (str(v) if k == "id" else v) for k, v in zip(keys, row)}

    # -- profile ---------------------------------------------------------
    def insert_business_fact(self, business_id: str, *, fact: str, source: str,
                             embedding: Sequence[float],
                             confidence: float = 1.0) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO business_fact
                    (business_id, fact, source, confidence, embedding)
                VALUES (%s, %s, %s, %s, %s::VECTOR)
                RETURNING id
                """,
                (business_id, fact, source, confidence, _vector_literal(embedding)),
            )
            return str(cur.fetchone()[0])

    def supersede_business_fact(self, fact_id: str, *, superseded_by: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE business_fact SET superseded_by = %s WHERE id = %s",
                (superseded_by, fact_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown fact {fact_id}")

    def get_business_facts(self, business_id: str) -> list[str]:
        """Current profile facts — what only the owner knows.

        Superseded facts are excluded: memory keeps the history, since a former
        price is part of the record, but the Analyst must reason over what is
        true now.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT fact FROM business_fact
                WHERE business_id = %s AND superseded_by IS NULL
                ORDER BY learned_at DESC
                """,
                (business_id,),
            )
            return [r[0] for r in cur.fetchall()]

    def insert_owner_rule(self, business_id: str, *, rule: str, enabled: bool = True,
                          cap_cents: int | None = None) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO owner_rule (business_id, rule, enabled, cap_cents)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (business_id, rule, enabled, cap_cents),
            )
            return str(cur.fetchone()[0])

    def get_owner_rules(self, business_id: str) -> list[OwnerRule]:
        """Enabled rules only — a rule the owner switched off must not constrain
        tonight's run."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, rule, cap_cents FROM owner_rule
                WHERE business_id = %s AND enabled = true
                ORDER BY created_at
                """,
                (business_id,),
            )
            return [
                OwnerRule(rule_id=str(r[0]), rule=r[1], cap_cents=r[2])
                for r in cur.fetchall()
            ]

    # -- runs ------------------------------------------------------------
    def start_run(self, business_id: str, *, agent: str,
                  model_id: str | None = None) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_run (business_id, agent, status, model_id)
                VALUES (%s, %s, 'running', %s)
                RETURNING id
                """,
                (business_id, agent, model_id),
            )
            return str(cur.fetchone()[0])

    def finish_run(self, run_id: str, *, status: str, error: str | None = None,
                   note: str | None = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_run
                SET status = %s, finished_at = clock_timestamp(), error = %s, note = %s
                WHERE id = %s
                """,
                (status, error, note, run_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown run {run_id}")

    def recent_runs(self, business_id: str, *, limit: int) -> list[RunRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, agent, status, started_at, finished_at, error, note
                FROM agent_run
                WHERE business_id = %s
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (business_id, limit),
            )
            return [
                RunRecord(run_id=str(r[0]), agent=str(r[1]), status=str(r[2]),
                          started_at=r[3], finished_at=r[4], error=r[5], note=r[6])
                for r in cur.fetchall()
            ]

    # -- observations ----------------------------------------------------
    def insert_observation(self, business_id: str, *, content: str, kind: str,
                           embedding: Sequence[float], observed_at: datetime,
                           source_name: str | None = None,
                           source_url: str | None = None,
                           subject: str | None = None, rating: float | None = None,
                           run_id: str | None = None) -> str | None:
        """Store an observation. Returns its id, or None if it was a duplicate.

        A duplicate is normal operation — Radar re-reads the same review nightly —
        so the unique index absorbs it via ON CONFLICT rather than raising and
        aborting a run. Returning the id rather than a bool lets a caller wire
        the stored row straight into find_evidence without a second lookup.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO observation (
                    business_id, run_id, kind, content, source_name, source_url,
                    subject, rating, observed_at, content_hash, embedding
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::VECTOR)
                ON CONFLICT (business_id, content_hash) DO NOTHING
                RETURNING id
                """,
                (business_id, run_id, kind, content, source_name, source_url,
                 subject, rating, observed_at, content_hash(content),
                 _vector_literal(embedding)),
            )
            row = cur.fetchone()
            return str(row[0]) if row is not None else None

    def count_observations(self, business_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM observation WHERE business_id = %s",
                (business_id,),
            )
            return int(cur.fetchone()[0])

    def search_observations(self, business_id: str, query_embedding: Sequence[float],
                            *, limit: int) -> list[Retrieved]:
        """Semantic retrieval over everything ever observed for this business.

        This is the memory retrieval the whole product rests on. Notes:

        * ``<=>`` is cosine distance, matching the index's ``vector_cosine_ops``.
          Using a different operator would silently bypass the index.
        * ``business_id`` is the index's prefix column, so the equality filter is
          what makes the vector index engage — and it is also the tenant
          boundary.
        * Similarity is reported as ``1 - distance`` because callers reason about
          closeness, and ``find_evidence.similarity`` stores it that way.
        """
        literal = _vector_literal(query_embedding)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, kind, source_name, subject, observed_at,
                       1 - (embedding <=> %s::VECTOR) AS similarity
                FROM observation
                WHERE business_id = %s AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::VECTOR
                LIMIT %s
                """,
                (literal, business_id, literal, limit),
            )
            rows = cur.fetchall()

        return [
            Retrieved(
                observation_id=str(r[0]), content=r[1], kind=str(r[2]),
                source_name=r[3], subject=r[4], observed_at=r[5],
                similarity=float(r[6]), rank=rank,
            )
            for rank, r in enumerate(rows)
        ]

    # -- finds -----------------------------------------------------------
    def insert_find_with_evidence(
        self, business_id: str, *, title: str, rationale: str, move: str,
        emoji: str, predicted_daily_cents: int, confidence: float,
        verify_after: date, evidence: Sequence[EvidenceRef],
        status: str = "proposed", run_id: str | None = None,
        created_at: datetime | None = None, decided_at: datetime | None = None,
    ) -> str:
        """Write a find and its evidence atomically.

        The invariant this enforces is the one the submission rests on: a
        recommendation without the retrieved rows that produced it must be
        impossible to create. Two separate calls would leave a window where a
        crash yields a find with no receipt, so both live in one transaction and
        a bad evidence reference rolls the find back with it.
        """
        if not evidence:
            raise RepositoryError(
                "a find must cite at least one observation as evidence — a "
                "recommendation with no traceable source cannot be defended"
            )

        try:
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    # created_at/decided_at are overridable so seeded history
                    # reads as history. coalesce keeps the live path on now().
                    cur.execute(
                        """
                        INSERT INTO find (
                            business_id, run_id, emoji, title, rationale, move,
                            predicted_daily_cents, confidence, verify_after,
                            status, created_at, decided_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                coalesce(%s, clock_timestamp()), %s)
                        RETURNING id
                        """,
                        (business_id, run_id, emoji, title, rationale, move,
                         predicted_daily_cents, confidence, verify_after, status,
                         created_at, decided_at),
                    )
                    find_id = str(cur.fetchone()[0])

                    cur.executemany(
                        """
                        INSERT INTO find_evidence
                            (find_id, observation_id, similarity, rank)
                        VALUES (%s, %s, %s, %s)
                        """,
                        [(find_id, ref.observation_id, ref.similarity, rank)
                         for rank, ref in enumerate(evidence)],
                    )
        except psycopg.Error as e:
            raise RepositoryError(f"could not store find with evidence: {e}") from e

        return find_id

    def set_find_status(self, find_id: str, *, status: str,
                        decided_at: datetime | None = None) -> None:
        """Record the owner's decision on a find.

        Only 'accepted' and 'live' are judgeable, so this is the gate between a
        proposal and something the Meter will hold us to.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE find
                SET status = %s, decided_at = coalesce(%s, clock_timestamp())
                WHERE id = %s
                """,
                (status, decided_at, find_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown find {find_id}")

    def get_find_evidence(self, find_id: str) -> list[StoredEvidence]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT fe.observation_id, fe.similarity, fe.rank, o.content
                FROM find_evidence fe
                JOIN observation o ON o.id = fe.observation_id
                WHERE fe.find_id = %s
                ORDER BY fe.rank
                """,
                (find_id,),
            )
            return [
                StoredEvidence(observation_id=str(r[0]), similarity=float(r[1]),
                               rank=int(r[2]), content=r[3])
                for r in cur.fetchall()
            ]

    def count_finds(self, business_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM find WHERE business_id = %s",
                        (business_id,))
            return int(cur.fetchone()[0])

    def recent_finds(self, business_id: str, *, limit: int) -> list[FindSummary]:
        """What the Analyst has already proposed, so it does not repeat itself."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, move, status, predicted_daily_cents, created_at
                FROM find
                WHERE business_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (business_id, limit),
            )
            return [
                FindSummary(find_id=str(r[0]), title=r[1], move=r[2], status=str(r[3]),
                            predicted_daily_cents=int(r[4]), created_at=r[5])
                for r in cur.fetchall()
            ]

    def due_finds(self, business_id: str, *, today: date) -> list[DueFind]:
        """The Meter's inbox: acted-on finds whose window has elapsed, unjudged.

        The NOT EXISTS clause is what stops the Meter re-scoring the same find
        every night and inflating the ledger without new work happening.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.title, f.predicted_daily_cents, f.verify_after,
                       f.created_at
                FROM find f
                WHERE f.business_id = %s
                  AND f.verify_after <= %s
                  AND f.status = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM ledger_entry le WHERE le.find_id = f.id
                  )
                ORDER BY f.verify_after, f.created_at
                """,
                (business_id, today, list(JUDGEABLE_STATUSES)),
            )
            return [
                DueFind(find_id=str(r[0]), title=r[1],
                        predicted_daily_cents=int(r[2]), verify_after=r[3],
                        created_at=r[4])
                for r in cur.fetchall()
            ]

    # -- ledger ----------------------------------------------------------
    def insert_ledger_entry(
        self, business_id: str, *, find_id: str, verdict: str,
        predicted_daily_cents: int, actual_daily_cents: int,
        period_start: date, period_end: date, method: str,
        note: str | None = None, run_id: str | None = None,
        measured_at: datetime | None = None,
    ) -> str:
        try:
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ledger_entry (
                            business_id, find_id, run_id, verdict,
                            predicted_daily_cents, actual_daily_cents,
                            measured_at, period_start, period_end, method, note
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()),
                                %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (business_id, find_id, run_id, verdict,
                         predicted_daily_cents, actual_daily_cents, measured_at,
                         period_start, period_end, method, note),
                    )
                    return str(cur.fetchone()[0])
        except psycopg.Error as e:
            # ledger_period_idx makes the ledger append-only per window, so a
            # recorded miss can never be quietly overwritten by a kinder verdict.
            raise RepositoryError(
                f"could not record verdict for find {find_id} "
                f"({period_start}..{period_end}): {e}"
            ) from e

    def ledger_summary(self, business_id: str) -> LedgerSummary:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE verdict = 'verified'),
                    count(*) FILTER (WHERE verdict = 'estimated'),
                    count(*) FILTER (WHERE verdict = 'miss'),
                    coalesce(sum(actual_daily_cents)
                             FILTER (WHERE verdict = 'verified'), 0)
                FROM ledger_entry
                WHERE business_id = %s
                """,
                (business_id,),
            )
            verified, estimated, miss, verified_cents = cur.fetchone()

        return LedgerSummary(
            verified_count=int(verified),
            estimated_count=int(estimated),
            miss_count=int(miss),
            verified_daily_cents=int(verified_cents),
            hit_rate=compute_hit_rate(int(verified), int(miss)),
        )
