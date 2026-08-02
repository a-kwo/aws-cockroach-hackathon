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
    StoredArtifact,
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
                        goal_monthly_cents: int | None = None,
                        latitude: float | None = None,
                        longitude: float | None = None) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO business
                    (name, category, city, goal_monthly_cents, latitude, longitude)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, category, city, goal_monthly_cents, latitude, longitude),
            )
            return str(cur.fetchone()[0])

    def get_business(self, business_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, category, city, region, goal_monthly_cents,
                       goal_note, latitude, longitude
                FROM business WHERE id = %s
                """,
                (business_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ("id", "name", "category", "city", "region",
                "goal_monthly_cents", "goal_note", "latitude", "longitude")
        return {k: (str(v) if k == "id" else v) for k, v in zip(keys, row)}

    # -- owners ----------------------------------------------------------
    def create_account(self, business_id: str | None, *, username: str,
                       password_hash: str) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM owner_account WHERE username = %s", (username,))
            if cur.fetchone():
                # Checked rather than relying on the unique index alone, so the
                # caller gets a message it can show instead of a driver error.
                raise RepositoryError(f"username {username!r} is already taken")
            cur.execute(
                """
                INSERT INTO owner_account (business_id, username, password_hash)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (business_id, username, password_hash),
            )
            return str(cur.fetchone()[0])

    def attach_business(self, account_id: str, *, business_id: str) -> None:
        """Point an account at the business it just created, and its sessions
        with it — the owner is already signed in when the business appears."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE owner_account SET business_id = %s WHERE id = %s",
                (business_id, account_id))
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown account {account_id}")
            cur.execute(
                "UPDATE owner_session SET business_id = %s WHERE account_id = %s",
                (business_id, account_id))

    def account_for_session(self, token_hash: str, *,
                            now: datetime) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_id, business_id FROM owner_session
                WHERE token_hash = %s AND expires_at > %s
                """,
                (token_hash, now),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"account_id": str(row[0]),
                "business_id": str(row[1]) if row[1] else None}

    def find_account(self, username: str) -> dict[str, Any] | None:
        """None rather than raising: the login handler must do the same work
        whether or not the user exists."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, business_id, username, password_hash
                FROM owner_account WHERE username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        # str(None) is "None", which would sail through every truthiness check
        # downstream and land in a URL as a business id.
        return {"id": str(row[0]),
                "business_id": str(row[1]) if row[1] else None,
                "username": row[2], "password_hash": row[3]}

    def create_session(self, token_hash: str, *, business_id: str,
                       account_id: str, expires_at: datetime) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO owner_session
                    (token_hash, business_id, account_id, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (token_hash, business_id, account_id, expires_at),
            )
            cur.execute(
                "UPDATE owner_account SET last_login_at = clock_timestamp() "
                "WHERE id = %s", (account_id,))

    def business_for_session(self, token_hash: str, *,
                             now: datetime) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT business_id FROM owner_session
                WHERE token_hash = %s AND expires_at > %s
                """,
                (token_hash, now),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM owner_session WHERE token_hash = %s", (token_hash,))

    def set_business_status(self, business_id: str, *, status: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE business SET status = %s WHERE id = %s",
                (status, business_id))
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown business {business_id}")

    def active_business_ids(self, *, limit: int = 50) -> list[str]:
        """Tenants the agents work for tonight, oldest first and capped.

        Every one costs a search, embeddings and a Claude call, so the cap is a
        spend control rather than pagination. Oldest first so a burst of signups
        cannot push an established tenant out of tonight's run.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM business
                WHERE status = 'active'
                ORDER BY created_at
                LIMIT %s
                """,
                (limit,),
            )
            return [str(r[0]) for r in cur.fetchall()]

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
                   note: str | None = None, input_tokens: int | None = None,
                   output_tokens: int | None = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_run
                SET status = %s, finished_at = clock_timestamp(), error = %s,
                    note = %s, input_tokens = %s, output_tokens = %s
                WHERE id = %s
                """,
                (status, error, note, input_tokens, output_tokens, run_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown run {run_id}")

    def recent_runs(self, business_id: str, *, limit: int) -> list[RunRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, agent, status, started_at, finished_at, error, note,
                       input_tokens, output_tokens
                FROM agent_run
                WHERE business_id = %s
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (business_id, limit),
            )
            return [
                RunRecord(run_id=str(r[0]), agent=str(r[1]), status=str(r[2]),
                          started_at=r[3], finished_at=r[4], error=r[5], note=r[6],
                          input_tokens=r[7], output_tokens=r[8])
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

    def purge_observations(self, business_id: str, *, source_name: str,
                           older_than: datetime) -> int:
        """Delete this source's observations older than the cutoff.

        Exists because some sources are licensed rather than owned — Yelp
        forbids retaining its content beyond 24 hours. Scoped to one source so
        a licence term can never reach the corpus we do own.

        `find_evidence` cascades on delete, so a find that cited a since-expired
        Yelp review loses that row. That is the correct trade: the alternative
        is retaining content we are not licensed to keep in order to preserve a
        footnote.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM observation
                WHERE business_id = %s AND source_name = %s AND observed_at < %s
                """,
                (business_id, source_name, older_than),
            )
            return cur.rowcount

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
        summary: str | None = None,
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
                            business_id, run_id, emoji, title, summary,
                            rationale, move,
                            predicted_daily_cents, confidence, verify_after,
                            status, created_at, decided_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                coalesce(%s, clock_timestamp()), %s)
                        RETURNING id
                        """,
                        (business_id, run_id, emoji, title, summary,
                         rationale, move,
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
                        decided_at: datetime | None = None,
                        business_id: str | None = None) -> None:
        """Record the owner's decision on a find.

        ``business_id`` scopes browser-originated writes to the configured
        tenant. Agent-internal callers may omit it because they already operate
        on repository objects retrieved for that tenant.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE find
                SET status = %s, decided_at = coalesce(%s, clock_timestamp())
                WHERE id = %s
                  AND (%s::UUID IS NULL OR business_id = %s::UUID)
                  AND status IN ('proposed', 'later')
                """,
                (status, decided_at, find_id, business_id, business_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(
                    f"unknown, already decided, or inaccessible find {find_id}"
                )

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
        """What the Analyst has already proposed, so it does not repeat itself.

        In-play finds sort first regardless of age. Ordering on recency alone
        let twelve unacted-on proposals hide every accepted move, and the
        Analyst re-proposed two of its own verified winners because it could no
        longer see them.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, move, status, predicted_daily_cents, created_at
                FROM find
                WHERE business_id = %s
                ORDER BY (status = ANY(%s)) DESC, created_at DESC
                LIMIT %s
                """,
                (business_id, list(JUDGEABLE_STATUSES), limit),
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

    # -- artifacts -------------------------------------------------------
    def insert_artifact(self, *, find_id: str, kind: str, title: str,
                        preview: str | None = None,
                        s3_bucket: str | None = None,
                        s3_key: str | None = None,
                        run_id: str | None = None) -> str:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO artifact
                        (find_id, run_id, kind, title, s3_bucket, s3_key, preview)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (find_id, run_id, kind, title, s3_bucket, s3_key, preview),
                )
                return str(cur.fetchone()[0])
        except psycopg.Error as e:
            # Almost always the find_id foreign key: an artifact is the
            # deliverable for a specific promise and cannot outlive it.
            raise RepositoryError(f"could not store artifact: {e}") from e

    def get_artifacts(self, find_id: str) -> list[StoredArtifact]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, find_id, kind, title, created_at, preview,
                       s3_bucket, s3_key
                FROM artifact
                WHERE find_id = %s
                ORDER BY created_at DESC
                """,
                (find_id,),
            )
            return [
                StoredArtifact(
                    artifact_id=str(r[0]), find_id=str(r[1]), kind=r[2],
                    title=r[3], created_at=r[4], preview=r[5],
                    s3_bucket=r[6], s3_key=r[7])
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
