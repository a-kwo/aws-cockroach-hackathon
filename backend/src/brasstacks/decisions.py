"""Immutable owner-decision history and reconsideration policy.

A recommendation's current ``find.status`` is a projection used by the feed.
Every confirmed owner action is also appended to ``decision_event`` so changing
one's mind never erases what happened before.

Reconsideration is intentionally narrower than a generic undo. Drafts and
review emails sent only to the owner can be superseded safely. A tool that has
already changed a customer-facing external system must be corrected through a
new task instead of pretending the original side effect never happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

DECISION_ACCEPTED = "owner.accepted"
DECISION_PASSED = "owner.passed"
DECISION_SAVED_LATER = "owner.saved_later"
DECISION_UNDO_PASS = "owner.undo_pass"
DECISION_REOPENED = "owner.reopened"

DECISION_EVENT_TYPES = frozenset({
    DECISION_ACCEPTED,
    DECISION_PASSED,
    DECISION_SAVED_LATER,
    DECISION_UNDO_PASS,
    DECISION_REOPENED,
})

RECONSIDER_REASON_CODES = frozenset({
    "approved_by_mistake",
    "timing_changed",
    "cost_or_staffing",
    "needs_revision",
    "other",
})

# These side effects remain internal to the owner review workflow. They are
# preserved as receipts but do not prevent the recommendation from returning to
# For You. Future customer-facing publishing/sending tools are deliberately not
# included here.
REVERSIBLE_REVIEW_TOOLS = frozenset({"ses.send_review_email"})


@dataclass(frozen=True)
class DecisionEvent:
    event_id: str
    business_id: str
    find_id: str
    decision_cycle: int
    event_type: str
    previous_status: str | None
    new_status: str
    created_at: datetime
    actor_account_id: str | None = None
    reason_code: str | None = None
    reason_note: str | None = None
    source: str = "owner_decision"
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconsiderResult:
    find_id: str
    previous_status: str
    status: str
    previous_cycle: int
    decision_cycle: int
    reopened_at: datetime
    event_id: str | None = None
    cancelled_task_ids: tuple[str, ...] = ()
    superseded_task_ids: tuple[str, ...] = ()
    superseded_artifact_ids: tuple[str, ...] = ()
    changed: bool = True


__all__ = [
    "DecisionEvent",
    "ReconsiderResult",
    "DECISION_ACCEPTED",
    "DECISION_PASSED",
    "DECISION_SAVED_LATER",
    "DECISION_UNDO_PASS",
    "DECISION_REOPENED",
    "DECISION_EVENT_TYPES",
    "RECONSIDER_REASON_CODES",
    "REVERSIBLE_REVIEW_TOOLS",
]
