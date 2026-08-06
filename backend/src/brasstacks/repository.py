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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

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
from brasstacks.tasks import (
    MAKER_AGENT,
    MAKER_DRAFT_TASK,
    TASK_CANCELLED,
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

#: Statuses whose outcome the Meter is allowed to judge. A find the owner never
#: acted on has no outcome to measure.
JUDGEABLE_STATUSES = ("accepted", "live")

#: Statuses that mean this recommendation reached the owner's board. What she
#: then did with it — accepted it, passed on it, parked it, retired it — does not
#: matter here; she saw it, so raising it again wastes her night, and the
#: Analyst is told not to.
#:
#: This is an allowlist and has to stay one. The gates being built next withhold
#: a weak find instead of showing it, and a withheld find is not "already
#: proposed" — it is a topic the Analyst must stay free to raise once the
#: evidence is better. Naming the seen statuses means a verdict invented later
#: is unseen by default. Naming the unseen ones instead would mean any status
#: nobody remembered to register here silently becomes a do-not-repeat
#: instruction, and at three withheld finds a night that fills the Analyst's
#: twenty-row window in a week: one quiet night becomes a permanently empty
#: board.
OWNER_SEEN_STATUSES = (
    "proposed", "accepted", "later", "rejected", "live", "retired",
)

#: The pipeline produced this find and declined to show it, with the reason in
#: ``find.withheld_reason``.
#:
#: Deliberately absent from both tuples above. It is not an owner decision — she
#: never saw it — so the Meter has nothing to judge and the Analyst must stay
#: free to raise the topic once the evidence is better. The allowlist already
#: excludes it by construction; naming it here is for callers that need to
#: recognise the state, not for the filter.
WITHHELD_STATUS = "withheld"


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
    #: The page this came from. Carried through retrieval because without it
    #: every web observation reaches the Analyst as source_name "web", and three
    #: fragments of one fetch read as three independent signals.
    source_url: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class StoredObservation:
    """A row as memory holds it, with no retrieval metadata attached.

    Separate from ``Retrieved`` on purpose. A `similarity` of 0.0 and a `rank`
    of -1 would be two lies about a row nobody searched for, and `find_evidence`
    stores both numbers — a caller that confused the two would write a
    fabricated retrieval position into the audit trail.
    """

    observation_id: str
    content: str
    kind: str
    observed_at: datetime
    source_name: str | None = None
    source_url: str | None = None
    subject: str | None = None
    #: What the row asserts, as opposed to `kind`, which says where it came
    #: from. ``None`` means nobody looked — every row written before Radar
    #: classified, and never a guess.
    statement_type: str | None = None


@dataclass(frozen=True)
class EvidenceRef:
    """An observation the Analyst cited, and how close retrieval scored it.

    ``rank`` is the row's position in the merged retrieved set, which is what
    ``find_evidence.rank`` claims to hold. ``None`` means the caller has no
    retrieval position to offer and the citation order stands in for it.
    """

    observation_id: str
    similarity: float
    rank: int | None = None


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
class FindSummary:
    """A find as the Analyst needs to see it — enough to avoid repeating itself."""

    find_id: str
    title: str
    move: str
    status: str
    predicted_daily_cents: int
    created_at: datetime

    @property
    def seen_by_owner(self) -> bool:
        """Did this reach the owner's board?

        True is "you already proposed this, do not raise it again". False is
        "you considered this on an earlier night and it was never shown" —
        a different sentence in the prompt, because the second one is a topic
        the Analyst may still raise once the evidence supports it.

        Derived from the status rather than stored, so the two Repository
        implementations cannot disagree and no caller can hand-build a summary
        claiming a withheld find was on the board.
        """
        return self.status in OWNER_SEEN_STATUSES


@dataclass(frozen=True)
class DecisionTransition:
    """The durable result of changing one owner decision.

    ``changed`` is false for an idempotent retry. The previous status and
    timestamp let the application preserve an honest Pass -> Do it history
    without allowing accepted work to be silently reversed.
    """

    find_id: str
    previous_status: str
    status: str
    decided_at: datetime
    previous_decided_at: datetime | None = None
    decision_cycle: int = 1
    event_id: str | None = None
    changed: bool = True


#: Owner conversations share the existing observation vector index, while
#: remaining distinct from market signals in counts and Analyst retrieval.
CHAT_OWNER_SOURCE = "owner_chat"
CHAT_ASSISTANT_SOURCE = "ask_agent"
CHAT_SOURCES = frozenset({CHAT_OWNER_SOURCE, CHAT_ASSISTANT_SOURCE})


@dataclass(frozen=True)
class ChatMessage:
    """One durable message in the owner/Brass Tacks conversation."""

    message_id: str
    role: str
    content: str
    created_at: datetime
    find_id: str | None = None
    similarity: float | None = None


@dataclass(frozen=True)
class FindContext:
    """Compact recommendation context for a tenant-scoped Ask turn."""

    find_id: str
    title: str
    summary: str | None
    rationale: str
    move: str
    status: str
    predicted_daily_cents: int
    confidence: float
    verify_after: date
    decided_at: datetime | None = None
    decision_cycle: int = 1
    reopened_at: datetime | None = None
    reopen_reason_code: str | None = None
    reopen_reason_note: str | None = None
    #: The rival reading of the same evidence the Analyst rejected. NULL for
    #: every find written before 2026-08-03, and for any night where the model
    #: skipped the field — absent, not "none considered".
    alternative_explanation: str | None = None
    #: Why a quality gate declined to show this one. NULL on every find that
    #: reached the board: "nothing withheld this", never an empty reason.
    withheld_reason: str | None = None
    #: The tier the find was published at, after any demotion. The difference
    #: between "worth a look" and "your storefront is broken", so it has to
    #: survive the process that decided it. NULL on finds written before tiers
    #: existed — absent, never a default of "opportunity".
    claim_type: str | None = None
    #: Compact, bounded labels for the For You decision surface. Revenue,
    #: evidence, approval and timing remain separate deterministic fields.
    #: JSON-shaped so the repository contract stays independent of the
    #: Analyst's dataclass. None on finds written before the field existed.
    feed_brief: Mapping[str, Any] | None = None
    #: What the Coster priced this move at: setup and daily cents, the basis,
    #: and the lines behind them. ``None`` means nobody priced it — an outage,
    #: no coster wired, or a find older than the column. Never read as zero:
    #: unpriced and free are different facts and only one of them is a claim
    #: about the owner's money.
    cost_estimate: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StoredArtifact:
    """A versioned, owner-reviewable deliverable produced by Maker.

    The detailed body remains durable in CockroachDB even when S3 is
    unavailable. Structured review fields let the UI and email renderer show
    the smallest useful next action instead of dumping raw Markdown on a busy
    owner.
    """

    artifact_id: str
    find_id: str
    kind: str
    title: str
    created_at: datetime
    preview: str | None = None
    s3_bucket: str | None = None
    s3_key: str | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    body: str | None = None
    summary: str | None = None
    owner_action: str | None = None
    review_state: str = "ready_for_review"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 1
    parent_artifact_id: str | None = None
    decision_cycle: int = 1
    superseded_at: datetime | None = None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    agent: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    note: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class OwnerRule:
    """A constraint the autopilot obeys on every run — "the leash you hold"."""

    rule_id: str
    rule: str
    cap_cents: int | None = None


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
                        goal_monthly_cents: int | None = ...,
                        latitude: float | None = ...,
                        longitude: float | None = ...) -> str: ...

    def get_business(self, business_id: str) -> dict[str, Any] | None: ...

    def get_owner_profile(self, account_id: str, *, business_id: str) -> dict[str, Any] | None: ...

    def update_account_profile(self, account_id: str, *, display_name: str,
                               email: str) -> None: ...

    def owner_email_for_business(
        self, business_id: str, *, preferred_account_id: str | None = ...
    ) -> str | None: ...

    def update_business_profile(
        self, business_id: str, *, name: str, category: str, city: str,
        profile_data: Mapping[str, Any], latitude: float | None = ...,
        longitude: float | None = ...,
    ) -> None: ...

    def replace_business_profile_facts(
        self, business_id: str, *, facts: Sequence[str],
        embeddings: Sequence[Sequence[float]], source: str,
    ) -> list[str]: ...

    def insert_business_fact(self, business_id: str, *, fact: str, source: str,
                             embedding: Sequence[float],
                             confidence: float = ...) -> str: ...

    def supersede_business_fact(self, fact_id: str, *, superseded_by: str) -> None: ...

    def get_business_facts(self, business_id: str) -> list[str]: ...

    def insert_owner_rule(self, business_id: str, *, rule: str, enabled: bool = ...,
                          cap_cents: int | None = ...) -> str: ...

    def get_owner_rules(self, business_id: str) -> list[OwnerRule]: ...

    def start_run(self, business_id: str, *, agent: str,
                  model_id: str | None = ...) -> str: ...

    def finish_run(self, run_id: str, *, status: str, error: str | None = ...,
                   note: str | None = ..., input_tokens: int | None = ...,
                   output_tokens: int | None = ...) -> None: ...

    def recent_runs(self, business_id: str, *, limit: int) -> list[RunRecord]: ...

    def insert_observation(self, business_id: str, *, content: str, kind: str,
                           embedding: Sequence[float], observed_at: datetime,
                           source_name: str | None = ..., source_url: str | None = ...,
                           subject: str | None = ..., rating: float | None = ...,
                           run_id: str | None = ...,
                           statement_type: str | None = ...) -> str | None: ...

    def count_observations(self, business_id: str) -> int: ...

    def purge_observations(self, business_id: str, *, source_name: str,
                           older_than: datetime) -> int: ...

    def search_observations(self, business_id: str, query_embedding: Sequence[float],
                            *, limit: int) -> list[Retrieved]: ...

    def all_observations(self, business_id: str, *,
                         limit: int) -> list[StoredObservation]: ...

    def insert_chat_message(
        self, business_id: str, *, role: str, content: str, created_at: datetime,
        embedding: Sequence[float] | None = ..., find_id: str | None = ...,
        run_id: str | None = ...,
    ) -> str: ...

    def recent_chat_messages(
        self, business_id: str, *, limit: int, find_id: str | None = ...,
    ) -> list[ChatMessage]: ...

    def search_chat_messages(
        self, business_id: str, query_embedding: Sequence[float], *, limit: int,
    ) -> list[ChatMessage]: ...

    def count_chat_messages(self, business_id: str) -> int: ...

    def get_find_context(self, business_id: str, find_id: str) -> FindContext | None: ...

    def insert_find_with_evidence(
        self, business_id: str, *, title: str, rationale: str, move: str,
        emoji: str, predicted_daily_cents: int, confidence: float,
        verify_after: date, evidence: Sequence[EvidenceRef],
        summary: str | None = ...,
        alternative_explanation: str | None = ...,
        feed_brief: Mapping[str, Any] | None = ...,
        cost_estimate: Mapping[str, Any] | None = ...,
        status: str = ..., withheld_reason: str | None = ...,
        claim_type: str | None = ...,
        run_id: str | None = ...,
        created_at: datetime | None = ..., decided_at: datetime | None = ...,
    ) -> str: ...

    def set_find_status(self, find_id: str, *, status: str,
                        decided_at: datetime | None = ...,
                        business_id: str | None = ...,
                        actor_account_id: str | None = ...,
                        source: str = ...) -> DecisionTransition: ...

    def reconsider_find(
        self, find_id: str, *, business_id: str,
        actor_account_id: str | None = ...,
        reason_code: str | None = ..., reason_note: str | None = ...,
        source: str = ..., reopened_at: datetime | None = ...,
    ) -> ReconsiderResult: ...

    def decision_events(
        self, find_id: str, *, business_id: str | None = ..., limit: int = ...,
    ) -> list[DecisionEvent]: ...

    def get_find_evidence(self, find_id: str) -> list[StoredEvidence]: ...

    def count_finds(self, business_id: str) -> int: ...

    def latest_find_created_at(self, business_id: str) -> datetime | None: ...

    def recent_finds(self, business_id: str, *, limit: int,
                     include_unseen: bool = ...) -> list[FindSummary]: ...

    def due_finds(self, business_id: str, *, today: date) -> list[DueFind]: ...

    def insert_artifact(self, *, find_id: str, kind: str, title: str,
                        preview: str | None = ..., s3_bucket: str | None = ...,
                        s3_key: str | None = ..., run_id: str | None = ...,
                        task_id: str | None = ...,
                        idempotency_key: str | None = ...,
                        body: str | None = ..., summary: str | None = ...,
                        owner_action: str | None = ...,
                        review_state: str = ...,
                        metadata: Mapping[str, Any] | None = ...,
                        revision: int = ...,
                        parent_artifact_id: str | None = ...,
                        decision_cycle: int = ...) -> str: ...

    def get_artifacts(self, find_id: str) -> list[StoredArtifact]: ...

    def get_artifact_by_idempotency_key(
        self, idempotency_key: str,
    ) -> StoredArtifact | None: ...

    def create_or_get_maker_task(
        self, business_id: str, *, find_id: str,
        requested_by_account_id: str | None = ...,
        approved_at: datetime | None = ..., priority: int = ...,
        input_data: Mapping[str, Any] | None = ...,
    ) -> TaskRecord: ...

    def get_task(
        self, task_id: str, *, business_id: str | None = ...,
    ) -> TaskRecord | None: ...

    def prepare_task_dispatch(self, task_id: str) -> TaskRecord | None: ...

    def record_task_workflow(
        self, task_id: str, *, execution_arn: str,
    ) -> TaskRecord | None: ...

    def claim_task(
        self, task_id: str, *, worker_id: str, lease_seconds: int = ...,
    ) -> TaskRecord | None: ...

    def task_can_continue(
        self, task_id: str, *, claim_token: str,
    ) -> bool: ...

    def complete_task(
        self, task_id: str, *, claim_token: str,
        output_artifact_id: str | None = ...,
        output_data: Mapping[str, Any] | None = ...,
    ) -> TaskRecord: ...

    def fail_task(
        self, task_id: str, *, claim_token: str, error: str,
        retryable: bool = ..., retry_after_seconds: int = ...,
    ) -> TaskRecord: ...

    def list_tasks(
        self, business_id: str, *, statuses: Sequence[str] | None = ...,
        limit: int = ...,
    ) -> list[TaskRecord]: ...

    def list_dispatchable_tasks(
        self, *, limit: int = ..., per_business_limit: int = ...
    ) -> list[TaskRecord]: ...

    def recover_stale_tasks(
        self, *, now: datetime, max_attempts: int = ...,
    ) -> int: ...

    def record_task_event(
        self, task_id: str, *, event_type: str, actor_type: str = ...,
        actor_id: str | None = ..., data: Mapping[str, Any] | None = ...,
    ) -> str: ...

    def task_events(self, task_id: str, *, limit: int = ...) -> list[TaskEvent]: ...

    def start_tool_execution(
        self, *, task_id: str, business_id: str, tool_name: str,
        idempotency_key: str, input_data: Mapping[str, Any] | None = ...,
    ) -> ToolExecutionRecord: ...

    def finish_tool_execution(
        self, execution_id: str, *, status: str,
        output_data: Mapping[str, Any] | None = ...,
        external_reference: str | None = ..., error: str | None = ...,
    ) -> ToolExecutionRecord: ...

    def tool_executions(
        self, task_id: str, *, limit: int = ...,
    ) -> list[ToolExecutionRecord]: ...

    def request_maker_revision(
        self, *, business_id: str, find_id: str, account_id: str | None,
        instruction: str, requested_at: datetime | None = ...,
    ) -> TaskRecord: ...

    def record_email_event(
        self, *, provider_event_id: str, message_id: str, event_type: str,
        event_at: datetime, tool_execution_id: str | None = ...,
        recipient: str | None = ..., link: str | None = ...,
        data: Mapping[str, Any] | None = ...,
    ) -> EmailEventRecord | None: ...

    def email_events_for_tool(
        self, tool_execution_id: str, *, limit: int = ...,
    ) -> list[EmailEventRecord]: ...

    def insert_ledger_entry(
        self, business_id: str, *, find_id: str, verdict: str,
        predicted_daily_cents: int, actual_daily_cents: int | None,
        period_start: date, period_end: date, method: str,
        note: str | None = ..., run_id: str | None = ...,
        measured_at: datetime | None = ...,
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
    embedding: list[float] | None
    observed_at: datetime
    content_hash: str
    source_name: str | None = None
    source_url: str | None = None
    subject: str | None = None
    rating: float | None = None
    run_id: str | None = None
    statement_type: str | None = None


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
    decided_at: datetime | None = None
    decision_cycle: int = 1
    reopened_at: datetime | None = None
    reopen_reason_code: str | None = None
    reopen_reason_note: str | None = None
    run_id: str | None = None
    claim_type: str | None = None
    #: Why a quality gate declined to show this one; None on everything that
    #: reached the board.
    withheld_reason: str | None = None
    #: One sentence for the card face; the rationale is the full argument.
    summary: str | None = None
    #: The strongest rival reading of the evidence, and why it was rejected.
    alternative_explanation: str | None = None
    #: Compact owner-facing metadata; absent on legacy rows.
    feed_brief: dict[str, Any] | None = None
    #: What the move costs. Absent on legacy rows and on any night the costing
    #: pass could not run; never zero to mean "we did not price it".
    cost_estimate: dict[str, Any] | None = None
    evidence: list[StoredEvidence] = field(default_factory=list)


@dataclass
class _Artifact:
    artifact_id: str
    find_id: str
    kind: str
    title: str
    created_at: datetime
    preview: str | None = None
    s3_bucket: str | None = None
    s3_key: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    body: str | None = None
    summary: str | None = None
    owner_action: str | None = None
    review_state: str = "ready_for_review"
    metadata: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    parent_artifact_id: str | None = None
    decision_cycle: int = 1
    superseded_at: datetime | None = None


@dataclass
class _LedgerEntry:
    entry_id: str
    business_id: str
    find_id: str
    verdict: str
    predicted_daily_cents: int
    # None where nothing was measured. Mirrors the nullable column: an estimate
    # has no actual, and storing 0 for one would report an unchecked move as a
    # move that earned nothing.
    actual_daily_cents: int | None
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
        self._facts: dict[str, dict[str, Any]] = {}
        self._rules: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, RunRecord] = {}
        self._run_business: dict[str, str] = {}
        self._observations: list[_Observation] = []
        self._finds: dict[str, _Find] = {}
        self._decision_events: list[DecisionEvent] = []
        self._artifacts: list[_Artifact] = []
        self._tasks: dict[str, dict[str, Any]] = {}
        self._task_by_idempotency: dict[str, str] = {}
        self._task_events: list[TaskEvent] = []
        self._tool_executions: dict[str, ToolExecutionRecord] = {}
        self._tool_by_idempotency: dict[str, str] = {}
        self._email_events: dict[str, EmailEventRecord] = {}
        self._ledger: list[_LedgerEntry] = []
        self._accounts: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
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
                        goal_monthly_cents: int | None = None,
                        latitude: float | None = None,
                        longitude: float | None = None) -> str:
        business_id = str(uuid.uuid4())
        self._businesses[business_id] = {
            "name": name, "category": category, "city": city,
            "goal_monthly_cents": goal_monthly_cents,
            "latitude": latitude, "longitude": longitude,
            "profile_data": {}, "profile_updated_at": None,
        }
        return business_id

    def get_business(self, business_id: str) -> dict[str, Any] | None:
        business = self._businesses.get(business_id)
        if business is None:
            return None
        return {"id": business_id, **dict(business)}

    def get_owner_profile(self, account_id: str, *, business_id: str) -> dict[str, Any] | None:
        account = next((row for row in self._accounts.values()
                        if row["id"] == account_id and row.get("business_id") == business_id), None)
        business = self._businesses.get(business_id)
        if account is None or business is None:
            return None
        return {
            "account_id": account_id, "business_id": business_id,
            "username": account.get("username"),
            "display_name": account.get("display_name"),
            "email": account.get("email"),
            "business_name": business.get("name"),
            "category": business.get("category"),
            "city": business.get("city"),
            "profile_data": dict(business.get("profile_data") or {}),
            "profile_updated_at": business.get("profile_updated_at"),
        }

    def update_account_profile(self, account_id: str, *, display_name: str,
                               email: str) -> None:
        for row in self._accounts.values():
            if row["id"] == account_id:
                row["display_name"] = display_name
                row["email"] = email
                return
        raise RepositoryError(f"unknown account {account_id}")

    def owner_email_for_business(
        self, business_id: str, *, preferred_account_id: str | None = None
    ) -> str | None:
        candidates = [row for row in self._accounts.values()
                      if row.get("business_id") == business_id and str(row.get("email") or "").strip()]
        candidates.sort(key=lambda row: 0 if preferred_account_id and row["id"] == preferred_account_id else 1)
        return str(candidates[0]["email"]).strip() if candidates else None

    def update_business_profile(
        self, business_id: str, *, name: str, category: str, city: str,
        profile_data: Mapping[str, Any], latitude: float | None = None,
        longitude: float | None = None,
    ) -> None:
        row = self._businesses.get(business_id)
        if row is None:
            raise RepositoryError(f"unknown business {business_id}")
        row.update({
            "name": name, "category": category, "city": city,
            "profile_data": dict(profile_data), "profile_updated_at": self._now(),
        })
        if latitude is not None and longitude is not None:
            row["latitude"], row["longitude"] = latitude, longitude

    def replace_business_profile_facts(
        self, business_id: str, *, facts: Sequence[str],
        embeddings: Sequence[Sequence[float]], source: str,
    ) -> list[str]:
        if len(facts) != len(embeddings):
            raise RepositoryError("profile facts and embeddings must have the same length")
        old_ids = [fact_id for fact_id, row in self._facts.items()
                   if row["business_id"] == business_id and row["source"] == source
                   and row.get("profile_managed", False)
                   and row.get("superseded_by") is None]
        new_ids = []
        for fact, embedding in zip(facts, embeddings):
            fact_id = self.insert_business_fact(
                business_id, fact=fact, source=source, embedding=embedding
            )
            self._facts[fact_id]["profile_managed"] = True
            new_ids.append(fact_id)
        if new_ids:
            for fact_id in old_ids:
                self._facts[fact_id]["superseded_by"] = new_ids[0]
        return new_ids

    # -- owners ----------------------------------------------------------
    def create_account(self, business_id: str | None, *, username: str,
                       password_hash: str) -> str:
        if username in self._accounts:
            raise RepositoryError(f"username {username!r} is already taken")
        account_id = str(uuid.uuid4())
        self._accounts[username] = {
            "id": account_id, "business_id": business_id,
            "username": username, "password_hash": password_hash,
            "display_name": None, "email": None,
            "is_admin": False,
        }
        return account_id

    def attach_business(self, account_id: str, *, business_id: str) -> None:
        """Point an account at the business it just created.

        Separate from create_account because signup is two steps: the account
        exists from the credentials page, the business from the profile page.
        """
        for row in self._accounts.values():
            if row["id"] == account_id:
                row["business_id"] = business_id
                for session in self._sessions.values():
                    if session["account_id"] == account_id:
                        session["business_id"] = business_id
                return
        raise RepositoryError(f"unknown account {account_id}")

    def account_for_session(self, token_hash: str, *,
                            now: datetime) -> dict[str, Any] | None:
        session = self._sessions.get(token_hash)
        if session is None or session["expires_at"] <= now:
            return None
        account = next(
            (a for a in self._accounts.values()
             if a["id"] == session["account_id"]), {})
        return {"account_id": session["account_id"],
                # The account is authoritative. A business can be attached after
                # the session was created during two-step onboarding.
                "business_id": account.get("business_id") or session["business_id"],
                # From the account, so revoking admin takes effect at once.
                "is_admin": bool(account.get("is_admin"))}

    def find_account(self, username: str) -> dict[str, Any] | None:
        """None rather than raising: the login handler must do the same work
        whether or not the user exists, or "no such user" becomes a timing
        signal and a different code path."""
        found = self._accounts.get(username)
        return dict(found) if found else None

    def create_session(self, token_hash: str, *, business_id: str,
                       account_id: str, expires_at: datetime) -> None:
        self._sessions[token_hash] = {
            "business_id": business_id, "account_id": account_id,
            "expires_at": expires_at,
        }

    def business_for_session(self, token_hash: str, *,
                             now: datetime) -> str | None:
        session = self._sessions.get(token_hash)
        if session is None or session["expires_at"] <= now:
            return None
        return session["business_id"]

    def delete_session(self, token_hash: str) -> None:
        self._sessions.pop(token_hash, None)

    def set_account_admin(self, username: str, *, is_admin: bool) -> None:
        if username not in self._accounts:
            raise RepositoryError(f"unknown account {username!r}")
        self._accounts[username]["is_admin"] = is_admin

    def set_business_status(self, business_id: str, *, status: str) -> None:
        if business_id not in self._businesses:
            raise RepositoryError(f"unknown business {business_id}")
        self._businesses[business_id]["status"] = status

    def active_business_ids(self, *, limit: int = 50) -> list[str]:
        """Tenants the agents work for tonight, oldest first and capped.

        Every one costs a search, embeddings and a Claude call per night, so the
        cap is a spend control rather than pagination. Oldest first so a burst
        of signups cannot push an established tenant out of tonight's run.
        """
        active = [
            business_id for business_id, row in self._businesses.items()
            if row.get("status", "active") == "active"
        ]
        return active[:limit]

    # -- profile ---------------------------------------------------------
    def insert_business_fact(self, business_id: str, *, fact: str, source: str,
                             embedding: Sequence[float],
                             confidence: float = 1.0) -> str:
        fact_id = str(uuid.uuid4())
        self._facts[fact_id] = {
            "business_id": business_id, "fact": fact, "source": source,
            "confidence": confidence, "embedding": list(embedding),
            "profile_managed": False,
            "superseded_by": None, "learned_at": self._now(),
        }
        return fact_id

    def supersede_business_fact(self, fact_id: str, *, superseded_by: str) -> None:
        existing = self._facts.get(fact_id)
        if existing is None:
            raise RepositoryError(f"unknown fact {fact_id}")
        existing["superseded_by"] = superseded_by

    def get_business_facts(self, business_id: str) -> list[str]:
        current = [
            f for f in self._facts.values()
            if f["business_id"] == business_id and f["superseded_by"] is None
        ]
        current.sort(key=lambda f: f["learned_at"], reverse=True)
        return [f["fact"] for f in current]

    def insert_owner_rule(self, business_id: str, *, rule: str, enabled: bool = True,
                          cap_cents: int | None = None) -> str:
        rule_id = str(uuid.uuid4())
        self._rules[rule_id] = {
            "business_id": business_id, "rule": rule, "enabled": enabled,
            "cap_cents": cap_cents,
        }
        return rule_id

    def get_owner_rules(self, business_id: str) -> list[OwnerRule]:
        return [
            OwnerRule(rule_id=rule_id, rule=r["rule"], cap_cents=r["cap_cents"])
            for rule_id, r in self._rules.items()
            if r["business_id"] == business_id and r["enabled"]
        ]

    # -- runs ------------------------------------------------------------
    def start_run(self, business_id: str, *, agent: str,
                  model_id: str | None = None) -> str:
        run_id = str(uuid.uuid4())
        self._runs[run_id] = RunRecord(
            run_id=run_id, agent=agent, status="running", started_at=self._now(),
        )
        self._run_business[run_id] = business_id
        return run_id

    def finish_run(self, run_id: str, *, status: str, error: str | None = None,
                   note: str | None = None, input_tokens: int | None = None,
                   output_tokens: int | None = None) -> None:
        existing = self._runs.get(run_id)
        if existing is None:
            raise RepositoryError(f"unknown run {run_id}")
        self._runs[run_id] = RunRecord(
            run_id=run_id, agent=existing.agent, status=status,
            started_at=existing.started_at, finished_at=self._now(),
            error=error, note=note, input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def recent_runs(self, business_id: str, *, limit: int) -> list[RunRecord]:
        runs = [
            run for run_id, run in self._runs.items()
            if self._run_business.get(run_id) == business_id
        ]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    # -- observations ----------------------------------------------------
    def insert_observation(self, business_id: str, *, content: str, kind: str,
                           embedding: Sequence[float], observed_at: datetime,
                           source_name: str | None = None,
                           source_url: str | None = None,
                           subject: str | None = None, rating: float | None = None,
                           run_id: str | None = None,
                           statement_type: str | None = None) -> str | None:
        digest = content_hash(content)
        # Dedup is scoped per business: two restaurants can share a phrase.
        for existing in self._observations:
            if existing.business_id == business_id and existing.content_hash == digest:
                return None
        observation_id = str(uuid.uuid4())
        self._observations.append(_Observation(
            observation_id=observation_id, business_id=business_id,
            content=content, kind=kind, embedding=list(embedding),
            observed_at=observed_at, content_hash=digest, source_name=source_name,
            source_url=source_url, subject=subject, rating=rating, run_id=run_id,
            statement_type=statement_type,
        ))
        return observation_id

    def purge_observations(self, business_id: str, *, source_name: str,
                           older_than: datetime) -> int:
        """Delete this source's observations older than the cutoff.

        Exists because some sources are licensed rather than owned — Yelp
        forbids retaining its content beyond 24 hours. Scoped to one source so
        a licence term can never reach the corpus we do own.
        """
        doomed = [
            o for o in self._observations
            if o.business_id == business_id
            and o.source_name == source_name
            and o.observed_at < older_than
        ]
        for observation in doomed:
            self._observations.remove(observation)
        return len(doomed)

    def count_observations(self, business_id: str) -> int:
        return sum(
            1 for o in self._observations
            if o.business_id == business_id and o.source_name not in CHAT_SOURCES
        )

    def search_observations(self, business_id: str, query_embedding: Sequence[float],
                            *, limit: int) -> list[Retrieved]:
        scored = [
            (cosine_similarity(query_embedding, o.embedding), o)
            for o in self._observations
            if o.business_id == business_id
            and o.source_name not in CHAT_SOURCES
            and o.embedding is not None
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            Retrieved(
                observation_id=o.observation_id, content=o.content, kind=o.kind,
                similarity=similarity, rank=rank, observed_at=o.observed_at,
                source_name=o.source_name, source_url=o.source_url,
                subject=o.subject,
            )
            for rank, (similarity, o) in enumerate(scored[:limit])
        ]

    def all_observations(self, business_id: str, *,
                         limit: int) -> list[StoredObservation]:
        mine = [
            o for o in self._observations
            if o.business_id == business_id and o.source_name not in CHAT_SOURCES
        ]
        mine.sort(key=lambda o: o.observed_at)
        return [
            StoredObservation(
                observation_id=o.observation_id, content=o.content, kind=o.kind,
                observed_at=o.observed_at, source_name=o.source_name,
                source_url=o.source_url, subject=o.subject,
                statement_type=o.statement_type,
            )
            for o in mine[:limit]
        ]

    # -- owner conversation memory --------------------------------------
    def insert_chat_message(
        self, business_id: str, *, role: str, content: str, created_at: datetime,
        embedding: Sequence[float] | None = None, find_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        if role not in {"user", "assistant"}:
            raise RepositoryError("chat role must be 'user' or 'assistant'")
        message_id = str(uuid.uuid4())
        source_name = CHAT_OWNER_SOURCE if role == "user" else CHAT_ASSISTANT_SOURCE
        digest = content_hash(f"chat:{message_id}:{role}:{find_id or ''}:{content}")
        self._observations.append(_Observation(
            observation_id=message_id, business_id=business_id,
            content=content, kind="owner_upload",
            embedding=list(embedding) if embedding is not None else None,
            observed_at=created_at, content_hash=digest,
            source_name=source_name, subject=find_id, run_id=run_id,
        ))
        return message_id

    def recent_chat_messages(
        self, business_id: str, *, limit: int, find_id: str | None = None,
    ) -> list[ChatMessage]:
        rows = [
            o for o in self._observations
            if o.business_id == business_id
            and o.source_name in CHAT_SOURCES
            and (find_id is None or o.subject == find_id)
        ]
        # Ask stores the owner and assistant rows in the same turn. If their
        # timestamps tie, put the assistant first in the descending query so
        # reversing the window returns the natural owner -> assistant order.
        rows.sort(
            key=lambda o: (
                o.observed_at,
                o.source_name == CHAT_ASSISTANT_SOURCE,
            ),
            reverse=True,
        )
        rows = list(reversed(rows[:limit]))
        return [
            ChatMessage(
                message_id=o.observation_id,
                role="user" if o.source_name == CHAT_OWNER_SOURCE else "assistant",
                content=o.content, created_at=o.observed_at, find_id=o.subject,
            )
            for o in rows
        ]

    def search_chat_messages(
        self, business_id: str, query_embedding: Sequence[float], *, limit: int,
    ) -> list[ChatMessage]:
        scored = [
            (cosine_similarity(query_embedding, o.embedding), o)
            for o in self._observations
            if o.business_id == business_id
            and o.source_name == CHAT_OWNER_SOURCE
            and o.embedding is not None
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            ChatMessage(
                message_id=o.observation_id, role="user", content=o.content,
                created_at=o.observed_at, find_id=o.subject, similarity=similarity,
            )
            for similarity, o in scored[:limit]
        ]

    def count_chat_messages(self, business_id: str) -> int:
        return sum(
            1 for o in self._observations
            if o.business_id == business_id and o.source_name in CHAT_SOURCES
        )

    def get_find_context(self, business_id: str, find_id: str) -> FindContext | None:
        found = self._finds.get(find_id)
        if found is None or found.business_id != business_id:
            return None
        return FindContext(
            find_id=found.find_id, title=found.title, summary=found.summary,
            rationale=found.rationale, move=found.move, status=found.status,
            predicted_daily_cents=found.predicted_daily_cents,
            confidence=found.confidence, verify_after=found.verify_after,
            decided_at=found.decided_at, decision_cycle=found.decision_cycle,
            reopened_at=found.reopened_at,
            reopen_reason_code=found.reopen_reason_code,
            reopen_reason_note=found.reopen_reason_note,
            alternative_explanation=found.alternative_explanation,
            withheld_reason=found.withheld_reason,
            claim_type=found.claim_type,
            feed_brief=(dict(found.feed_brief)
                        if found.feed_brief is not None else None),
            cost_estimate=(dict(found.cost_estimate)
                           if found.cost_estimate is not None else None),
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
            summary=summary, rationale=rationale, move=move, emoji=emoji,
            predicted_daily_cents=predicted_daily_cents, confidence=confidence,
            verify_after=verify_after, status=status,
            created_at=created_at if created_at is not None else self._now(),
            decided_at=decided_at,
            run_id=run_id,
            alternative_explanation=alternative_explanation,
            feed_brief=(dict(feed_brief) if feed_brief is not None else None),
            cost_estimate=(dict(cost_estimate)
                           if cost_estimate is not None else None),
            withheld_reason=withheld_reason,
            claim_type=claim_type,
            # rank is the row's position in what retrieval returned, not the
            # order the model happened to cite in. Falling back to the citation
            # index only covers callers with no retrieval position to give;
            # citation order itself is deliberately not stored, because it
            # carries no claim anyone reads it as making.
            evidence=[
                StoredEvidence(observation_id=ref.observation_id,
                               similarity=ref.similarity,
                               rank=cited if ref.rank is None else int(ref.rank))
                for cited, ref in enumerate(evidence)
            ],
        )
        return find_id

    def _append_decision_event(
        self, *, found: _Find, event_type: str,
        previous_status: str | None, new_status: str,
        actor_account_id: str | None, source: str,
        created_at: datetime, reason_code: str | None = None,
        reason_note: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        self._decision_events.append(DecisionEvent(
            event_id=event_id,
            business_id=found.business_id,
            find_id=found.find_id,
            decision_cycle=found.decision_cycle,
            event_type=event_type,
            previous_status=previous_status,
            new_status=new_status,
            actor_account_id=actor_account_id,
            reason_code=reason_code,
            reason_note=reason_note,
            source=source,
            data=dict(data or {}),
            created_at=created_at,
        ))
        return event_id

    def set_find_status(self, find_id: str, *, status: str,
                        decided_at: datetime | None = None,
                        business_id: str | None = None,
                        actor_account_id: str | None = None,
                        source: str = "owner_decision") -> DecisionTransition:
        found = self._finds.get(find_id)
        if found is None or (business_id is not None and found.business_id != business_id):
            raise RepositoryError("recommendation is no longer available")
        if found.status == WITHHELD_STATUS:
            # The owner was never shown this, so there is no decision of hers to
            # record. A stale tab or a crafted request must not be able to put a
            # find the pipeline itself refused to stand behind onto the board,
            # where it would queue Maker work and add its prediction to the
            # forecast. Same message as an unknown id: the handlers turn it into
            # a 409 and it reveals nothing about what exists.
            raise RepositoryError("recommendation is no longer available")

        moment = decided_at or self._now()
        previous_status = found.status
        previous_decided_at = found.decided_at
        if found.status == status:
            return DecisionTransition(
                find_id=find_id,
                previous_status=previous_status,
                status=status,
                decided_at=previous_decided_at or moment,
                previous_decided_at=previous_decided_at,
                decision_cycle=found.decision_cycle,
                changed=False,
            )

        normal_decision = found.status in ("proposed", "later") and status in (
            "accepted", "rejected", "later"
        )
        undo_pass = found.status == "rejected" and status == "accepted"
        if normal_decision or undo_pass:
            # A pass is reversible because no work has been produced from it.
            # The only reverse transition is Pass -> Do it; accepted work cannot
            # later be rewritten as rejected once Maker or Meter may have acted.
            found.status = status
            found.decided_at = moment
            if undo_pass:
                event_type = DECISION_UNDO_PASS
            elif status == "accepted":
                event_type = DECISION_ACCEPTED
            elif status == "rejected":
                event_type = DECISION_PASSED
            else:
                event_type = DECISION_SAVED_LATER
            event_id = self._append_decision_event(
                found=found,
                event_type=event_type,
                previous_status=previous_status,
                new_status=status,
                actor_account_id=actor_account_id,
                source=source,
                created_at=moment,
            )
            return DecisionTransition(
                find_id=find_id,
                previous_status=previous_status,
                status=status,
                decided_at=moment,
                previous_decided_at=previous_decided_at,
                decision_cycle=found.decision_cycle,
                event_id=event_id,
                changed=True,
            )
        raise RepositoryError(f"find already decided as {found.status}")

    def reconsider_find(
        self, find_id: str, *, business_id: str,
        actor_account_id: str | None = None,
        reason_code: str | None = None, reason_note: str | None = None,
        source: str = "owner_reconsider",
        reopened_at: datetime | None = None,
    ) -> ReconsiderResult:
        found = self._finds.get(find_id)
        if found is None or found.business_id != business_id:
            raise RepositoryError("recommendation is no longer available")
        if reason_code and reason_code not in RECONSIDER_REASON_CODES:
            raise RepositoryError("choose a valid reconsideration reason")
        moment = reopened_at or self._now()
        # Preserve causal ordering even when an imported decision timestamp is
        # ahead of the local synthetic/test clock or a worker clock is skewed.
        if found.decided_at is not None and moment <= found.decided_at:
            moment = found.decided_at + timedelta(microseconds=1)
        if found.status == "proposed" and found.reopened_at is not None:
            return ReconsiderResult(
                find_id=find_id,
                previous_status="accepted",
                status="proposed",
                previous_cycle=max(1, found.decision_cycle - 1),
                decision_cycle=found.decision_cycle,
                reopened_at=found.reopened_at,
                changed=False,
            )
        if found.status != "accepted":
            raise RepositoryError(
                "only an approved recommendation that has not changed a customer-facing system can be reconsidered"
            )

        if any(entry.find_id == find_id for entry in self._ledger):
            raise RepositoryError(
                "this move already has a Meter result; create a new recommendation revision instead"
            )

        previous_cycle = found.decision_cycle
        # Imported or pre-migration decisions may have only the current find
        # projection. Before opening a new cycle, backfill an explicitly marked
        # accepted event so the original Do it can never disappear from history.
        has_accept_event = any(
            event.find_id == find_id
            and event.decision_cycle == previous_cycle
            and event.event_type in {DECISION_ACCEPTED, DECISION_UNDO_PASS}
            for event in self._decision_events
        )
        if not has_accept_event:
            self._append_decision_event(
                found=found,
                event_type=DECISION_ACCEPTED,
                previous_status="proposed",
                new_status="accepted",
                actor_account_id=None,
                source="legacy_projection_backfill",
                created_at=found.decided_at or found.created_at,
                data={"inferred_from_find_projection": True},
            )

        task_rows = [
            row for row in self._tasks.values()
            if row.get("find_id") == find_id
            and int(row.get("decision_cycle") or 1) <= previous_cycle
            and row.get("superseded_at") is None
        ]
        irreversible = sorted({
            execution.tool_name
            for execution in self._tool_executions.values()
            if execution.status in {"running", "succeeded"}
            and execution.tool_name not in REVERSIBLE_REVIEW_TOOLS
            and any(row["task_id"] == execution.task_id for row in task_rows)
        })
        if irreversible:
            raise RepositoryError(
                "an external action is already running or completed; create a corrective task instead"
            )

        cancelled: list[str] = []
        superseded_tasks: list[str] = []
        for row in task_rows:
            row["superseded_at"] = moment
            superseded_tasks.append(row["task_id"])
            if row["status"] in {TASK_QUEUED, TASK_RETRY, TASK_RUNNING, "waiting_user"}:
                row.update({
                    "status": "cancelled",
                    "approval_state": "rejected",
                    "cancel_requested_at": moment,
                    "cancelled_at": moment,
                    "claimed_by": None,
                    "claim_token": None,
                    "lease_expires_at": None,
                    "next_attempt_at": None,
                    "updated_at": moment,
                    "last_error": "superseded when the owner reopened the recommendation",
                })
                cancelled.append(row["task_id"])
                event_type = "task.cancelled_by_reconsider"
            else:
                row["updated_at"] = moment
                event_type = "task.superseded_by_reconsider"
            self.record_task_event(
                row["task_id"], event_type=event_type,
                actor_type="owner", actor_id=actor_account_id,
                data={"decision_cycle": previous_cycle},
            )

        superseded_artifacts: list[str] = []
        for artifact in self._artifacts:
            if (
                artifact.find_id == find_id
                and artifact.superseded_at is None
                and artifact.decision_cycle <= previous_cycle
            ):
                artifact.superseded_at = moment
                superseded_artifacts.append(artifact.artifact_id)

        found.decision_cycle = previous_cycle + 1
        found.status = "proposed"
        found.decided_at = moment
        found.reopened_at = moment
        found.reopen_reason_code = reason_code
        found.reopen_reason_note = reason_note
        event_id = self._append_decision_event(
            found=found,
            event_type=DECISION_REOPENED,
            previous_status="accepted",
            new_status="proposed",
            actor_account_id=actor_account_id,
            source=source,
            created_at=moment,
            reason_code=reason_code,
            reason_note=reason_note,
            data={
                "previous_cycle": previous_cycle,
                "cancelled_task_ids": cancelled,
                "superseded_task_ids": superseded_tasks,
                "superseded_artifact_ids": superseded_artifacts,
            },
        )
        return ReconsiderResult(
            find_id=find_id,
            previous_status="accepted",
            status="proposed",
            previous_cycle=previous_cycle,
            decision_cycle=found.decision_cycle,
            reopened_at=moment,
            event_id=event_id,
            cancelled_task_ids=tuple(cancelled),
            superseded_task_ids=tuple(superseded_tasks),
            superseded_artifact_ids=tuple(superseded_artifacts),
        )

    def decision_events(
        self, find_id: str, *, business_id: str | None = None,
        limit: int = 100,
    ) -> list[DecisionEvent]:
        rows = [
            event for event in self._decision_events
            if event.find_id == find_id
            and (business_id is None or event.business_id == business_id)
        ]
        rows.sort(key=lambda event: event.created_at, reverse=True)
        return rows[: max(0, int(limit))]

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

    def latest_find_created_at(self, business_id: str) -> datetime | None:
        stamps = [f.created_at for f in self._finds.values()
                  if f.business_id == business_id]
        return max(stamps) if stamps else None

    def recent_finds(self, business_id: str, *, limit: int,
                     include_unseen: bool = False) -> list[FindSummary]:
        mine = [
            f for f in self._finds.values()
            if f.business_id == business_id
            and (include_unseen or f.status in OWNER_SEEN_STATUSES)
        ]
        # In-play first, then everything else the owner saw, then by recency. A
        # move that has been running for six weeks is far stronger evidence of
        # "already covered" than a proposal nobody acted on yesterday — and
        # sorting on recency alone silently hides the winners once proposals
        # accumulate. Never-shown finds sort last for the same reason: on the
        # opt-in path they must not spend the window the board needs.
        mine.sort(key=lambda f: (f.status not in JUDGEABLE_STATUSES,
                                 f.status not in OWNER_SEEN_STATUSES,
                                 -f.created_at.timestamp()))
        return [
            FindSummary(find_id=f.find_id, title=f.title, move=f.move,
                        status=f.status,
                        predicted_daily_cents=f.predicted_daily_cents,
                        created_at=f.created_at)
            for f in mine[:limit]
        ]

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
        found = self._finds.get(find_id)
        if found is None:
            raise RepositoryError(
                f"no find {find_id} — an artifact is the deliverable for a "
                "specific promise, so it cannot outlive the find that made it"
            )
        artifact_cycle = max(1, int(decision_cycle))
        if task_id is not None:
            task = self._tasks.get(task_id)
            if task is None:
                raise RepositoryError(f"unknown task {task_id}")
            artifact_cycle = int(task.get("decision_cycle") or 1)
            if (
                task.get("status") != TASK_RUNNING
                or task.get("superseded_at") is not None
                or found.status != "accepted"
                or found.decision_cycle != artifact_cycle
            ):
                raise RepositoryError("Maker task was cancelled or superseded")
        if idempotency_key:
            existing = self.get_artifact_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing.artifact_id

        revision_number = max(1, int(revision))
        if parent_artifact_id is not None:
            parent = next(
                (item for item in self._artifacts
                 if item.artifact_id == parent_artifact_id
                 and item.find_id == find_id),
                None,
            )
            if parent is None:
                raise RepositoryError("base Maker draft is no longer available")
            revision_number = max(revision_number, int(parent.revision) + 1)

        artifact_id = str(uuid.uuid4())
        now = self._now()
        self._artifacts.append(_Artifact(
            artifact_id=artifact_id, find_id=find_id, kind=kind, title=title,
            created_at=now, preview=preview, s3_bucket=s3_bucket,
            s3_key=s3_key, run_id=run_id, task_id=task_id,
            idempotency_key=idempotency_key, body=body, summary=summary,
            owner_action=owner_action, review_state=review_state,
            metadata=dict(metadata or {}), revision=revision_number,
            parent_artifact_id=parent_artifact_id,
            decision_cycle=artifact_cycle,
        ))
        if parent_artifact_id is not None:
            for item in self._artifacts:
                if item.artifact_id == parent_artifact_id and item.superseded_at is None:
                    item.superseded_at = now
                    break
        return artifact_id

    def _stored_artifact(self, artifact: _Artifact) -> StoredArtifact:
        return StoredArtifact(
            artifact_id=artifact.artifact_id, find_id=artifact.find_id,
            kind=artifact.kind, title=artifact.title,
            created_at=artifact.created_at, preview=artifact.preview,
            s3_bucket=artifact.s3_bucket, s3_key=artifact.s3_key,
            task_id=artifact.task_id, idempotency_key=artifact.idempotency_key,
            body=artifact.body, summary=artifact.summary,
            owner_action=artifact.owner_action, review_state=artifact.review_state,
            metadata=dict(artifact.metadata), revision=artifact.revision,
            parent_artifact_id=artifact.parent_artifact_id,
            decision_cycle=artifact.decision_cycle,
            superseded_at=artifact.superseded_at,
        )

    def get_artifacts(self, find_id: str) -> list[StoredArtifact]:
        mine = [a for a in self._artifacts if a.find_id == find_id]
        mine.sort(key=lambda a: a.created_at, reverse=True)
        return [self._stored_artifact(a) for a in mine]

    def get_artifact_by_idempotency_key(
        self, idempotency_key: str,
    ) -> StoredArtifact | None:
        found = next(
            (artifact for artifact in self._artifacts
             if artifact.idempotency_key == idempotency_key),
            None,
        )
        return self._stored_artifact(found) if found is not None else None

    # -- durable tasks ---------------------------------------------------
    def _task_record(self, row: dict[str, Any]) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"], business_id=row["business_id"],
            find_id=row.get("find_id"),
            requested_by_account_id=row.get("requested_by_account_id"),
            agent=row["agent"], task_type=row["task_type"],
            status=row["status"], priority=row["priority"],
            idempotency_key=row["idempotency_key"],
            resource_key=row["resource_key"],
            approval_state=row["approval_state"],
            decision_cycle=int(row.get("decision_cycle") or 1),
            attempt_count=row["attempt_count"],
            dispatch_count=row["dispatch_count"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            approved_at=row.get("approved_at"), started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            cancel_requested_at=row.get("cancel_requested_at"),
            cancelled_at=row.get("cancelled_at"),
            superseded_at=row.get("superseded_at"),
            next_attempt_at=row.get("next_attempt_at"),
            lease_expires_at=row.get("lease_expires_at"),
            claimed_by=row.get("claimed_by"), claim_token=row.get("claim_token"),
            workflow_execution_arn=row.get("workflow_execution_arn"),
            output_artifact_id=row.get("output_artifact_id"),
            last_error=row.get("last_error"),
            input_data=dict(row.get("input_data") or {}),
            output_data=dict(row.get("output_data") or {}),
        )

    def create_or_get_maker_task(
        self, business_id: str, *, find_id: str,
        requested_by_account_id: str | None = None,
        approved_at: datetime | None = None, priority: int = 100,
        input_data: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        found = self._finds.get(find_id)
        if found is None or found.business_id != business_id:
            raise RepositoryError("recommendation is no longer available")
        if found.status != "accepted":
            raise RepositoryError("Maker work requires an approved recommendation")

        cycle = max(1, int(found.decision_cycle))
        key = maker_task_idempotency_key(find_id, decision_cycle=cycle)
        existing_id = self._task_by_idempotency.get(key)
        if existing_id:
            return self._task_record(self._tasks[existing_id])

        now = self._now()
        task_id = str(uuid.uuid4())
        row = {
            "task_id": task_id,
            "business_id": business_id,
            "find_id": find_id,
            "requested_by_account_id": requested_by_account_id,
            "agent": MAKER_AGENT,
            "task_type": MAKER_DRAFT_TASK,
            "status": TASK_QUEUED,
            "priority": int(priority),
            "idempotency_key": key,
            "resource_key": maker_resource_key(
                business_id, find_id, decision_cycle=cycle
            ),
            "approval_state": "approved",
            "approved_at": approved_at or found.decided_at or now,
            "decision_cycle": cycle,
            "attempt_count": 0,
            "dispatch_count": 0,
            "claimed_by": None,
            "claim_token": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
            "workflow_execution_arn": None,
            "output_artifact_id": None,
            "input_data": dict(input_data or {
                "title": found.title,
                "move": found.move,
                "predicted_daily_cents": found.predicted_daily_cents,
                "decision_cycle": cycle,
            }),
            "output_data": {},
            "last_error": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "cancel_requested_at": None,
            "cancelled_at": None,
            "superseded_at": None,
        }
        self._tasks[task_id] = row
        self._task_by_idempotency[key] = task_id
        self.record_task_event(
            task_id, event_type="task.created", actor_type="owner",
            actor_id=requested_by_account_id,
            data={
                "find_id": find_id,
                "task_type": MAKER_DRAFT_TASK,
                "decision_cycle": cycle,
            },
        )
        return replace(self._task_record(row), created=True)

    def get_task(
        self, task_id: str, *, business_id: str | None = None,
    ) -> TaskRecord | None:
        row = self._tasks.get(task_id)
        if row is None or (business_id is not None and row["business_id"] != business_id):
            return None
        return self._task_record(row)

    def prepare_task_dispatch(self, task_id: str) -> TaskRecord | None:
        row = self._tasks.get(task_id)
        if row is None or row["status"] not in {TASK_QUEUED, TASK_RETRY}:
            return None
        now = self._now()
        if row.get("next_attempt_at") and row["next_attempt_at"] > now:
            return None
        row["dispatch_count"] += 1
        row["updated_at"] = now
        self.record_task_event(
            task_id, event_type="task.dispatched", actor_type="system",
            data={"dispatch_count": row["dispatch_count"]},
        )
        return self._task_record(row)

    def record_task_workflow(
        self, task_id: str, *, execution_arn: str,
    ) -> TaskRecord | None:
        row = self._tasks.get(task_id)
        if row is None:
            return None
        row["workflow_execution_arn"] = execution_arn
        row["updated_at"] = self._now()
        self.record_task_event(
            task_id, event_type="workflow.started", actor_type="orchestrator",
            data={"execution_arn": execution_arn},
        )
        return self._task_record(row)

    def claim_task(
        self, task_id: str, *, worker_id: str, lease_seconds: int = 900,
    ) -> TaskRecord | None:
        row = self._tasks.get(task_id)
        if row is None:
            return None
        now = self._now()
        reclaimable = (
            row["status"] in {TASK_QUEUED, TASK_RETRY}
            or (
                row["status"] == TASK_RUNNING
                and row.get("lease_expires_at") is not None
                and row["lease_expires_at"] <= now
            )
        )
        if not reclaimable:
            return None
        claim_token = str(uuid.uuid4())
        row.update({
            "status": TASK_RUNNING,
            "claimed_by": worker_id,
            "claim_token": claim_token,
            "lease_expires_at": now + timedelta(seconds=max(30, int(lease_seconds))),
            "started_at": row.get("started_at") or now,
            "attempt_count": row["attempt_count"] + 1,
            "updated_at": now,
            "next_attempt_at": None,
            "last_error": None,
        })
        self.record_task_event(
            task_id, event_type="task.claimed", actor_type="worker",
            actor_id=worker_id,
            data={"attempt_count": row["attempt_count"], "claim_token": claim_token},
        )
        return self._task_record(row)

    def task_can_continue(
        self, task_id: str, *, claim_token: str,
    ) -> bool:
        row = self._tasks.get(task_id)
        if row is None or row.get("find_id") is None:
            return False
        found = self._finds.get(str(row["find_id"]))
        return bool(
            found is not None
            and row.get("status") == TASK_RUNNING
            and row.get("claim_token") == claim_token
            and row.get("superseded_at") is None
            and row.get("cancel_requested_at") is None
            and found.status == "accepted"
            and found.decision_cycle == int(row.get("decision_cycle") or 1)
        )

    def complete_task(
        self, task_id: str, *, claim_token: str,
        output_artifact_id: str | None = None,
        output_data: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        row = self._tasks.get(task_id)
        if row is None:
            raise RepositoryError(f"unknown task {task_id}")
        if row["status"] == TASK_COMPLETED:
            return self._task_record(row)
        if row.get("claim_token") != claim_token or row["status"] != TASK_RUNNING:
            raise RepositoryError("task claim is no longer valid")
        now = self._now()
        row.update({
            "status": TASK_COMPLETED,
            "output_artifact_id": output_artifact_id,
            "output_data": dict(output_data or {}),
            "completed_at": now,
            "updated_at": now,
            "claimed_by": None,
            "claim_token": None,
            "lease_expires_at": None,
            "last_error": None,
        })
        self.record_task_event(
            task_id, event_type="task.completed", actor_type="worker",
            data={"output_artifact_id": output_artifact_id},
        )
        return self._task_record(row)

    def fail_task(
        self, task_id: str, *, claim_token: str, error: str,
        retryable: bool = True, retry_after_seconds: int = 60,
    ) -> TaskRecord:
        row = self._tasks.get(task_id)
        if row is None:
            raise RepositoryError(f"unknown task {task_id}")
        if row.get("claim_token") != claim_token or row["status"] != TASK_RUNNING:
            raise RepositoryError("task claim is no longer valid")
        now = self._now()
        row.update({
            "status": TASK_RETRY if retryable else TASK_FAILED,
            "last_error": str(error),
            "next_attempt_at": (
                now + timedelta(seconds=max(1, int(retry_after_seconds)))
                if retryable else None
            ),
            "updated_at": now,
            "claimed_by": None,
            "claim_token": None,
            "lease_expires_at": None,
        })
        self.record_task_event(
            task_id,
            event_type="task.retry_scheduled" if retryable else "task.failed",
            actor_type="worker",
            data={"error": str(error), "retryable": retryable},
        )
        return self._task_record(row)

    def list_tasks(
        self, business_id: str, *, statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        allowed = set(statuses or ())
        rows = [
            row for row in self._tasks.values()
            if row["business_id"] == business_id
            and (not allowed or row["status"] in allowed)
        ]
        rows.sort(key=lambda row: (row["priority"], -row["created_at"].timestamp()))
        return [self._task_record(row) for row in rows[: max(0, int(limit))]]

    def list_dispatchable_tasks(
        self, *, limit: int = 100, per_business_limit: int = 3,
    ) -> list[TaskRecord]:
        now = self._now()
        rows = [
            row for row in self._tasks.values()
            if row["status"] in {TASK_QUEUED, TASK_RETRY}
            and row.get("superseded_at") is None
            and row.get("cancel_requested_at") is None
            and (row.get("next_attempt_at") is None or row["next_attempt_at"] <= now)
        ]
        rows.sort(key=lambda row: (row["priority"], row["created_at"]))
        selected: list[dict[str, Any]] = []
        per_business: dict[str, int] = {}
        ceiling = max(1, int(per_business_limit))
        for row in rows:
            business_id = row["business_id"]
            if per_business.get(business_id, 0) >= ceiling:
                continue
            per_business[business_id] = per_business.get(business_id, 0) + 1
            selected.append(row)
            if len(selected) >= max(0, int(limit)):
                break
        return [self._task_record(row) for row in selected]

    def recover_stale_tasks(
        self, *, now: datetime, max_attempts: int = 3,
    ) -> int:
        # Tests may advance a lease by minutes while the deterministic in-memory
        # clock advances one second per write. Bring that clock forward so the
        # immediately following dispatch query observes the recovered row as due.
        elapsed = int((now - self._EPOCH).total_seconds())
        if elapsed > self._clock:
            self._clock = elapsed
        changed = 0
        for task_id, row in self._tasks.items():
            if (
                row["status"] == TASK_RUNNING
                and row.get("superseded_at") is None
                and row.get("cancel_requested_at") is None
                and row.get("lease_expires_at") is not None
                and row["lease_expires_at"] <= now
            ):
                row["status"] = TASK_RETRY if row["attempt_count"] < max_attempts else TASK_FAILED
                row["last_error"] = "worker lease expired"
                row["next_attempt_at"] = now if row["status"] == TASK_RETRY else None
                row["claimed_by"] = None
                row["claim_token"] = None
                row["lease_expires_at"] = None
                row["updated_at"] = now
                self.record_task_event(
                    task_id,
                    event_type="task.lease_expired",
                    actor_type="reconciler",
                    data={"status": row["status"], "attempt_count": row["attempt_count"]},
                )
                changed += 1
        return changed

    def record_task_event(
        self, task_id: str, *, event_type: str, actor_type: str = "system",
        actor_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        row = self._tasks.get(task_id)
        if row is None:
            raise RepositoryError(f"unknown task {task_id}")
        event = TaskEvent(
            event_id=str(uuid.uuid4()), task_id=task_id,
            business_id=row["business_id"], event_type=event_type,
            actor_type=actor_type, actor_id=actor_id,
            data=dict(data or {}), created_at=self._now(),
        )
        self._task_events.append(event)
        return event.event_id

    def task_events(self, task_id: str, *, limit: int = 100) -> list[TaskEvent]:
        events = [event for event in self._task_events if event.task_id == task_id]
        events.sort(key=lambda event: event.created_at, reverse=True)
        return events[: max(0, int(limit))]

    def start_tool_execution(
        self, *, task_id: str, business_id: str, tool_name: str,
        idempotency_key: str, input_data: Mapping[str, Any] | None = None,
    ) -> ToolExecutionRecord:
        if task_id not in self._tasks or self._tasks[task_id]["business_id"] != business_id:
            raise RepositoryError("task is no longer available")
        task = self._tasks[task_id]
        if (
            task.get("superseded_at") is not None
            or task.get("cancel_requested_at") is not None
            or task.get("status") == TASK_CANCELLED
        ):
            raise RepositoryError("task was superseded by a newer owner decision")
        existing_id = self._tool_by_idempotency.get(idempotency_key)
        if existing_id:
            existing = self._tool_executions[existing_id]
            return ToolExecutionRecord(**{**existing.__dict__, "created": False})
        record = ToolExecutionRecord(
            execution_id=str(uuid.uuid4()), task_id=task_id,
            business_id=business_id, tool_name=tool_name, status="running",
            idempotency_key=idempotency_key, started_at=self._now(),
            input_data=dict(input_data or {}), created=True,
        )
        self._tool_executions[record.execution_id] = record
        self._tool_by_idempotency[idempotency_key] = record.execution_id
        self.record_task_event(
            task_id, event_type="tool.started", actor_type="tool",
            actor_id=tool_name, data={"tool_execution_id": record.execution_id},
        )
        return record

    def finish_tool_execution(
        self, execution_id: str, *, status: str,
        output_data: Mapping[str, Any] | None = None,
        external_reference: str | None = None, error: str | None = None,
    ) -> ToolExecutionRecord:
        existing = self._tool_executions.get(execution_id)
        if existing is None:
            raise RepositoryError(f"unknown tool execution {execution_id}")
        finished = ToolExecutionRecord(
            execution_id=existing.execution_id, task_id=existing.task_id,
            business_id=existing.business_id, tool_name=existing.tool_name,
            status=status, idempotency_key=existing.idempotency_key,
            started_at=existing.started_at, finished_at=self._now(),
            external_reference=external_reference, error=error,
            input_data=dict(existing.input_data), output_data=dict(output_data or {}),
        )
        self._tool_executions[execution_id] = finished
        self.record_task_event(
            existing.task_id, event_type=f"tool.{status}", actor_type="tool",
            actor_id=existing.tool_name,
            data={"tool_execution_id": execution_id,
                  "external_reference": external_reference, "error": error},
        )
        return finished

    def tool_executions(
        self, task_id: str, *, limit: int = 100,
    ) -> list[ToolExecutionRecord]:
        rows = [row for row in self._tool_executions.values() if row.task_id == task_id]
        rows.sort(key=lambda row: row.started_at, reverse=True)
        return rows[: max(0, int(limit))]

    def request_maker_revision(
        self, *, business_id: str, find_id: str, account_id: str | None,
        instruction: str, requested_at: datetime | None = None,
    ) -> TaskRecord:
        found = self._finds.get(find_id)
        if found is None or found.business_id != business_id:
            raise RepositoryError("recommendation is no longer available")
        if found.status != "accepted":
            raise RepositoryError("only an approved recommendation can be revised")
        candidates = [
            row for row in self._tasks.values()
            if row["business_id"] == business_id
            and row.get("find_id") == find_id
            and int(row.get("decision_cycle") or 1) == int(found.decision_cycle)
            and row.get("superseded_at") is None
            and row.get("status") == TASK_COMPLETED
            and row.get("output_artifact_id")
        ]
        if not candidates:
            raise RepositoryError("Maker must finish the current draft before revising it")
        candidates.sort(key=lambda row: row["created_at"], reverse=True)
        row = candidates[0]
        text = " ".join(str(instruction or "").split())
        if not text:
            raise RepositoryError("tell Maker what you want changed")
        now = requested_at or self._now()
        prior_input = dict(row.get("input_data") or {})
        revision = max(1, int(prior_input.get("revision") or 1)) + 1
        prior_input.update({
            "revision": revision,
            "revision_instruction": text,
            "base_artifact_id": row.get("output_artifact_id"),
            "revision_requested_at": now.isoformat(),
        })
        row.update({
            "status": TASK_QUEUED,
            # A revision is new model work. Preserve dispatch_count so Step
            # Functions execution names stay unique, but give the new version a
            # fresh retry budget rather than inheriting failures from v1.
            "attempt_count": 0,
            "input_data": prior_input,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "next_attempt_at": None,
            "lease_expires_at": None,
            "claimed_by": None,
            "claim_token": None,
            "workflow_execution_arn": None,
            "last_error": None,
        })
        self.record_task_event(
            row["task_id"],
            event_type="task.revision_requested",
            actor_type="owner",
            actor_id=account_id,
            data={
                "revision": revision,
                "instruction": text,
                "base_artifact_id": row.get("output_artifact_id"),
            },
        )
        return self._task_record(row)

    def record_email_event(
        self, *, provider_event_id: str, message_id: str, event_type: str,
        event_at: datetime, tool_execution_id: str | None = None,
        recipient: str | None = None, link: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> EmailEventRecord | None:
        existing = self._email_events.get(provider_event_id)
        if existing is not None:
            return replace(existing, created=False)
        tool = (
            self._tool_executions.get(str(tool_execution_id))
            if tool_execution_id else None
        )
        if tool is None:
            tool = next(
                (row for row in self._tool_executions.values()
                 if row.tool_name == "ses.send_review_email"
                 and row.external_reference == message_id),
                None,
            )
        if tool is None:
            return None
        record = EmailEventRecord(
            event_id=str(uuid.uuid4()),
            provider_event_id=provider_event_id,
            tool_execution_id=tool.execution_id,
            task_id=tool.task_id,
            business_id=tool.business_id,
            message_id=message_id,
            event_type=str(event_type),
            event_at=event_at,
            recipient=recipient,
            link=link,
            data=dict(data or {}),
            created=True,
        )
        self._email_events[provider_event_id] = record
        self.record_task_event(
            tool.task_id,
            event_type=f"email.{event_type}",
            actor_type="provider",
            actor_id="amazon_ses",
            data={
                "email_event_id": record.event_id,
                "tool_execution_id": tool.execution_id,
                "message_id": message_id,
                "recipient": recipient,
                "link": link,
            },
        )
        return record

    def email_events_for_tool(
        self, tool_execution_id: str, *, limit: int = 100,
    ) -> list[EmailEventRecord]:
        rows = [
            row for row in self._email_events.values()
            if row.tool_execution_id == tool_execution_id
        ]
        rows.sort(key=lambda row: row.event_at)
        return rows[-max(0, int(limit)):] if limit else []

    # -- ledger ----------------------------------------------------------
    def insert_ledger_entry(
        self, business_id: str, *, find_id: str, verdict: str,
        predicted_daily_cents: int, actual_daily_cents: int | None,
        period_start: date, period_end: date, method: str,
        note: str | None = None, run_id: str | None = None,
        measured_at: datetime | None = None,
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
            # An unmeasured row contributes nothing, exactly as SQL `sum` skips
            # NULL. A verified verdict always has a number behind it, so this
            # only matters if something writes one that does not.
            verified_daily_cents=sum(
                e.actual_daily_cents for e in verified
                if e.actual_daily_cents is not None),
            hit_rate=compute_hit_rate(
                len(verified), sum(1 for e in entries if e.verdict == "miss")),
        )
