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
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import uuid
from typing import Any, Mapping

import psycopg
from psycopg.types.json import Jsonb

from brasstacks.decision_schema import ensure_decision_schema
from brasstacks.connections import (
    CONNECTION_CONNECTED,
    ExternalConnectionRecord,
)
from brasstacks.execution_schema import ensure_execution_schema
from brasstacks.meter import daily_cents_from
from brasstacks.outcome_schema import ensure_outcome_schema
from brasstacks.decisions import (
    DECISION_ACCEPTED,
    DECISION_PASSED,
    DECISION_REOPENED,
    DECISION_SAVED_LATER,
    DECISION_UNDO_PASS,
    RECONSIDER_REASON_CODES,
    REVERSIBLE_REVIEW_TOOLS,
    DecisionEvent,
    ReconsiderResult,
)
from brasstacks.repository import (
    CHAT_ASSISTANT_SOURCE,
    CHAT_OWNER_SOURCE,
    JUDGEABLE_STATUSES,
    OWNER_SEEN_STATUSES,
    WITHHELD_STATUS,
    ChatMessage,
    DecisionTransition,
    DueFind,
    EvidenceRef,
    FindContext,
    FindSummary,
    LedgerSummary,
    OutcomeReport,
    OwnerRule,
    RepositoryError,
    Retrieved,
    RunRecord,
    StoredArtifact,
    StoredEvidence,
    StoredObservation,
    compute_hit_rate,
    content_hash,
)
from brasstacks.task_schema import ensure_task_schema
from brasstacks.tasks import (
    MAKER_AGENT,
    MAKER_DRAFT_TASK,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RETRY,
    TASK_RUNNING,
    TaskEvent,
    TaskRecord,
    ToolExecutionRecord,
    EmailEventRecord,
    maker_resource_key,
    maker_task_idempotency_key,
)


def _vector_literal(embedding: Sequence[float]) -> str:
    """Render an embedding as a CockroachDB VECTOR literal.

    psycopg has no native adapter for the VECTOR type, so it is passed as a
    string and cast in SQL with ``::VECTOR``.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


_TASK_COLUMNS = """
    id, business_id, find_id, requested_by_account_id, agent, task_type,
    status, priority, idempotency_key, resource_key, approval_state,
    decision_cycle, attempt_count, dispatch_count, created_at, updated_at,
    approved_at, started_at, completed_at, cancel_requested_at, cancelled_at,
    superseded_at, next_attempt_at, lease_expires_at, claimed_by, claim_token,
    workflow_execution_arn, output_artifact_id, last_error, input_data, output_data
"""


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return dict(value)


def _task_record(row: Sequence[Any]) -> TaskRecord:
    return TaskRecord(
        task_id=str(row[0]), business_id=str(row[1]),
        find_id=str(row[2]) if row[2] is not None else None,
        requested_by_account_id=str(row[3]) if row[3] is not None else None,
        agent=str(row[4]), task_type=str(row[5]), status=str(row[6]),
        priority=int(row[7]), idempotency_key=str(row[8]),
        resource_key=str(row[9]), approval_state=str(row[10]),
        decision_cycle=int(row[11] or 1),
        attempt_count=int(row[12]), dispatch_count=int(row[13]),
        created_at=row[14], updated_at=row[15], approved_at=row[16],
        started_at=row[17], completed_at=row[18],
        cancel_requested_at=row[19], cancelled_at=row[20],
        superseded_at=row[21], next_attempt_at=row[22],
        lease_expires_at=row[23], claimed_by=row[24],
        claim_token=str(row[25]) if row[25] is not None else None,
        workflow_execution_arn=row[26],
        output_artifact_id=str(row[27]) if row[27] is not None else None,
        last_error=row[28], input_data=_mapping(row[29]),
        output_data=_mapping(row[30]),
    )


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
                       goal_note, latitude, longitude, profile_data,
                       profile_updated_at
                FROM business WHERE id = %s
                """,
                (business_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ("id", "name", "category", "city", "region",
                "goal_monthly_cents", "goal_note", "latitude", "longitude",
                "profile_data", "profile_updated_at")
        return {k: (str(v) if k == "id" else v) for k, v in zip(keys, row)}

    def get_owner_profile(self, account_id: str, *, business_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.business_id, a.username, a.display_name, a.email,
                       b.name, b.category, b.city, b.profile_data,
                       b.profile_updated_at
                FROM owner_account a
                JOIN business b ON b.id = a.business_id
                WHERE a.id = %s AND a.business_id = %s
                """,
                (account_id, business_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "account_id": str(row[0]), "business_id": str(row[1]),
            "username": row[2], "display_name": row[3], "email": row[4],
            "business_name": row[5], "category": row[6], "city": row[7],
            "profile_data": row[8] or {}, "profile_updated_at": row[9],
        }

    def update_account_profile(self, account_id: str, *, display_name: str,
                               email: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owner_account
                SET display_name = %s, email = %s
                WHERE id = %s
                """,
                (display_name, email, account_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown account {account_id}")

    def owner_email_for_business(
        self, business_id: str, *, preferred_account_id: str | None = None
    ) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT email
                FROM owner_account
                WHERE business_id = %s
                  AND email IS NOT NULL
                  AND trim(email) <> ''
                ORDER BY CASE WHEN id = %s THEN 0 ELSE 1 END, created_at
                LIMIT 1
                """,
                (business_id, preferred_account_id),
            )
            row = cur.fetchone()
        return str(row[0]).strip() if row and row[0] else None

    def update_business_profile(
        self, business_id: str, *, name: str, category: str, city: str,
        profile_data: Mapping[str, Any], latitude: float | None = None,
        longitude: float | None = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE business
                SET name = %s,
                    category = %s,
                    city = %s,
                    profile_data = %s,
                    profile_updated_at = clock_timestamp(),
                    latitude = CASE WHEN %s IS NULL THEN latitude ELSE %s END,
                    longitude = CASE WHEN %s IS NULL THEN longitude ELSE %s END
                WHERE id = %s
                """,
                (name, category, city, Jsonb(dict(profile_data)),
                 latitude, latitude, longitude, longitude, business_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"unknown business {business_id}")

    def replace_business_profile_facts(
        self, business_id: str, *, facts: Sequence[str],
        embeddings: Sequence[Sequence[float]], source: str,
    ) -> list[str]:
        if len(facts) != len(embeddings):
            raise RepositoryError("profile facts and embeddings must have the same length")
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM business_fact
                WHERE business_id = %s AND source = %s
                  AND profile_managed = true AND superseded_by IS NULL
                """,
                (business_id, source),
            )
            old_ids = [str(row[0]) for row in cur.fetchall()]
            new_ids: list[str] = []
            for fact, embedding in zip(facts, embeddings):
                cur.execute(
                    """
                    INSERT INTO business_fact
                        (business_id, fact, source, confidence, profile_managed, embedding)
                    VALUES (%s, %s, %s, 1.0, true, %s::VECTOR)
                    RETURNING id
                    """,
                    (business_id, fact, source, _vector_literal(embedding)),
                )
                new_ids.append(str(cur.fetchone()[0]))
            if old_ids and new_ids:
                placeholders = ",".join(["%s"] * len(old_ids))
                cur.execute(
                    f"UPDATE business_fact SET superseded_by = %s WHERE id IN ({placeholders})",
                    (new_ids[0], *old_ids),
                )
        return new_ids

    # -- owners ----------------------------------------------------------
    def create_account(self, business_id: str | None, *, username: str,
                       password_hash: str | None,
                       auth_provider: str = "password",
                       provider_subject: str | None = None,
                       email: str | None = None,
                       display_name: str | None = None) -> str:
        """Create an owner account.

        `password_hash` is None exactly when the account signs in through an
        identity provider — there is no password to hash and none to leak. It
        stays a required argument rather than defaulting to None so a caller
        cannot create a passwordless password-account by forgetting it.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM owner_account WHERE username = %s", (username,))
            if cur.fetchone():
                # Checked rather than relying on the unique index alone, so the
                # caller gets a message it can show instead of a driver error.
                raise RepositoryError(f"username {username!r} is already taken")
            cur.execute(
                """
                INSERT INTO owner_account
                    (business_id, username, password_hash,
                     auth_provider, provider_subject, email, display_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (business_id, username, password_hash, auth_provider,
                 provider_subject, email, display_name),
            )
            return str(cur.fetchone()[0])

    def find_account_by_provider(self, provider: str,
                                 subject: str) -> dict[str, Any] | None:
        """The account for an external identity, keyed on the provider's stable
        subject rather than the email — people change their address."""
        if not subject:
            return None
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, business_id, username, password_hash, is_admin,
                       email, display_name, auth_provider, provider_subject
                FROM owner_account
                WHERE auth_provider = %s AND provider_subject = %s
                """,
                (provider, subject),
            )
            return self._account_row(cur.fetchone())

    def find_account_by_id(self, account_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, business_id, username, password_hash, is_admin,
                       email, display_name, auth_provider, provider_subject
                FROM owner_account WHERE id = %s
                """,
                (account_id,),
            )
            return self._account_row(cur.fetchone())

    @staticmethod
    def _account_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        # str(None) is "None", which would sail through every truthiness check
        # downstream and land in a URL as a business id.
        return {"id": str(row[0]),
                "business_id": str(row[1]) if row[1] else None,
                "username": row[2], "password_hash": row[3],
                "is_admin": bool(row[4]), "email": row[5],
                "display_name": row[6], "auth_provider": row[7],
                "provider_subject": row[8]}

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
                SELECT s.account_id, COALESCE(a.business_id, s.business_id), a.is_admin
                FROM owner_session s
                JOIN owner_account a ON a.id = s.account_id
                WHERE s.token_hash = %s AND s.expires_at > %s
                """,
                (token_hash, now),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"account_id": str(row[0]),
                "business_id": str(row[1]) if row[1] else None,
                # Read from the account, never from the session row: revoking
                # admin must take effect on the next request, not in two weeks
                # when the session happens to expire.
                "is_admin": bool(row[2])}

    def find_account(self, username: str) -> dict[str, Any] | None:
        """None rather than raising: the login handler must do the same work
        whether or not the user exists."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, business_id, username, password_hash, is_admin,
                       email, display_name, auth_provider, provider_subject
                FROM owner_account WHERE username = %s
                """,
                (username,),
            )
            return self._account_row(cur.fetchone())

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
                SELECT COALESCE(a.business_id, s.business_id)
                FROM owner_session s
                JOIN owner_account a ON a.id = s.account_id
                WHERE s.token_hash = %s AND s.expires_at > %s
                """,
                (token_hash, now),
            )
            row = cur.fetchone()
        # `row` is a truthy (None,) when the session exists but has no business
        # — an operator account, or an owner between /register and onboarding.
        # str(None) is "None", which sails through every truthiness check
        # downstream and ends up queried as a business id.
        return str(row[0]) if row and row[0] else None

    def delete_session(self, token_hash: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM owner_session WHERE token_hash = %s", (token_hash,))

    def create_handoff(self, code_hash: str, *, account_id: str,
                       expires_at: datetime) -> None:
        """Park an account against a one-time code for the OAuth redirect.

        This exists so the callback never has to put a session token in a URL.
        Only the code's fingerprint is stored, like a session token.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_handoff (code_hash, account_id, expires_at)
                VALUES (%s, %s, %s)
                """,
                (code_hash, account_id, expires_at),
            )

    def consume_handoff(self, code_hash: str, *,
                        now: datetime) -> str | None:
        """Redeem a one-time code, or None.

        DELETE ... RETURNING, in one statement: a SELECT followed by a DELETE
        would let two requests arriving together both redeem the same code.
        The expiry is checked in Python rather than in the WHERE clause so an
        expired code is still deleted rather than left to linger.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM oauth_handoff WHERE code_hash = %s
                RETURNING account_id, expires_at
                """,
                (code_hash,),
            )
            row = cur.fetchone()
        if row is None or row[1] <= now:
            return None
        return str(row[0])

    def purge_expired_handoffs(self, *, now: datetime) -> int:
        """Housekeeping for codes nobody redeemed. Called on the nightly run —
        the table is tiny, but an unbounded one is a slow leak."""
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM oauth_handoff WHERE expires_at <= %s", (now,))
            return cur.rowcount or 0

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
                           run_id: str | None = None,
                           statement_type: str | None = None) -> str | None:
        """Store an observation. Returns its id, or None if it was a duplicate.

        A duplicate is normal operation — Radar re-reads the same review nightly —
        so the unique index absorbs it via ON CONFLICT rather than raising and
        aborting a run. Returning the id rather than a bool lets a caller wire
        the stored row straight into find_evidence without a second lookup.

        `statement_type` is what the row asserts rather than where it came from.
        The column arrived before this parameter did, so Radar classified every
        row and the write dropped the answer on the floor — the label existed
        nowhere but in a log line. Bootstrap the column here for the same reason
        the ledger does: a deploy that outruns the migration must not cost a
        night's observations.
        """
        ensure_decision_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO observation (
                    business_id, run_id, kind, content, source_name, source_url,
                    subject, rating, observed_at, content_hash, statement_type,
                    embedding
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::VECTOR)
                ON CONFLICT (business_id, content_hash) DO NOTHING
                RETURNING id
                """,
                (business_id, run_id, kind, content, source_name, source_url,
                 subject, rating, observed_at, content_hash(content),
                 statement_type, _vector_literal(embedding)),
            )
            row = cur.fetchone()
            return str(row[0]) if row is not None else None

    def count_observations(self, business_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM observation
                WHERE business_id = %s
                  AND coalesce(source_name, '') NOT IN (%s, %s)
                """,
                (business_id, CHAT_OWNER_SOURCE, CHAT_ASSISTANT_SOURCE),
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
                       1 - (embedding <=> %s::VECTOR) AS similarity,
                       source_url
                FROM observation
                WHERE business_id = %s AND embedding IS NOT NULL
                  AND coalesce(source_name, '') NOT IN (%s, %s)
                ORDER BY embedding <=> %s::VECTOR
                LIMIT %s
                """,
                (literal, business_id, CHAT_OWNER_SOURCE,
                 CHAT_ASSISTANT_SOURCE, literal, limit),
            )
            rows = cur.fetchall()

        return [
            Retrieved(
                observation_id=str(r[0]), content=r[1], kind=str(r[2]),
                source_name=r[3], subject=r[4], observed_at=r[5],
                similarity=float(r[6]), rank=rank, source_url=r[7],
            )
            for rank, r in enumerate(rows)
        ]

    def all_observations(self, business_id: str, *,
                         limit: int) -> list[StoredObservation]:
        """Every stored row for this tenant, in the order they were observed.

        The deliberate opposite of `search_observations`. Retrieval answers
        "what is relevant to tonight's question"; this answers "what does memory
        hold about this business at all", and `business_state` needs the second
        one. The three rows naming Yellow Cow's Dosirak set meal were in the
        corpus on the night the Analyst told the owner to launch a set meal, and
        vector search did not return any of them.

        Affordable because a tenant corpus is tens of rows, not millions — 43 to
        52 on 2026-08-03 — and `limit` is here so that stops being an assumption
        the night depends on. No embedding column is selected: this row goes to
        a text parser, and a 1024-float vector per row is the expensive half of
        the table.
        """
        ensure_decision_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, kind, source_name, source_url, subject,
                       observed_at, statement_type
                FROM observation
                WHERE business_id = %s
                  AND coalesce(source_name, '') NOT IN (%s, %s)
                ORDER BY observed_at, id
                LIMIT %s
                """,
                (business_id, CHAT_OWNER_SOURCE, CHAT_ASSISTANT_SOURCE, limit),
            )
            return [
                StoredObservation(
                    observation_id=str(r[0]), content=r[1], kind=str(r[2]),
                    source_name=r[3], source_url=r[4], subject=r[5],
                    observed_at=r[6], statement_type=r[7],
                )
                for r in cur.fetchall()
            ]

    # -- owner conversation memory --------------------------------------
    def insert_chat_message(
        self, business_id: str, *, role: str, content: str, created_at: datetime,
        embedding: Sequence[float] | None = None, find_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        if role not in {"user", "assistant"}:
            raise RepositoryError("chat role must be 'user' or 'assistant'")
        source_name = CHAT_OWNER_SOURCE if role == "user" else CHAT_ASSISTANT_SOURCE
        # A conversation is chronological. The same sentence repeated later is
        # a second memory, so include a fresh id in the dedup hash.
        import uuid
        marker = str(uuid.uuid4())
        digest = content_hash(f"chat:{marker}:{role}:{find_id or ''}:{content}")
        vector = _vector_literal(embedding) if embedding is not None else None
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO observation (
                    business_id, run_id, kind, content, source_name, subject,
                    observed_at, content_hash, embedding
                )
                VALUES (%s, %s, 'owner_upload', %s, %s, %s, %s, %s, %s::VECTOR)
                RETURNING id
                """,
                (business_id, run_id, content, source_name, find_id, created_at,
                 digest, vector),
            )
            return str(cur.fetchone()[0])

    def recent_chat_messages(
        self, business_id: str, *, limit: int, find_id: str | None = None,
    ) -> list[ChatMessage]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_name, content, observed_at, subject
                FROM observation
                WHERE business_id = %s
                  AND source_name IN (%s, %s)
                  AND (%s::STRING IS NULL OR subject = %s)
                ORDER BY observed_at DESC,
                         CASE WHEN source_name = %s THEN 0 ELSE 1 END
                LIMIT %s
                """,
                (business_id, CHAT_OWNER_SOURCE, CHAT_ASSISTANT_SOURCE,
                 find_id, find_id, CHAT_ASSISTANT_SOURCE, limit),
            )
            rows = list(reversed(cur.fetchall()))
        return [
            ChatMessage(
                message_id=str(row[0]),
                role="user" if row[1] == CHAT_OWNER_SOURCE else "assistant",
                content=row[2], created_at=row[3], find_id=row[4],
            )
            for row in rows
        ]

    def search_chat_messages(
        self, business_id: str, query_embedding: Sequence[float], *, limit: int,
    ) -> list[ChatMessage]:
        literal = _vector_literal(query_embedding)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, observed_at, subject,
                       1 - (embedding <=> %s::VECTOR) AS similarity
                FROM observation
                WHERE business_id = %s
                  AND source_name = %s
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::VECTOR
                LIMIT %s
                """,
                (literal, business_id, CHAT_OWNER_SOURCE, literal, limit),
            )
            rows = cur.fetchall()
        return [
            ChatMessage(
                message_id=str(row[0]), role="user", content=row[1],
                created_at=row[2], find_id=row[3], similarity=float(row[4]),
            )
            for row in rows
        ]

    def count_chat_messages(self, business_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM observation
                WHERE business_id = %s AND source_name IN (%s, %s)
                """,
                (business_id, CHAT_OWNER_SOURCE, CHAT_ASSISTANT_SOURCE),
            )
            return int(cur.fetchone()[0])

    def get_find_context(self, business_id: str, find_id: str) -> FindContext | None:
        ensure_decision_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, summary, rationale, move, status,
                       predicted_daily_cents, confidence, verify_after,
                       decided_at, decision_cycle, reopened_at,
                       reopen_reason_code, reopen_reason_note,
                       alternative_explanation, withheld_reason, claim_type,
                       feed_brief, cost_estimate
                FROM find
                WHERE id = %s AND business_id = %s
                """,
                (find_id, business_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return FindContext(
            find_id=str(row[0]), title=row[1], summary=row[2], rationale=row[3],
            move=row[4] or "", status=str(row[5]),
            predicted_daily_cents=int(row[6]), confidence=float(row[7]),
            verify_after=row[8], decided_at=row[9],
            decision_cycle=int(row[10] or 1), reopened_at=row[11],
            reopen_reason_code=row[12], reopen_reason_note=row[13],
            alternative_explanation=row[14], withheld_reason=row[15],
            claim_type=row[16], feed_brief=_mapping(row[17]) or None,
            cost_estimate=_mapping(row[18]) or None,
        )

    # -- finds -----------------------------------------------------------
    def insert_find_with_evidence(
        self, business_id: str, *, title: str, rationale: str, move: str,
        emoji: str, predicted_daily_cents: int, confidence: float,
        verify_after: date, evidence: Sequence[EvidenceRef],
        summary: str | None = None,
        alternative_explanation: str | None = None,
        feed_brief: Mapping[str, Any] | None = None,
        cost_estimate: Mapping[str, Any] | None = None,
        status: str = "proposed", withheld_reason: str | None = None,
        claim_type: str | None = None,
        run_id: str | None = None,
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

        # The night writes `alternative_explanation` and `withheld_reason`, and
        # a cluster that has not had the migration applied has neither column —
        # nor 'withheld' in the find_status enum: the INSERT would fail and the
        # night would store nothing at all. Every other writer of `find` already
        # self-heals this way.
        ensure_decision_schema(self._conn)

        try:
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    # created_at/decided_at are overridable so seeded history
                    # reads as history. coalesce keeps the live path on now().
                    cur.execute(
                        """
                        INSERT INTO find (
                            business_id, run_id, emoji, title, summary,
                            rationale, move, feed_brief, cost_estimate,
                            alternative_explanation,
                            predicted_daily_cents, confidence, verify_after,
                            status, withheld_reason, claim_type,
                            created_at, decided_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, coalesce(%s, clock_timestamp()), %s)
                        RETURNING id
                        """,
                        (business_id, run_id, emoji, title, summary,
                         rationale, move,
                         Jsonb(dict(feed_brief)) if feed_brief is not None else None,
                         # NULL, not an empty object, when nobody priced it. A
                         # stored `{}` would read back as an estimate that found
                         # no costs, which is the "free move" claim this whole
                         # design refuses to make by accident.
                         Jsonb(dict(cost_estimate)) if cost_estimate is not None else None,
                         alternative_explanation, predicted_daily_cents,
                         confidence, verify_after, status, withheld_reason,
                         claim_type, created_at, decided_at),
                    )
                    find_id = str(cur.fetchone()[0])

                    cur.executemany(
                        """
                        INSERT INTO find_evidence
                            (find_id, observation_id, similarity, rank)
                        VALUES (%s, %s, %s, %s)
                        """,
                        # rank is retrieval position, supplied by the caller.
                        # The citation index is only a fallback for callers with
                        # no retrieval position to give; citation order is not
                        # stored, because nothing reads the column that way.
                        [(find_id, ref.observation_id, ref.similarity,
                          cited if ref.rank is None else int(ref.rank))
                         for cited, ref in enumerate(evidence)],
                    )
        except psycopg.Error as e:
            raise RepositoryError(f"could not store find with evidence: {e}") from e

        return find_id

    def set_find_status(self, find_id: str, *, status: str,
                        decided_at: datetime | None = None,
                        business_id: str | None = None,
                        actor_account_id: str | None = None,
                        source: str = "owner_decision") -> DecisionTransition:
        """Record one immutable owner decision and update the current projection."""
        ensure_decision_schema(self._conn)
        moment = decided_at or datetime.now(timezone.utc)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT business_id, status, decided_at, decision_cycle
                    FROM find
                    WHERE id = %s
                      AND (%s::UUID IS NULL OR business_id = %s::UUID)
                    FOR UPDATE
                    """,
                    (find_id, business_id, business_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise RepositoryError("recommendation is no longer available")

                resolved_business_id = str(row[0])
                previous_status = str(row[1])
                if previous_status == WITHHELD_STATUS:
                    # She was never shown this, so there is no decision of hers
                    # to record. A stale tab or a crafted request must not be
                    # able to put a find the pipeline refused to stand behind
                    # onto the board, where it would queue Maker work and add
                    # its prediction to the forecast. Same message as an unknown
                    # id: a 409, revealing nothing about what exists.
                    raise RepositoryError("recommendation is no longer available")
                previous_decided_at = row[2]
                decision_cycle = int(row[3] or 1)
                if previous_status == status:
                    return DecisionTransition(
                        find_id=find_id,
                        previous_status=previous_status,
                        status=status,
                        decided_at=previous_decided_at or moment,
                        previous_decided_at=previous_decided_at,
                        decision_cycle=decision_cycle,
                        changed=False,
                    )

                normal_decision = previous_status in ("proposed", "later") and status in (
                    "accepted", "rejected", "later"
                )
                undo_pass = previous_status == "rejected" and status == "accepted"
                if not (normal_decision or undo_pass):
                    raise RepositoryError(f"find already decided as {previous_status}")

                cur.execute(
                    """
                    UPDATE find
                    SET status = %s, decided_at = %s
                    WHERE id = %s
                      AND business_id = %s
                      AND status = %s
                    """,
                    (status, moment, find_id, resolved_business_id, previous_status),
                )
                if cur.rowcount != 1:
                    raise RepositoryError(
                        "recommendation changed while the decision was saving"
                    )

                if undo_pass:
                    event_type = DECISION_UNDO_PASS
                elif status == "accepted":
                    event_type = DECISION_ACCEPTED
                elif status == "rejected":
                    event_type = DECISION_PASSED
                else:
                    event_type = DECISION_SAVED_LATER
                cur.execute(
                    """
                    INSERT INTO decision_event (
                        business_id, find_id, decision_cycle, event_type,
                        previous_status, new_status, actor_account_id, source,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (resolved_business_id, find_id, decision_cycle, event_type,
                     previous_status, status, actor_account_id, source, moment),
                )
                event_id = str(cur.fetchone()[0])

        return DecisionTransition(
            find_id=find_id,
            previous_status=previous_status,
            status=status,
            decided_at=moment,
            previous_decided_at=previous_decided_at,
            decision_cycle=decision_cycle,
            event_id=event_id,
            changed=True,
        )

    def reconsider_find(
        self, find_id: str, *, business_id: str,
        actor_account_id: str | None = None,
        reason_code: str | None = None, reason_note: str | None = None,
        source: str = "owner_reconsider",
        reopened_at: datetime | None = None,
    ) -> ReconsiderResult:
        """Open a new decision cycle without rewriting the prior Do it history."""
        ensure_decision_schema(self._conn)
        ensure_task_schema(self._conn)
        if reason_code and reason_code not in RECONSIDER_REASON_CODES:
            raise RepositoryError("choose a valid reconsideration reason")
        moment = reopened_at or datetime.now(timezone.utc)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, decision_cycle, reopened_at, decided_at, created_at
                    FROM find
                    WHERE id = %s AND business_id = %s
                    FOR UPDATE
                    """,
                    (find_id, business_id),
                )
                found = cur.fetchone()
                if found is None:
                    raise RepositoryError("recommendation is no longer available")
                previous_status = str(found[0])
                previous_cycle = int(found[1] or 1)
                prior_reopened_at = found[2]
                previous_decided_at = found[3]
                find_created_at = found[4]
                if previous_decided_at is not None and moment <= previous_decided_at:
                    moment = previous_decided_at + timedelta(microseconds=1)
                if previous_status == "proposed" and prior_reopened_at is not None:
                    return ReconsiderResult(
                        find_id=find_id,
                        previous_status="accepted",
                        status="proposed",
                        previous_cycle=max(1, previous_cycle - 1),
                        decision_cycle=previous_cycle,
                        reopened_at=prior_reopened_at,
                        changed=False,
                    )
                if previous_status != "accepted":
                    raise RepositoryError(
                        "only an approved recommendation that has not changed a customer-facing system can be reconsidered"
                    )

                cur.execute(
                    "SELECT 1 FROM ledger_entry WHERE find_id = %s LIMIT 1",
                    (find_id,),
                )
                if cur.fetchone() is not None:
                    raise RepositoryError(
                        "this move already has a Meter result; create a new recommendation revision instead"
                    )

                # Older imported rows can predate decision_event. Mark the
                # backfill as inferred instead of attributing it to the owner
                # who is performing today's reconsideration.
                cur.execute(
                    """
                    SELECT 1
                    FROM decision_event
                    WHERE find_id = %s
                      AND decision_cycle = %s
                      AND event_type = ANY(%s::STRING[])
                    LIMIT 1
                    """,
                    (find_id, previous_cycle, [DECISION_ACCEPTED, DECISION_UNDO_PASS]),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        """
                        INSERT INTO decision_event (
                            business_id, find_id, decision_cycle, event_type,
                            previous_status, new_status, actor_account_id,
                            source, data, created_at
                        )
                        VALUES (%s, %s, %s, %s, 'proposed', 'accepted', NULL,
                                'legacy_projection_backfill', %s, %s)
                        """,
                        (business_id, find_id, previous_cycle, DECISION_ACCEPTED,
                         Jsonb({"inferred_from_find_projection": True}),
                         previous_decided_at or find_created_at or moment),
                    )

                safe_tools = list(REVERSIBLE_REVIEW_TOOLS)
                cur.execute(
                    """
                    SELECT DISTINCT tx.tool_name
                    FROM tool_execution tx
                    JOIN work_task wt ON wt.id = tx.task_id
                    WHERE wt.business_id = %s
                      AND wt.find_id = %s
                      AND wt.decision_cycle <= %s
                      AND tx.status IN ('running', 'succeeded')
                      AND NOT (tx.tool_name = ANY(%s::STRING[]))
                    ORDER BY tx.tool_name
                    """,
                    (business_id, find_id, previous_cycle, safe_tools),
                )
                irreversible = [str(row[0]) for row in cur.fetchall()]
                if irreversible:
                    raise RepositoryError(
                        "an external action is already running or completed; create a corrective task instead"
                    )

                cur.execute(
                    """
                    SELECT id, status
                    FROM work_task
                    WHERE business_id = %s
                      AND find_id = %s
                      AND decision_cycle <= %s
                      AND superseded_at IS NULL
                    FOR UPDATE
                    """,
                    (business_id, find_id, previous_cycle),
                )
                task_rows = [(str(row[0]), str(row[1])) for row in cur.fetchall()]
                active_statuses = {"queued", "retry", "running", "waiting_user"}
                cancelled_ids = [task_id for task_id, task_status in task_rows
                                 if task_status in active_statuses]
                superseded_ids = [task_id for task_id, _ in task_rows]

                if superseded_ids:
                    cur.execute(
                        """
                        UPDATE work_task
                        SET status = CASE
                              WHEN status IN ('queued','retry','running','waiting_user')
                                THEN 'cancelled'
                              ELSE status
                            END,
                            approval_state = CASE
                              WHEN status IN ('queued','retry','running','waiting_user')
                                THEN 'rejected'
                              ELSE approval_state
                            END,
                            cancel_requested_at = CASE
                              WHEN status IN ('queued','retry','running','waiting_user')
                                THEN %s
                              ELSE cancel_requested_at
                            END,
                            cancelled_at = CASE
                              WHEN status IN ('queued','retry','running','waiting_user')
                                THEN %s
                              ELSE cancelled_at
                            END,
                            superseded_at = %s,
                            claimed_by = NULL,
                            claim_token = NULL,
                            lease_expires_at = NULL,
                            next_attempt_at = NULL,
                            last_error = CASE
                              WHEN status IN ('queued','retry','running','waiting_user')
                                THEN 'superseded when the owner reopened the recommendation'
                              ELSE last_error
                            END,
                            updated_at = %s
                        WHERE id = ANY(%s::UUID[])
                        """,
                        (moment, moment, moment, moment, superseded_ids),
                    )
                    for task_id, task_status in task_rows:
                        event_type = (
                            "task.cancelled_by_reconsider"
                            if task_status in active_statuses
                            else "task.superseded_by_reconsider"
                        )
                        cur.execute(
                            """
                            INSERT INTO task_event
                                (task_id, business_id, event_type, actor_type, actor_id, data)
                            VALUES (%s, %s, %s, 'owner', %s, %s)
                            """,
                            (task_id, business_id, event_type, actor_account_id,
                             Jsonb({"decision_cycle": previous_cycle})),
                        )

                cur.execute(
                    """
                    UPDATE artifact
                    SET superseded_at = %s
                    WHERE find_id = %s
                      AND decision_cycle <= %s
                      AND superseded_at IS NULL
                    RETURNING id
                    """,
                    (moment, find_id, previous_cycle),
                )
                superseded_artifact_ids = tuple(str(row[0]) for row in cur.fetchall())

                next_cycle = previous_cycle + 1
                cur.execute(
                    """
                    UPDATE find
                    SET status = 'proposed',
                        decision_cycle = %s,
                        decided_at = %s,
                        reopened_at = %s,
                        reopen_reason_code = %s,
                        reopen_reason_note = %s
                    WHERE id = %s AND business_id = %s AND status = 'accepted'
                    """,
                    (next_cycle, moment, moment, reason_code, reason_note,
                     find_id, business_id),
                )
                if cur.rowcount != 1:
                    raise RepositoryError(
                        "recommendation changed while it was being reopened"
                    )
                event_data = {
                    "previous_cycle": previous_cycle,
                    "cancelled_task_ids": cancelled_ids,
                    "superseded_task_ids": superseded_ids,
                    "superseded_artifact_ids": list(superseded_artifact_ids),
                }
                cur.execute(
                    """
                    INSERT INTO decision_event (
                        business_id, find_id, decision_cycle, event_type,
                        previous_status, new_status, actor_account_id,
                        reason_code, reason_note, source, data, created_at
                    )
                    VALUES (%s, %s, %s, %s, 'accepted', 'proposed', %s,
                            %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (business_id, find_id, next_cycle, DECISION_REOPENED,
                     actor_account_id, reason_code, reason_note, source,
                     Jsonb(event_data), moment),
                )
                event_id = str(cur.fetchone()[0])

        return ReconsiderResult(
            find_id=find_id,
            previous_status="accepted",
            status="proposed",
            previous_cycle=previous_cycle,
            decision_cycle=next_cycle,
            reopened_at=moment,
            event_id=event_id,
            cancelled_task_ids=tuple(cancelled_ids),
            superseded_task_ids=tuple(superseded_ids),
            superseded_artifact_ids=superseded_artifact_ids,
        )

    def decision_events(
        self, find_id: str, *, business_id: str | None = None,
        limit: int = 100,
    ) -> list[DecisionEvent]:
        ensure_decision_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, business_id, find_id, decision_cycle, event_type,
                       previous_status, new_status, actor_account_id,
                       reason_code, reason_note, source, data, created_at
                FROM decision_event
                WHERE find_id = %s
                  AND (%s::UUID IS NULL OR business_id = %s::UUID)
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (find_id, business_id, business_id, max(0, int(limit))),
            )
            rows = cur.fetchall()
        return [DecisionEvent(
            event_id=str(row[0]), business_id=str(row[1]), find_id=str(row[2]),
            decision_cycle=int(row[3]), event_type=str(row[4]),
            previous_status=str(row[5]) if row[5] is not None else None,
            new_status=str(row[6]),
            actor_account_id=str(row[7]) if row[7] is not None else None,
            reason_code=row[8], reason_note=row[9], source=str(row[10]),
            data=_mapping(row[11]), created_at=row[12],
        ) for row in rows]

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

    def latest_find_created_at(self, business_id: str) -> datetime | None:
        """When this business last got a recommendation.

        Not `recent_finds`: that one sorts in-play moves first so the Analyst
        can see what is already running, which is the wrong order for a
        question about time.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT max(created_at) FROM find WHERE business_id = %s",
                (business_id,))
            return cur.fetchone()[0]

    def recent_finds(self, business_id: str, *, limit: int,
                     include_unseen: bool = False) -> list[FindSummary]:
        """What the owner has already been shown, so the Analyst does not repeat it.

        In-play finds sort first regardless of age. Ordering on recency alone
        let twelve unacted-on proposals hide every accepted move, and the
        Analyst re-proposed two of its own verified winners because it could no
        longer see them.

        Rows in a status the owner never reached are excluded unless asked for.
        This method feeds the "do not repeat any of these" list, and a find that
        a quality gate withheld was never proposed to anyone — forbidding it
        would mean the corrected version can never be raised either. Callers
        that want the fuller picture pass ``include_unseen`` and split on
        ``FindSummary.seen_by_owner``; the two groups need different sentences.
        """
        seen = list(OWNER_SEEN_STATUSES)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, move, status, predicted_daily_cents, created_at
                FROM find
                WHERE business_id = %s
                  AND (%s::BOOL OR status = ANY(%s))
                ORDER BY (status = ANY(%s)) DESC,
                         (status = ANY(%s)) DESC,
                         created_at DESC
                LIMIT %s
                """,
                (business_id, include_unseen, seen,
                 list(JUDGEABLE_STATUSES), seen, limit),
            )
            return [
                FindSummary(find_id=str(r[0]), title=r[1], move=r[2], status=str(r[3]),
                            predicted_daily_cents=int(r[4]), created_at=r[5])
                for r in cur.fetchall()
            ]

    def due_finds(self, business_id: str, *, today: date) -> list[DueFind]:
        """The Meter's inbox, measured from execution when a receipt exists."""
        ensure_execution_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.title, f.predicted_daily_cents,
                       CASE
                         WHEN f.executed_at IS NULL THEN f.verify_after
                         ELSE f.executed_at::DATE + (f.verify_after - f.created_at::DATE)
                       END AS effective_verify_after,
                       f.created_at, f.executed_at::DATE
                FROM find f
                WHERE f.business_id = %s
                  AND CASE
                        WHEN f.executed_at IS NULL THEN f.verify_after
                        ELSE f.executed_at::DATE + (f.verify_after - f.created_at::DATE)
                      END <= %s
                  AND f.status = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM ledger_entry le WHERE le.find_id = f.id
                  )
                ORDER BY effective_verify_after, f.created_at
                """,
                (business_id, today, list(JUDGEABLE_STATUSES)),
            )
            return [
                DueFind(find_id=str(r[0]), title=r[1],
                        predicted_daily_cents=int(r[2]), verify_after=r[3],
                        created_at=r[4], measurement_start=r[5])
                for r in cur.fetchall()
            ]

    # -- artifacts -------------------------------------------------------
    def insert_artifact(self, *, find_id: str, kind: str, title: str,
                        preview: str | None = None,
                        s3_bucket: str | None = None,
                        s3_key: str | None = None,
                        run_id: str | None = None,
                        task_id: str | None = None,
                        idempotency_key: str | None = None,
                        body: str | None = None,
                        summary: str | None = None,
                        owner_action: str | None = None,
                        review_state: str = "ready_for_review",
                        metadata: Mapping[str, Any] | None = None,
                        revision: int = 1,
                        parent_artifact_id: str | None = None,
                        decision_cycle: int = 1) -> str:
        ensure_task_schema(self._conn)
        ensure_decision_schema(self._conn)
        try:
            with self._conn.transaction():
                with self._conn.cursor() as cur:
                    artifact_cycle = max(1, int(decision_cycle))
                    if task_id is not None:
                        cur.execute(
                            """
                            SELECT wt.status, wt.decision_cycle, wt.superseded_at,
                                   f.status, f.decision_cycle
                            FROM work_task wt
                            JOIN find f ON f.id = wt.find_id
                            WHERE wt.id = %s AND f.id = %s
                            FOR UPDATE
                            """,
                            (task_id, find_id),
                        )
                        guard = cur.fetchone()
                        if guard is None:
                            raise RepositoryError("Maker task is no longer available")
                        artifact_cycle = int(guard[1] or 1)
                        if (
                            str(guard[0]) != TASK_RUNNING
                            or guard[2] is not None
                            or str(guard[3]) != "accepted"
                            or int(guard[4] or 1) != artifact_cycle
                        ):
                            raise RepositoryError("Maker task was cancelled or superseded")

                    revision_number = max(1, int(revision))
                    if parent_artifact_id is not None:
                        cur.execute(
                            """
                            SELECT revision
                            FROM artifact
                            WHERE id = %s AND find_id = %s
                            FOR UPDATE
                            """,
                            (parent_artifact_id, find_id),
                        )
                        parent = cur.fetchone()
                        if parent is None:
                            raise RepositoryError("base Maker draft is no longer available")
                        revision_number = max(revision_number, int(parent[0] or 1) + 1)

                    columns = """
                        find_id, run_id, kind, title, s3_bucket, s3_key, preview,
                        task_id, idempotency_key, body, summary, owner_action,
                        review_state, metadata, revision, parent_artifact_id,
                        decision_cycle
                    """
                    values = (
                        find_id, run_id, kind, title, s3_bucket, s3_key, preview,
                        task_id, idempotency_key, body, summary, owner_action,
                        review_state, Jsonb(dict(metadata or {})), revision_number,
                        parent_artifact_id, artifact_cycle,
                    )
                    if idempotency_key:
                        cur.execute(
                            f"""
                            INSERT INTO artifact ({columns})
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (idempotency_key) DO UPDATE
                            SET idempotency_key = excluded.idempotency_key
                            RETURNING id
                            """,
                            values,
                        )
                    else:
                        cur.execute(
                            f"""
                            INSERT INTO artifact ({columns})
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            values,
                        )
                    artifact_id = str(cur.fetchone()[0])
                    if parent_artifact_id is not None:
                        cur.execute(
                            """
                            UPDATE artifact
                            SET superseded_at = coalesce(superseded_at, clock_timestamp())
                            WHERE id = %s AND id <> %s
                            """,
                            (parent_artifact_id, artifact_id),
                        )
                    return artifact_id
        except RepositoryError:
            raise
        except psycopg.Error as e:
            raise RepositoryError(f"could not store artifact: {e}") from e

    @staticmethod
    def _stored_artifact(row: Sequence[Any]) -> StoredArtifact:
        return StoredArtifact(
            artifact_id=str(row[0]), find_id=str(row[1]), kind=row[2],
            title=row[3], created_at=row[4], preview=row[5],
            s3_bucket=row[6], s3_key=row[7],
            task_id=str(row[8]) if row[8] is not None else None,
            idempotency_key=row[9], body=row[10], summary=row[11],
            owner_action=row[12], review_state=row[13] or "ready_for_review",
            metadata=_mapping(row[14]), revision=int(row[15] or 1),
            parent_artifact_id=str(row[16]) if row[16] is not None else None,
            decision_cycle=int(row[17] or 1), superseded_at=row[18],
        )

    def get_artifacts(self, find_id: str) -> list[StoredArtifact]:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, find_id, kind, title, created_at, preview,
                       s3_bucket, s3_key, task_id, idempotency_key, body,
                       summary, owner_action, review_state, metadata, revision,
                       parent_artifact_id, decision_cycle, superseded_at
                FROM artifact
                WHERE find_id = %s
                ORDER BY revision DESC, created_at DESC
                """,
                (find_id,),
            )
            return [self._stored_artifact(row) for row in cur.fetchall()]

    def get_artifact_by_idempotency_key(
        self, idempotency_key: str,
    ) -> StoredArtifact | None:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, find_id, kind, title, created_at, preview,
                       s3_bucket, s3_key, task_id, idempotency_key, body,
                       summary, owner_action, review_state, metadata, revision,
                       parent_artifact_id, decision_cycle, superseded_at
                FROM artifact
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            )
            row = cur.fetchone()
        return self._stored_artifact(row) if row is not None else None

    # -- durable tasks ---------------------------------------------------
    def create_or_get_maker_task(
        self, business_id: str, *, find_id: str,
        requested_by_account_id: str | None = None,
        approved_at: datetime | None = None, priority: int = 100,
        input_data: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        ensure_task_schema(self._conn)
        ensure_decision_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, move, predicted_daily_cents, status, decided_at,
                           decision_cycle
                    FROM find
                    WHERE id = %s AND business_id = %s
                    FOR UPDATE
                    """,
                    (find_id, business_id),
                )
                found = cur.fetchone()
                if found is None:
                    raise RepositoryError("recommendation is no longer available")
                if str(found[3]) != "accepted":
                    raise RepositoryError("Maker work requires an approved recommendation")
                decision_cycle = int(found[5] or 1)
                key = maker_task_idempotency_key(
                    find_id, decision_cycle=decision_cycle
                )
                payload = dict(input_data or {
                    "title": found[0],
                    "move": found[1],
                    "predicted_daily_cents": int(found[2]),
                    "decision_cycle": decision_cycle,
                })
                cur.execute(
                    f"""
                    INSERT INTO work_task (
                        business_id, find_id, requested_by_account_id, agent,
                        task_type, status, priority, idempotency_key, resource_key,
                        approval_state, approved_at, decision_cycle, input_data
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, 'queued', %s, %s, %s,
                        'approved', coalesce(%s, %s, clock_timestamp()), %s, %s
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING {_TASK_COLUMNS}
                    """,
                    (business_id, find_id, requested_by_account_id,
                     MAKER_AGENT, MAKER_DRAFT_TASK, int(priority), key,
                     maker_resource_key(
                         business_id, find_id, decision_cycle=decision_cycle
                     ), approved_at, found[4], decision_cycle,
                     Jsonb(payload)),
                )
                row = cur.fetchone()
                created = row is not None
                if row is None:
                    cur.execute(
                        f"SELECT {_TASK_COLUMNS} FROM work_task WHERE idempotency_key = %s",
                        (key,),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise RepositoryError("could not create Maker task")
                task = _task_record(row)
                if created:
                    cur.execute(
                        """
                        INSERT INTO task_event
                            (task_id, business_id, event_type, actor_type, actor_id, data)
                        VALUES (%s, %s, 'task.created', 'owner', %s, %s)
                        """,
                        (task.task_id, business_id, requested_by_account_id,
                         Jsonb({
                             "find_id": find_id,
                             "task_type": MAKER_DRAFT_TASK,
                             "decision_cycle": decision_cycle,
                         })),
                    )
        return replace(task, created=created)

    def get_task(
        self, task_id: str, *, business_id: str | None = None,
    ) -> TaskRecord | None:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_COLUMNS}
                FROM work_task
                WHERE id = %s
                  AND (%s::UUID IS NULL OR business_id = %s::UUID)
                """,
                (task_id, business_id, business_id),
            )
            row = cur.fetchone()
        return _task_record(row) if row is not None else None

    @staticmethod
    def _external_connection(row: Sequence[Any]) -> ExternalConnectionRecord:
        scopes_value = row[6]
        if isinstance(scopes_value, str):
            try:
                scopes_value = json.loads(scopes_value)
            except json.JSONDecodeError:
                scopes_value = []
        scopes = tuple(str(value) for value in (scopes_value or []) if str(value).strip())
        return ExternalConnectionRecord(
            connection_id=str(row[0]), business_id=str(row[1]), provider=str(row[2]),
            account_id=str(row[3]), status=str(row[4]), token_ciphertext=str(row[5]),
            scopes=scopes, external_account_name=row[7],
            external_location_name=row[8], display_name=row[9],
            metadata=_mapping(row[10]), last_error=row[11], connected_at=row[12],
            created_at=row[13], updated_at=row[14],
        )

    def upsert_external_connection(
        self, *, business_id: str, provider: str, account_id: str,
        token_ciphertext: str, scopes: Sequence[str], status: str,
        external_account_name: str | None = None,
        external_location_name: str | None = None,
        display_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        last_error: str | None = None,
    ) -> ExternalConnectionRecord:
        ensure_task_schema(self._conn)
        ensure_execution_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO external_connection
                    (business_id, provider, account_id, status, token_ciphertext,
                     scopes, external_account_name, external_location_name,
                     display_name, metadata, last_error, connected_at)
                SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s = 'connected' THEN clock_timestamp() ELSE NULL END
                FROM owner_account a
                WHERE a.id = %s AND a.business_id = %s
                ON CONFLICT (business_id, provider) DO UPDATE SET
                    account_id = excluded.account_id,
                    status = excluded.status,
                    token_ciphertext = excluded.token_ciphertext,
                    scopes = excluded.scopes,
                    external_account_name = excluded.external_account_name,
                    external_location_name = excluded.external_location_name,
                    display_name = excluded.display_name,
                    metadata = excluded.metadata,
                    last_error = excluded.last_error,
                    connected_at = CASE
                        WHEN excluded.status = 'connected'
                        THEN COALESCE(external_connection.connected_at, clock_timestamp())
                        ELSE external_connection.connected_at
                    END,
                    updated_at = clock_timestamp()
                RETURNING id, business_id, provider, account_id, status,
                          token_ciphertext, scopes, external_account_name,
                          external_location_name, display_name, metadata, last_error,
                          connected_at, created_at, updated_at
                """,
                (business_id, provider, account_id, status, token_ciphertext,
                 Jsonb(list(scopes)), external_account_name, external_location_name,
                 display_name, Jsonb(dict(metadata or {})), last_error, status,
                 account_id, business_id),
            )
            row = cur.fetchone()
        if row is None:
            raise RepositoryError("account does not belong to this business")
        return self._external_connection(row)

    def get_external_connection(
        self, business_id: str, *, provider: str,
    ) -> ExternalConnectionRecord | None:
        ensure_execution_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, business_id, provider, account_id, status,
                       token_ciphertext, scopes, external_account_name,
                       external_location_name, display_name, metadata, last_error,
                       connected_at, created_at, updated_at
                FROM external_connection
                WHERE business_id = %s AND provider = %s
                """,
                (business_id, provider),
            )
            row = cur.fetchone()
        return self._external_connection(row) if row is not None else None

    def select_external_connection(
        self, business_id: str, *, provider: str, account_name: str,
        location_name: str, display_name: str,
    ) -> ExternalConnectionRecord:
        ensure_execution_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, business_id, provider, account_id, status,
                           token_ciphertext, scopes, external_account_name,
                           external_location_name, display_name, metadata, last_error,
                           connected_at, created_at, updated_at
                    FROM external_connection
                    WHERE business_id = %s AND provider = %s
                    FOR UPDATE
                    """,
                    (business_id, provider),
                )
                row = cur.fetchone()
                if row is None:
                    raise RepositoryError("connect Google Business before choosing a location")
                current = self._external_connection(row)
                available = current.metadata.get("locations")
                match = next((item for item in (available or [])
                              if isinstance(item, Mapping)
                              and item.get("accountName") == account_name
                              and item.get("locationName") == location_name), None)
                if match is None:
                    raise RepositoryError("choose a location from the connected Google account")
                cur.execute(
                    """
                    UPDATE external_connection
                    SET status = 'connected', external_account_name = %s,
                        external_location_name = %s, display_name = %s,
                        connected_at = COALESCE(connected_at, clock_timestamp()),
                        last_error = NULL, updated_at = clock_timestamp()
                    WHERE id = %s
                    RETURNING id, business_id, provider, account_id, status,
                              token_ciphertext, scopes, external_account_name,
                              external_location_name, display_name, metadata, last_error,
                              connected_at, created_at, updated_at
                    """,
                    (account_name, location_name,
                     str(display_name or match.get("title") or "Google Business Profile")[:160],
                     current.connection_id),
                )
                return self._external_connection(cur.fetchone())

    def mark_find_executed(
        self, find_id: str, *, business_id: str, tool_execution_id: str,
        executed_at: datetime | None = None,
    ) -> None:
        ensure_task_schema(self._conn)
        ensure_execution_schema(self._conn)
        moment = executed_at or datetime.now(timezone.utc)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f.execution_tool_id, t.business_id
                    FROM find f
                    JOIN tool_execution t ON t.id = %s
                    WHERE f.id = %s AND f.business_id = %s
                    FOR UPDATE
                    """,
                    (tool_execution_id, find_id, business_id),
                )
                row = cur.fetchone()
                if row is None or str(row[1]) != business_id:
                    raise RepositoryError("publication receipt is no longer available")
                if row[0] is not None and str(row[0]) != tool_execution_id:
                    raise RepositoryError("this recommendation already has an execution receipt")
                cur.execute(
                    """
                    UPDATE find
                    SET status = 'live',
                        executed_at = COALESCE(executed_at, %s),
                        execution_tool_id = COALESCE(execution_tool_id, %s)
                    WHERE id = %s AND business_id = %s
                    """,
                    (moment, tool_execution_id, find_id, business_id),
                )

    def prepare_task_dispatch(self, task_id: str) -> TaskRecord | None:
        ensure_task_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE work_task
                    SET dispatch_count = dispatch_count + 1,
                        updated_at = clock_timestamp()
                    WHERE id = %s
                      AND status IN ('queued', 'retry')
                      AND superseded_at IS NULL
                      AND cancel_requested_at IS NULL
                      AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())
                    RETURNING {_TASK_COLUMNS}
                    """,
                    (task_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                task = _task_record(row)
                cur.execute(
                    """
                    INSERT INTO task_event
                        (task_id, business_id, event_type, actor_type, data)
                    VALUES (%s, %s, 'task.dispatched', 'system', %s)
                    """,
                    (task.task_id, task.business_id,
                     Jsonb({"dispatch_count": task.dispatch_count})),
                )
        return task

    def record_task_workflow(
        self, task_id: str, *, execution_arn: str,
    ) -> TaskRecord | None:
        ensure_task_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE work_task
                    SET workflow_execution_arn = %s,
                        updated_at = clock_timestamp()
                    WHERE id = %s
                    RETURNING {_TASK_COLUMNS}
                    """,
                    (execution_arn, task_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                task = _task_record(row)
                cur.execute(
                    """
                    INSERT INTO task_event
                        (task_id, business_id, event_type, actor_type, data)
                    VALUES (%s, %s, 'workflow.started', 'orchestrator', %s)
                    """,
                    (task.task_id, task.business_id,
                     Jsonb({"execution_arn": execution_arn})),
                )
        return task

    def claim_task(
        self, task_id: str, *, worker_id: str, lease_seconds: int = 900,
    ) -> TaskRecord | None:
        ensure_task_schema(self._conn)
        claim_token = str(uuid.uuid4())
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE work_task
                    SET status = 'running',
                        claimed_by = %s,
                        claim_token = %s,
                        lease_expires_at = clock_timestamp() + (%s * INTERVAL '1 second'),
                        started_at = coalesce(started_at, clock_timestamp()),
                        attempt_count = attempt_count + 1,
                        next_attempt_at = NULL,
                        last_error = NULL,
                        updated_at = clock_timestamp()
                    WHERE id = %s
                      AND superseded_at IS NULL
                      AND cancel_requested_at IS NULL
                      AND (
                        (status IN ('queued', 'retry')
                         AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp()))
                        OR (status = 'running' AND lease_expires_at <= clock_timestamp())
                      )
                    RETURNING {_TASK_COLUMNS}
                    """,
                    (worker_id, claim_token, max(30, int(lease_seconds)), task_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                task = _task_record(row)
                cur.execute(
                    """
                    INSERT INTO task_event
                        (task_id, business_id, event_type, actor_type, actor_id, data)
                    VALUES (%s, %s, 'task.claimed', 'worker', %s, %s)
                    """,
                    (task.task_id, task.business_id, worker_id,
                     Jsonb({"attempt_count": task.attempt_count,
                            "claim_token": task.claim_token})),
                )
        return task

    def task_can_continue(
        self, task_id: str, *, claim_token: str,
    ) -> bool:
        ensure_task_schema(self._conn)
        ensure_decision_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM work_task wt
                JOIN find f ON f.id = wt.find_id
                WHERE wt.id = %s
                  AND wt.status = 'running'
                  AND wt.claim_token = %s
                  AND wt.superseded_at IS NULL
                  AND wt.cancel_requested_at IS NULL
                  AND f.status = 'accepted'
                  AND f.decision_cycle = wt.decision_cycle
                """,
                (task_id, claim_token),
            )
            return cur.fetchone() is not None

    def complete_task(
        self, task_id: str, *, claim_token: str,
        output_artifact_id: str | None = None,
        output_data: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        ensure_task_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE work_task
                    SET status = 'completed',
                        output_artifact_id = coalesce(%s, output_artifact_id),
                        output_data = %s,
                        completed_at = coalesce(completed_at, clock_timestamp()),
                        updated_at = clock_timestamp(),
                        claimed_by = NULL,
                        claim_token = NULL,
                        lease_expires_at = NULL,
                        next_attempt_at = NULL,
                        last_error = NULL
                    WHERE id = %s
                      AND (
                        (status = 'running' AND claim_token = %s)
                        OR status = 'completed'
                      )
                    RETURNING {_TASK_COLUMNS}
                    """,
                    (output_artifact_id, Jsonb(dict(output_data or {})),
                     task_id, claim_token),
                )
                row = cur.fetchone()
                if row is None:
                    raise RepositoryError("task claim is no longer valid")
                task = _task_record(row)
                cur.execute(
                    """
                    INSERT INTO task_event
                        (task_id, business_id, event_type, actor_type, data)
                    VALUES (%s, %s, 'task.completed', 'worker', %s)
                    """,
                    (task.task_id, task.business_id,
                     Jsonb({"output_artifact_id": task.output_artifact_id})),
                )
        return task

    def fail_task(
        self, task_id: str, *, claim_token: str, error: str,
        retryable: bool = True, retry_after_seconds: int = 60,
    ) -> TaskRecord:
        ensure_task_schema(self._conn)
        status = TASK_RETRY if retryable else TASK_FAILED
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE work_task
                    SET status = %s,
                        last_error = %s,
                        next_attempt_at = CASE WHEN %s
                          THEN clock_timestamp() + (%s * INTERVAL '1 second')
                          ELSE NULL END,
                        updated_at = clock_timestamp(),
                        claimed_by = NULL,
                        claim_token = NULL,
                        lease_expires_at = NULL
                    WHERE id = %s AND status = 'running' AND claim_token = %s
                    RETURNING {_TASK_COLUMNS}
                    """,
                    (status, str(error), bool(retryable),
                     max(1, int(retry_after_seconds)), task_id, claim_token),
                )
                row = cur.fetchone()
                if row is None:
                    raise RepositoryError("task claim is no longer valid")
                task = _task_record(row)
                cur.execute(
                    """
                    INSERT INTO task_event
                        (task_id, business_id, event_type, actor_type, data)
                    VALUES (%s, %s, %s, 'worker', %s)
                    """,
                    (task.task_id, task.business_id,
                     "task.retry_scheduled" if retryable else "task.failed",
                     Jsonb({"error": str(error), "retryable": retryable})),
                )
        return task

    def list_tasks(
        self, business_id: str, *, statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_COLUMNS}
                FROM work_task
                WHERE business_id = %s
                  AND (%s::STRING[] IS NULL OR status = ANY(%s::STRING[]))
                ORDER BY priority ASC, created_at DESC
                LIMIT %s
                """,
                (business_id, list(statuses) if statuses else None,
                 list(statuses) if statuses else None, max(0, int(limit))),
            )
            return [_task_record(row) for row in cur.fetchall()]

    def list_dispatchable_tasks(
        self, *, limit: int = 100, per_business_limit: int = 3,
    ) -> list[TaskRecord]:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_COLUMNS}
                FROM (
                    SELECT {_TASK_COLUMNS},
                           row_number() OVER (
                               PARTITION BY business_id
                               ORDER BY priority ASC, created_at ASC
                           ) AS tenant_rank
                    FROM work_task
                    WHERE status IN ('queued', 'retry')
                      AND superseded_at IS NULL
                      AND cancel_requested_at IS NULL
                      AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())
                ) ranked
                WHERE tenant_rank <= %s
                ORDER BY priority ASC, created_at ASC
                LIMIT %s
                """,
                (max(1, int(per_business_limit)), max(0, int(limit))),
            )
            return [_task_record(row) for row in cur.fetchall()]

    def recover_stale_tasks(
        self, *, now: datetime, max_attempts: int = 3,
    ) -> int:
        ensure_task_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE work_task
                    SET status = CASE WHEN attempt_count < %s THEN 'retry' ELSE 'failed' END,
                        last_error = 'worker lease expired',
                        next_attempt_at = CASE WHEN attempt_count < %s THEN %s ELSE NULL END,
                        claimed_by = NULL,
                        claim_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = %s
                    WHERE status = 'running'
                      AND superseded_at IS NULL
                      AND cancel_requested_at IS NULL
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= %s
                    RETURNING id, business_id, status, attempt_count
                    """,
                    (int(max_attempts), int(max_attempts), now, now, now),
                )
                rows = cur.fetchall()
                for row in rows:
                    cur.execute(
                        """
                        INSERT INTO task_event
                            (task_id, business_id, event_type, actor_type, data)
                        VALUES (%s, %s, 'task.lease_expired', 'reconciler', %s)
                        """,
                        (row[0], row[1],
                         Jsonb({"status": str(row[2]),
                                "attempt_count": int(row[3])})),
                    )
        return len(rows)

    def record_task_event(
        self, task_id: str, *, event_type: str, actor_type: str = "system",
        actor_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_event
                    (task_id, business_id, event_type, actor_type, actor_id, data)
                SELECT id, business_id, %s, %s, %s, %s
                FROM work_task WHERE id = %s
                RETURNING id
                """,
                (event_type, actor_type, actor_id, Jsonb(dict(data or {})), task_id),
            )
            row = cur.fetchone()
        if row is None:
            raise RepositoryError(f"unknown task {task_id}")
        return str(row[0])

    def task_events(self, task_id: str, *, limit: int = 100) -> list[TaskEvent]:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, task_id, business_id, event_type, created_at,
                       actor_type, actor_id, data
                FROM task_event
                WHERE task_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (task_id, max(0, int(limit))),
            )
            return [
                TaskEvent(
                    event_id=str(row[0]), task_id=str(row[1]),
                    business_id=str(row[2]), event_type=row[3],
                    created_at=row[4], actor_type=row[5], actor_id=row[6],
                    data=_mapping(row[7]),
                )
                for row in cur.fetchall()
            ]

    def start_tool_execution(
        self, *, task_id: str, business_id: str, tool_name: str,
        idempotency_key: str, input_data: Mapping[str, Any] | None = None,
    ) -> ToolExecutionRecord:
        ensure_task_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM work_task
                    WHERE id = %s AND business_id = %s
                      AND superseded_at IS NULL
                      AND cancel_requested_at IS NULL
                      AND status <> 'cancelled'
                    """,
                    (task_id, business_id),
                )
                if cur.fetchone() is None:
                    raise RepositoryError(
                        "task was superseded by a newer owner decision"
                    )
                cur.execute(
                    """
                    INSERT INTO tool_execution
                        (task_id, business_id, tool_name, status,
                         idempotency_key, input_data)
                    VALUES (%s, %s, %s, 'running', %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id, task_id, business_id, tool_name, status,
                              idempotency_key, started_at, finished_at,
                              external_reference, error, input_data, output_data
                    """,
                    (task_id, business_id, tool_name, idempotency_key,
                     Jsonb(dict(input_data or {}))),
                )
                row = cur.fetchone()
                created = row is not None
                if row is None:
                    cur.execute(
                        """
                        SELECT id, task_id, business_id, tool_name, status,
                               idempotency_key, started_at, finished_at,
                               external_reference, error, input_data, output_data
                        FROM tool_execution WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise RepositoryError("could not start tool execution")
                record = ToolExecutionRecord(
                    execution_id=str(row[0]), task_id=str(row[1]),
                    business_id=str(row[2]), tool_name=row[3], status=row[4],
                    idempotency_key=row[5], started_at=row[6], finished_at=row[7],
                    external_reference=row[8], error=row[9],
                    input_data=_mapping(row[10]), output_data=_mapping(row[11]),
                    created=created,
                )
                if record.status == "running" and created:
                    cur.execute(
                        """
                        INSERT INTO task_event
                            (task_id, business_id, event_type, actor_type, actor_id, data)
                        VALUES (%s, %s, 'tool.started', 'tool', %s, %s)
                        """,
                        (record.task_id, record.business_id, tool_name,
                         Jsonb({"tool_execution_id": record.execution_id})),
                    )
        return record

    def finish_tool_execution(
        self, execution_id: str, *, status: str,
        output_data: Mapping[str, Any] | None = None,
        external_reference: str | None = None, error: str | None = None,
    ) -> ToolExecutionRecord:
        ensure_task_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tool_execution
                    SET status = %s, output_data = %s,
                        external_reference = %s, error = %s,
                        finished_at = clock_timestamp()
                    WHERE id = %s
                    RETURNING id, task_id, business_id, tool_name, status,
                              idempotency_key, started_at, finished_at,
                              external_reference, error, input_data, output_data
                    """,
                    (status, Jsonb(dict(output_data or {})), external_reference,
                     error, execution_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise RepositoryError(f"unknown tool execution {execution_id}")
                record = ToolExecutionRecord(
                    execution_id=str(row[0]), task_id=str(row[1]),
                    business_id=str(row[2]), tool_name=row[3], status=row[4],
                    idempotency_key=row[5], started_at=row[6], finished_at=row[7],
                    external_reference=row[8], error=row[9],
                    input_data=_mapping(row[10]), output_data=_mapping(row[11]),
                )
                cur.execute(
                    """
                    INSERT INTO task_event
                        (task_id, business_id, event_type, actor_type, actor_id, data)
                    VALUES (%s, %s, %s, 'tool', %s, %s)
                    """,
                    (record.task_id, record.business_id, f"tool.{status}",
                     record.tool_name,
                     Jsonb({"tool_execution_id": record.execution_id,
                            "external_reference": external_reference,
                            "error": error})),
                )
        return record

    def tool_executions(
        self, task_id: str, *, limit: int = 100,
    ) -> list[ToolExecutionRecord]:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, task_id, business_id, tool_name, status,
                       idempotency_key, started_at, finished_at,
                       external_reference, error, input_data, output_data
                FROM tool_execution
                WHERE task_id = %s
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (task_id, max(0, int(limit))),
            )
            return [
                ToolExecutionRecord(
                    execution_id=str(row[0]), task_id=str(row[1]),
                    business_id=str(row[2]), tool_name=row[3], status=row[4],
                    idempotency_key=row[5], started_at=row[6], finished_at=row[7],
                    external_reference=row[8], error=row[9],
                    input_data=_mapping(row[10]), output_data=_mapping(row[11]),
                )
                for row in cur.fetchall()
            ]

    def request_maker_revision(
        self, *, business_id: str, find_id: str, account_id: str | None,
        instruction: str, requested_at: datetime | None = None,
    ) -> TaskRecord:
        ensure_task_schema(self._conn)
        ensure_decision_schema(self._conn)
        text = " ".join(str(instruction or "").split())
        if not text:
            raise RepositoryError("tell Maker what you want changed")
        requested_at = requested_at or datetime.now(timezone.utc)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT wt.id
                    FROM work_task wt
                    JOIN find f ON f.id = wt.find_id
                    WHERE wt.business_id = %s
                      AND wt.find_id = %s
                      AND f.business_id = %s
                      AND f.status = 'accepted'
                      AND wt.decision_cycle = f.decision_cycle
                      AND wt.status = 'completed'
                      AND wt.superseded_at IS NULL
                      AND wt.output_artifact_id IS NOT NULL
                    ORDER BY wt.created_at DESC
                    LIMIT 1
                    FOR UPDATE OF wt
                    """,
                    (business_id, find_id, business_id),
                )
                task_row = cur.fetchone()
                if task_row is None:
                    raise RepositoryError(
                        "Maker must finish the current draft before revising it"
                    )
                cur.execute(
                    f"SELECT {_TASK_COLUMNS} FROM work_task WHERE id = %s",
                    (task_row[0],),
                )
                row = cur.fetchone()
                if row is None:
                    raise RepositoryError(
                        "Maker task disappeared while revision was requested"
                    )
                current = _task_record(row)
                revision = max(1, int(current.input_data.get("revision") or 1)) + 1
                patch = {
                    "revision": revision,
                    "revision_instruction": text,
                    "base_artifact_id": current.output_artifact_id,
                    "revision_requested_at": requested_at.isoformat(),
                }
                cur.execute(
                    f"""
                    UPDATE work_task
                    SET status = 'queued',
                        attempt_count = 0,
                        input_data = input_data || %s,
                        updated_at = %s,
                        started_at = NULL,
                        completed_at = NULL,
                        next_attempt_at = NULL,
                        lease_expires_at = NULL,
                        claimed_by = NULL,
                        claim_token = NULL,
                        workflow_execution_arn = NULL,
                        last_error = NULL
                    WHERE id = %s
                    RETURNING {_TASK_COLUMNS}
                    """,
                    (Jsonb(patch), requested_at, current.task_id),
                )
                updated = _task_record(cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO task_event
                        (task_id, business_id, event_type, actor_type, actor_id, data)
                    VALUES (%s, %s, 'task.revision_requested', 'owner', %s, %s)
                    """,
                    (
                        updated.task_id, updated.business_id, account_id,
                        Jsonb({
                            "revision": revision,
                            "instruction": text,
                            "base_artifact_id": current.output_artifact_id,
                        }),
                    ),
                )
        return updated

    def record_email_event(
        self, *, provider_event_id: str, message_id: str, event_type: str,
        event_at: datetime, tool_execution_id: str | None = None,
        recipient: str | None = None, link: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> EmailEventRecord | None:
        ensure_task_schema(self._conn)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                if tool_execution_id:
                    cur.execute(
                        """
                        SELECT id, task_id, business_id
                        FROM tool_execution
                        WHERE id = %s AND tool_name = 'ses.send_review_email'
                        """,
                        (tool_execution_id,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, task_id, business_id
                        FROM tool_execution
                        WHERE tool_name = 'ses.send_review_email'
                          AND external_reference = %s
                        ORDER BY started_at DESC
                        LIMIT 1
                        """,
                        (message_id,),
                    )
                tool = cur.fetchone()
                if tool is None:
                    return None
                cur.execute(
                    """
                    INSERT INTO email_event (
                        provider_event_id, tool_execution_id, task_id, business_id,
                        message_id, event_type, event_at, recipient, link, data
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (provider_event_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        provider_event_id, tool[0], tool[1], tool[2], message_id,
                        event_type, event_at, recipient, link, Jsonb(dict(data or {})),
                    ),
                )
                inserted = cur.fetchone()
                created = inserted is not None
                if inserted is None:
                    cur.execute(
                        """
                        SELECT id, provider_event_id, tool_execution_id, task_id,
                               business_id, message_id, event_type, event_at,
                               recipient, link, data
                        FROM email_event
                        WHERE provider_event_id = %s
                        """,
                        (provider_event_id,),
                    )
                    row = cur.fetchone()
                else:
                    cur.execute(
                        """
                        SELECT id, provider_event_id, tool_execution_id, task_id,
                               business_id, message_id, event_type, event_at,
                               recipient, link, data
                        FROM email_event
                        WHERE id = %s
                        """,
                        (inserted[0],),
                    )
                    row = cur.fetchone()
                    cur.execute(
                        """
                        INSERT INTO task_event
                            (task_id, business_id, event_type, actor_type, actor_id, data)
                        VALUES (%s, %s, %s, 'provider', 'amazon_ses', %s)
                        """,
                        (
                            tool[1], tool[2], f"email.{event_type}",
                            Jsonb({
                                "email_event_id": str(inserted[0]),
                                "tool_execution_id": str(tool[0]),
                                "message_id": message_id,
                                "recipient": recipient,
                                "link": link,
                            }),
                        ),
                    )
                if row is None:
                    return None
                return EmailEventRecord(
                    event_id=str(row[0]), provider_event_id=row[1],
                    tool_execution_id=str(row[2]), task_id=str(row[3]),
                    business_id=str(row[4]), message_id=row[5],
                    event_type=row[6], event_at=row[7], recipient=row[8],
                    link=row[9], data=_mapping(row[10]), created=created,
                )

    def email_events_for_tool(
        self, tool_execution_id: str, *, limit: int = 100,
    ) -> list[EmailEventRecord]:
        ensure_task_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, provider_event_id, tool_execution_id, task_id,
                       business_id, message_id, event_type, event_at,
                       recipient, link, data
                FROM email_event
                WHERE tool_execution_id = %s
                ORDER BY event_at ASC, created_at ASC
                LIMIT %s
                """,
                (tool_execution_id, max(0, int(limit))),
            )
            return [
                EmailEventRecord(
                    event_id=str(row[0]), provider_event_id=row[1],
                    tool_execution_id=str(row[2]), task_id=str(row[3]),
                    business_id=str(row[4]), message_id=row[5],
                    event_type=row[6], event_at=row[7], recipient=row[8],
                    link=row[9], data=_mapping(row[10]),
                )
                for row in cur.fetchall()
            ]

    # -- ledger ----------------------------------------------------------
    def insert_ledger_entry(
        self, business_id: str, *, find_id: str, verdict: str,
        predicted_daily_cents: int, actual_daily_cents: int | None,
        period_start: date, period_end: date, method: str,
        note: str | None = None, run_id: str | None = None,
        measured_at: datetime | None = None,
    ) -> str:
        # An unmeasured verdict writes NULL here, which a cluster still carrying
        # the old NOT NULL DEFAULT 0 rejects — and a rejected INSERT means the
        # Meter judges nothing at all. Self-heal on the way in, as every other
        # writer that outran a migration already does.
        ensure_decision_schema(self._conn)
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

    def update_ledger_entry(
        self, business_id: str, *, find_id: str, period_start: date,
        period_end: date, verdict: str, actual_daily_cents: int | None,
        method: str, note: str | None = None, run_id: str | None = None,
        measured_at: datetime | None = None,
    ) -> str:
        """Replace an estimate with a measurement. Nothing else.

        The append-only rule the ledger keeps is about *measured* verdicts: a
        published miss must not become a win because a better number turned up
        later. An estimate is not a measurement — it is the placeholder written
        because nobody had counted yet — so `verdict = 'estimated'` in the WHERE
        clause is the whole guard, enforced by the database rather than by a
        read-then-write the next request could interleave with.
        """
        ensure_decision_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ledger_entry
                   SET verdict = %s,
                       actual_daily_cents = %s,
                       method = %s,
                       note = %s,
                       run_id = %s,
                       measured_at = coalesce(%s, clock_timestamp())
                 WHERE business_id = %s
                   AND find_id = %s
                   AND period_start = %s
                   AND period_end = %s
                   AND verdict = 'estimated'
                RETURNING id
                """,
                (verdict, actual_daily_cents, method, note, run_id,
                 measured_at, business_id, find_id, period_start, period_end),
            )
            row = cur.fetchone()
        if row is None:
            raise RepositoryError(
                f"no estimated verdict for find {find_id} over "
                f"{period_start}..{period_end}"
            )
        return str(row[0])

    # -- outcomes the owner measured -------------------------------------
    def record_find_outcome(
        self, business_id: str, *, find_id: str, amount_cents: int, basis: str,
        note: str | None = None, reported_by_account_id: str | None = None,
        reported_at: datetime | None = None,
    ) -> OutcomeReport:
        # Raises before anything is written, so a bad period or a float amount
        # never reaches the table.
        daily = daily_cents_from(amount_cents, basis)
        ensure_outcome_schema(self._conn)

        with self._conn.cursor() as cur:
            # Scope and status in one statement: a find belonging to another
            # tenant and a find nobody acted on are both refused, and neither
            # answer tells the caller which it was.
            cur.execute(
                """
                SELECT status FROM find WHERE id = %s AND business_id = %s
                """,
                (find_id, business_id),
            )
            row = cur.fetchone()
            if row is None:
                raise RepositoryError("recommendation is no longer available")
            if row[0] not in JUDGEABLE_STATUSES:
                raise RepositoryError(
                    "only a move you accepted can have an outcome to report"
                )

            cur.execute(
                """
                INSERT INTO find_outcome (
                    business_id, find_id, reported_by_account_id,
                    amount_cents, basis, daily_cents, note, reported_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        coalesce(%s, clock_timestamp()))
                RETURNING id, reported_at
                """,
                (business_id, find_id, reported_by_account_id, amount_cents,
                 basis, daily, note, reported_at),
            )
            report_id, stored_at = cur.fetchone()

        return OutcomeReport(
            report_id=str(report_id), business_id=business_id,
            find_id=find_id, amount_cents=int(amount_cents), basis=basis,
            daily_cents=daily, reported_at=stored_at, note=note,
            reported_by_account_id=reported_by_account_id,
        )

    def find_outcome_reports(self, business_id: str) -> list[OutcomeReport]:
        """The current figure for every find that has one, newest first."""
        ensure_outcome_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (find_id)
                       id, find_id, amount_cents, basis, daily_cents, note,
                       reported_by_account_id, reported_at
                FROM find_outcome
                WHERE business_id = %s
                ORDER BY find_id, reported_at DESC
                """,
                (business_id,),
            )
            rows = cur.fetchall()
        reports = [self._outcome_row(business_id, r) for r in rows]
        reports.sort(key=lambda r: r.reported_at, reverse=True)
        return reports

    def find_outcome_history(self, business_id: str, *,
                             find_id: str) -> list[OutcomeReport]:
        ensure_outcome_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, find_id, amount_cents, basis, daily_cents, note,
                       reported_by_account_id, reported_at
                FROM find_outcome
                WHERE business_id = %s AND find_id = %s
                ORDER BY reported_at DESC
                """,
                (business_id, find_id),
            )
            return [self._outcome_row(business_id, r) for r in cur.fetchall()]

    @staticmethod
    def _outcome_row(business_id: str, row: Sequence[Any]) -> OutcomeReport:
        return OutcomeReport(
            report_id=str(row[0]), business_id=business_id,
            find_id=str(row[1]), amount_cents=int(row[2]), basis=row[3],
            daily_cents=int(row[4]), note=row[5],
            reported_by_account_id=str(row[6]) if row[6] else None,
            reported_at=row[7],
        )

    def remeasurable_finds(self, business_id: str) -> list[DueFind]:
        """Estimates whose owner has since reported a real number.

        Gated on the report being *newer* than the verdict. Without that the
        Meter would re-judge every estimate that ever had a figure, every
        night, rewriting the same row forever.

        The window comes off the ledger row rather than being recomputed: the
        update has to land on the verdict being replaced.
        """
        ensure_outcome_schema(self._conn)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.title, f.predicted_daily_cents,
                       le.period_end, f.created_at, le.period_start
                FROM ledger_entry le
                JOIN find f ON f.id = le.find_id
                JOIN (
                    SELECT find_id, max(reported_at) AS reported_at
                    FROM find_outcome
                    WHERE business_id = %s
                    GROUP BY find_id
                ) latest ON latest.find_id = le.find_id
                WHERE le.business_id = %s
                  AND le.verdict = 'estimated'
                  AND latest.reported_at > le.measured_at
                ORDER BY le.period_end, f.created_at
                """,
                (business_id, business_id),
            )
            return [
                DueFind(find_id=str(r[0]), title=r[1],
                        predicted_daily_cents=int(r[2]), verify_after=r[3],
                        created_at=r[4], measurement_start=r[5])
                for r in cur.fetchall()
            ]

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
