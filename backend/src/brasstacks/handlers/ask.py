"""Authenticated Ask API with durable, owner-scoped conversation memory.

``POST /ask`` retrieves a compact slice of relevant CockroachDB conversation
memory, asks Claude over the read-only CockroachDB MCP connector, stores both
sides of the turn, and records model tokens. ``GET /ask`` returns the durable
history for the signed-in owner (optionally scoped to one recommendation).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from brasstacks.agents.ask import ask_system_prompt, run_ask, trail_lines
from brasstacks.ask_trace import encode_ask_trace
from brasstacks.artifact_usage import artifact_use_context
from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.maker_dispatch import MAKER_QUEUE_URL_VAR, dispatch_maker, dispatch_task
from brasstacks.providers import build_asker, build_embedder
from brasstacks.repository import RepositoryError
from brasstacks.secrets import hydrate_environment

MAX_QUESTION_CHARS = 500
RECENT_MESSAGES_LIMIT = 6
RELEVANT_MESSAGES_LIMIT = 3
HISTORY_LIMIT = 30
MESSAGE_PREVIEW_CHARS = 360
CONTEXT_CHAR_BUDGET = 3600
UNDO_PASS_ACTION = "undo_pass"
RECONSIDER_ACTION = "reconsider"
REVISE_DRAFT_ACTION = "revise_draft"

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS), "body": json.dumps(body)}


def _method(event: Any) -> str:
    request = (event or {}).get("requestContext") or {}
    return str(
        (request.get("http") or {}).get("method")
        or (event or {}).get("httpMethod")
        or "POST"
    ).upper()


def _payload(event: Any) -> dict[str, Any]:
    raw = (event or {}).get("body")
    if raw is None:
        raise ValueError("send a JSON body with a 'question' field")
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"body is not valid JSON ({exc})") from None
    else:
        payload = raw
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    return payload


def parse_request(event: Any) -> tuple[str, str | None]:
    payload = _payload(event)
    question = str(payload.get("question") or "").strip()
    if not question:
        raise ValueError("'question' is required and cannot be blank")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"question is too long ({len(question)} characters, limit {MAX_QUESTION_CHARS})"
        )
    find_id = str(payload.get("find_id") or "").strip() or None
    return question, find_id


def parse_action(event: Any) -> tuple[str, str, str | None] | None:
    """Parse a deterministic chat action without invoking the model."""
    payload = _payload(event)
    action = str(payload.get("action") or "").strip().lower()
    if not action:
        return None
    if action not in {UNDO_PASS_ACTION, RECONSIDER_ACTION, REVISE_DRAFT_ACTION}:
        raise ValueError(f"unsupported action {action!r}")
    find_id = str(payload.get("find_id") or "").strip()
    if not find_id:
        raise ValueError("find_id is required for this action")
    question = str(payload.get("question") or "").strip() or None
    if question and len(question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"question is too long ({len(question)} characters, limit {MAX_QUESTION_CHARS})"
        )
    return action, find_id, question


def is_undo_pass_request(question: str) -> bool:
    """Recognise only clear requests to reverse a Pass.

    Vague questions such as "can I undo this?" still receive a normal Ask
    answer. A state change requires an imperative or explicit changed-mind
    phrase so chat never surprises the owner.
    """
    text = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    exact_imperatives = {
        "do it",
        "yes do it",
        "go ahead",
        "approve it",
        "approve this",
    }
    phrases = (
        "undo pass",
        "undo my pass",
        "redo pass",
        "redo my pass",
        "reverse my pass",
        "take back my pass",
        "change my pass to do it",
        "change this to do it",
        "changed my mind do it",
        "i changed my mind do it",
        "i want to do it now",
        "do it after all",
        "approve this after all",
        "go ahead with this after all",
    )
    return text in exact_imperatives or any(phrase in text for phrase in phrases)


def is_reconsider_request(question: str) -> bool:
    """Recognise an explicit request to reopen an already-approved move."""
    text = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    phrases = (
        "reconsider this",
        "reconsider this move",
        "undo do it",
        "undo my do it",
        "put this back in for you",
        "return this to for you",
        "send this back to for you",
        "i approved this by mistake",
        "i approved too quickly",
        "let me decide again",
        "let me reconsider",
        "reopen this recommendation",
    )
    return any(phrase in text for phrase in phrases)


def is_revision_request(question: str) -> bool:
    """Recognise a clear request to revise the current Maker draft.

    Revision changes the approved deliverable, not the owner's Do it decision.
    Keep this detector conservative so ordinary business questions still go to
    Ask rather than unexpectedly starting paid Maker work.
    """
    text = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    explicit = (
        "revise the draft",
        "revise this draft",
        "rewrite the draft",
        "rewrite this draft",
        "update the draft",
        "update this draft",
        "change the draft",
        "change this draft",
        "regenerate the draft",
        "make the draft shorter",
        "make this draft shorter",
        "make the draft longer",
        "make this draft longer",
        "make the draft warmer",
        "make this draft warmer",
        "make the draft friendlier",
        "make this draft friendlier",
        "make the draft more professional",
        "make this draft more professional",
        "make the next steps clearer",
        "simplify the draft",
        "simplify this draft",
        # The owner is already inside the exact recommendation drawer, so
        # these short pronoun-based commands are unambiguous and should not be
        # forced through a generic Q&A turn.
        "make it shorter",
        "make this shorter",
        "shorten it",
        "make it warmer",
        "make this warmer",
        "make it friendlier",
        "make this friendlier",
        "make it more professional",
        "make this more professional",
        "make it clearer",
        "make this clearer",
        "simplify it",
        "simplify this",
    )
    if any(phrase in text for phrase in explicit):
        return True
    # Allow concise commands only when they clearly refer to copy/deliverable.
    has_draft_noun = any(word in text.split() for word in ("draft", "email", "message", "copy", "post"))
    has_revision_verb = any(word in text.split() for word in ("revise", "rewrite", "shorten", "simplify", "change", "update"))
    return has_draft_noun and has_revision_verb


def parse_question(event: Any) -> str:
    """Backward-compatible helper used by existing tests and callers."""
    return parse_request(event)[0]


def _business_for_event(
    event: Any,
    *,
    repo: Any,
    now: datetime,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    token = bearer_token(event)
    if not token:
        return None, respond(401, {"error": "sign in first"})
    account = repo.account_for_session(token_fingerprint(token), now=now)
    if account is None:
        return None, respond(401, {"error": "sign in first"})
    if not account.get("business_id"):
        return None, respond(409, {"error": "finish setting up your business first"})
    return account, None


def _compact(value: Any, limit: int = MESSAGE_PREVIEW_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _unique_messages(recent: list[Any], relevant: list[Any]) -> list[Any]:
    """Keep recent continuity first, then add semantic memories not already shown."""
    output: list[Any] = []
    seen: set[str] = set()
    for message in [*recent, *relevant]:
        key = str(getattr(message, "message_id", "")) or (
            f"{getattr(message, 'role', '')}:{getattr(message, 'content', '')}"
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(message)
    return output


def _latest_artifact_context(repo: Any, find_id: str | None) -> dict[str, Any] | None:
    """Return safe placement metadata for the current Maker revision."""
    if not find_id or not hasattr(repo, "get_artifacts"):
        return None
    try:
        artifacts = list(repo.get_artifacts(find_id))
    except Exception:
        return None
    current = [artifact for artifact in artifacts if getattr(artifact, "superseded_at", None) is None]
    if not current:
        return None
    artifact = max(
        current,
        key=lambda item: (
            int(getattr(item, "revision", 1) or 1),
            str(getattr(item, "created_at", "") or ""),
        ),
    )
    metadata = getattr(artifact, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    artifact_type = str(metadata.get("artifact_type") or getattr(artifact, "kind", "general_draft"))
    return {
        "title": getattr(artifact, "title", "Maker draft"),
        "revision": int(getattr(artifact, "revision", 1) or 1),
        "review_state": getattr(artifact, "review_state", "ready_for_review"),
        "use_context": artifact_use_context(
            artifact_type, stored=metadata.get("use_context") if isinstance(metadata, Mapping) else None
        ),
    }

def build_context_question(
    *,
    question: str,
    find_context: Any | None,
    facts: list[str],
    rules: list[Any],
    recent_messages: list[Any],
    relevant_messages: list[Any],
    business: dict[str, Any] | None = None,
    artifact_context: Mapping[str, Any] | None = None,
) -> str:
    """Create a bounded retrieval packet instead of sending all chat history.

    The complete conversation remains durable in CockroachDB. One turn receives
    only a few semantically relevant owner memories plus a small recent window,
    which is the core token-efficiency pattern: retrieve first, reason second.
    """
    business = business or {}
    lines = [
        "OWNER'S CURRENT QUESTION",
        question,
        "",
        "OWNER-SCOPED CONTEXT RETRIEVED FROM COCKROACHDB",
        (
            f"Business: {business.get('name') or 'this business'}"
            + (f" · {business.get('category')}" if business.get("category") else "")
            + (f" · {business.get('city')}" if business.get("city") else "")
            + (f", {business.get('region')}" if business.get("region") else "")
        ),
    ]
    # These blocks are ordered by usefulness for the current turn. The footer
    # below is reserved before any optional memory is added, so a long history
    # can never truncate the rules that distinguish owner intent from evidence.
    if find_context is not None:
        lines.extend([
            "CURRENT FOR YOU RECOMMENDATION",
            f"- id: {find_context.find_id}",
            f"- title: {_compact(find_context.title, 180)}",
            f"- rationale: {_compact(find_context.rationale, 720)}",
            f"- action: {_compact(find_context.move, 720)}",
            f"- status: {find_context.status}",
            f"- modelled impact: {find_context.predicted_daily_cents} cents per day",
            f"- confidence: {find_context.confidence:.2f}",
            f"- verify after: {find_context.verify_after.isoformat()}",
        ])
    if artifact_context:
        use_context = artifact_context.get("use_context") or {}
        lines.extend([
            "CURRENT MAKER DRAFT",
            f"- title: {_compact(artifact_context.get('title'), 180)}",
            f"- revision: {artifact_context.get('revision') or 1}",
            f"- review state: {artifact_context.get('review_state') or 'ready_for_review'}",
            f"- intended surface: {_compact(use_context.get('surface'), 160)}",
            f"- placement: {_compact(use_context.get('placement'), 240)}",
            f"- audience: {_compact(use_context.get('audience'), 200)}",
            f"- current delivery state: {_compact(use_context.get('draft_state'), 100)}",
            f"- owner gate: {_compact(use_context.get('owner_gate'), 240)}",
        ])
    if rules:
        lines.append("OWNER GUARDRAILS")
        lines.extend(
            f"- {_compact(getattr(rule, 'rule', rule), 260)}" for rule in rules[:8]
        )
    if facts:
        lines.append("DURABLE BUSINESS PROFILE")
        lines.extend(f"- {_compact(fact, 260)}" for fact in facts[:8])
    if relevant_messages:
        lines.append("SEMANTICALLY RELEVANT PRIOR OWNER MEMORY")
        for message in relevant_messages:
            lines.append(f"- Owner: {_compact(getattr(message, 'content', ''), 420)}")
    if recent_messages:
        lines.append("RECENT CONVERSATION")
        for message in recent_messages:
            role = "Owner" if getattr(message, "role", "user") == "user" else "Brass Tacks"
            lines.append(f"- {role}: {_compact(getattr(message, 'content', ''), 420)}")
    if not relevant_messages and not recent_messages:
        lines.append("PRIOR CONVERSATION MEMORY: none yet")
    footer = (
        "\n\nMEMORY USE RULES\n"
        "Use owner conversation as intent, not independent proof of demand or "
        "financial performance. Use it for continuity, goals, preferences, and "
        "constraints. Verify current business, market, evidence, and outcome "
        "claims through the read-only CockroachDB MCP tools. Do not repeat this "
        "context packet unless it directly helps answer the question."
    )
    available = max(0, CONTEXT_CHAR_BUDGET - len(footer))
    body = ""
    for line in lines:
        candidate = line if not body else "\n" + line
        if len(body) + len(candidate) <= available:
            body += candidate
            continue
        remaining = available - len(body)
        if remaining > 1:
            fragment = candidate[: remaining - 1].rstrip()
            if fragment:
                body += fragment + "…"
        break
    return body.rstrip() + footer


def read_chat_history(
    event: Any,
    *,
    repo: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    account, error = _business_for_event(
        event, repo=repo, now=now or datetime.now(timezone.utc)
    )
    if error:
        return error
    params = (event or {}).get("queryStringParameters") or {}
    find_id = str(params.get("find_id") or "").strip() or None
    messages = repo.recent_chat_messages(
        account["business_id"], limit=HISTORY_LIMIT, find_id=find_id
    )
    stored_total = repo.count_chat_messages(account["business_id"])
    return respond(200, {
        "messages": [
            {
                "id": message.message_id,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at.isoformat(),
                "find_id": message.find_id,
            }
            for message in messages
        ],
        "memory": {
            "totalStoredMessages": stored_total,
            "storedMessages": stored_total,
            "ownerScoped": True,
            "analystBridge": True,
        },
    })


def _undo_answer(*, changed: bool, maker: str) -> str:
    if not changed:
        lead = "This recommendation is already approved."
    else:
        lead = "Pass undone. This recommendation is now Do it."
    if maker == "completed":
        return f"{lead} The Maker draft is already ready to review."
    if maker == "already_running":
        return f"{lead} Maker is already preparing the draft."
    if maker == "queued":
        return f"{lead} Maker is queued and will start shortly."
    if maker == "dispatch_failed":
        return (
            f"{lead} The task is safely queued in CockroachDB; the automatic "
            "reconciliation worker will deliver it again."
        )
    if maker == "not_configured":
        return f"{lead} The task was saved, but Maker dispatch is not configured yet."
    if maker == "failed":
        return f"{lead} The Maker task needs attention before it can be retried."
    return f"{lead} Maker will continue from the durable task queue."


def perform_undo_pass(
    *,
    repo: Any,
    business_id: str,
    find_id: str,
    request_text: str,
    timestamp: datetime,
    queue_client: Any | None = None,
    maker_queue_url: str | None = None,
    requested_by_account_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Change a tenant-scoped Pass to Do it and preserve the chat receipt.

    This is a deterministic application action, not a model decision. It uses
    zero reasoning tokens and still stores both sides of the exchange so Ask
    and Analyst remember that the owner's intent changed.
    """
    find_context = repo.get_find_context(business_id, find_id)
    if find_context is None:
        return respond(404, {"error": "recommendation is no longer available"})
    if find_context.status not in {"rejected", "accepted"}:
        return respond(409, {"error": "only a passed recommendation can be undone"})

    try:
        transition = repo.set_find_status(
            find_id,
            status="accepted",
            decided_at=timestamp,
            business_id=business_id,
            actor_account_id=requested_by_account_id,
            source="chat_undo_pass",
        )
    except RepositoryError as exc:
        return respond(409, {"error": str(exc)})

    dispatch = dispatch_maker(
        repo=repo,
        queue_client=queue_client,
        queue_url=maker_queue_url,
        business_id=business_id,
        find_id=find_id,
        requested_by_account_id=requested_by_account_id,
        approved_at=transition.decided_at,
        source="chat_undo_pass",
    )
    maker = dispatch.status
    maker_task = dispatch.as_dict()
    answer = _undo_answer(changed=transition.changed, maker=maker)

    run_id = None
    owner_message_id = None
    assistant_message_id = None
    storage_error = None
    try:
        run_id = repo.start_run(business_id, agent="ask", model_id=model_id)
        owner_message_id = repo.insert_chat_message(
            business_id,
            role="user",
            content=request_text,
            created_at=timestamp,
            embedding=None,
            find_id=find_id,
            run_id=run_id,
        )
        assistant_message_id = repo.insert_chat_message(
            business_id,
            role="assistant",
            content=answer,
            created_at=timestamp,
            embedding=None,
            find_id=find_id,
            run_id=run_id,
        )
        action_receipt = json.dumps({
            "type": UNDO_PASS_ACTION,
            "find_id": find_id,
            "previous_status": transition.previous_status,
            "status": transition.status,
            "previous_decided_at": (
                transition.previous_decided_at.isoformat()
                if transition.previous_decided_at else None
            ),
            "decided_at": transition.decided_at.isoformat(),
            "changed": transition.changed,
            "maker": maker,
            "maker_task": maker_task,
            "model_tokens": 0,
        }, separators=(",", ":"), sort_keys=True)
        note = encode_ask_trace(
            question=request_text,
            answer=answer,
            find_id=find_id,
            recent_message_ids=(),
            relevant_message_ids=(),
            stored_message_ids=(owner_message_id, assistant_message_id),
            queried_the_cluster=False,
        ) + f"\naction> {action_receipt}"
        repo.finish_run(
            run_id,
            status="ok",
            note=note,
            input_tokens=0,
            output_tokens=0,
        )
    except Exception as exc:
        storage_error = f"{type(exc).__name__}: {exc}"
        if run_id:
            try:
                repo.finish_run(
                    run_id,
                    status="failed",
                    error=storage_error,
                    note="Undo Pass succeeded, but its conversation receipt was incomplete",
                    input_tokens=0,
                    output_tokens=0,
                )
            except Exception:
                pass

    try:
        stored_total = repo.count_chat_messages(business_id)
    except Exception:
        stored_total = None

    return respond(200, {
        "answer": answer,
        "action": {
            "type": UNDO_PASS_ACTION,
            "status": "completed",
            "changed": transition.changed,
        },
        "find_id": find_id,
        "decision": "approved",
        "status": transition.status,
        "previous_status": transition.previous_status,
        "decided_at": transition.decided_at.isoformat(),
        "previous_decided_at": (
            transition.previous_decided_at.isoformat()
            if transition.previous_decided_at else None
        ),
        "maker": maker,
        "maker_task": maker_task,
        "queried_the_cluster": False,
        "trail": [],
        "run_id": run_id,
        "messages": {
            "owner": owner_message_id,
            "assistant": assistant_message_id,
        },
        "memory": {
            "stored": assistant_message_id is not None,
            "ownerScoped": True,
            "analystBridge": True,
            "storedThisTurn": int(owner_message_id is not None) + int(assistant_message_id is not None),
            "storedMessages": stored_total,
            "recentMessages": 0,
            "relevantMessages": 0,
            "contextMessages": 0,
            "retrievalLimit": 0,
            "embeddingCalls": 0,
            "embeddingFallback": False,
            "storageError": storage_error,
        },
        "tokens": {"input": 0, "output": 0, "total": 0},
    })


def perform_reconsider(
    *,
    repo: Any,
    business_id: str,
    find_id: str,
    request_text: str,
    timestamp: datetime,
    requested_by_account_id: str | None = None,
    reason_code: str | None = None,
    reason_note: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Reopen an approved recommendation and preserve every prior receipt.

    This is a deterministic zero-token action. The current task/draft becomes
    cancelled or superseded, while the original Do it, review email and tool
    receipts remain immutable history.
    """
    context = repo.get_find_context(business_id, find_id)
    if context is None:
        return respond(404, {"error": "recommendation is no longer available"})
    if context.status not in {"accepted", "proposed"}:
        return respond(409, {"error": "only an approved recommendation can be reconsidered"})
    try:
        reopened = repo.reconsider_find(
            find_id,
            business_id=business_id,
            actor_account_id=requested_by_account_id,
            reason_code=reason_code or "other",
            reason_note=reason_note or request_text,
            source="chat_reconsider",
            reopened_at=timestamp,
        )
    except RepositoryError as exc:
        return respond(409, {"error": str(exc)})

    answer = (
        "Returned to For You. The original Do it, Maker draft, and review-email "
        "receipt remain in history, and a new decision cycle is now open."
        if reopened.changed else
        "This recommendation is already back in For You for a new decision."
    )
    run_id = None
    owner_message_id = None
    assistant_message_id = None
    storage_error = None
    try:
        run_id = repo.start_run(business_id, agent="ask", model_id=model_id)
        owner_message_id = repo.insert_chat_message(
            business_id, role="user", content=request_text,
            created_at=timestamp, embedding=None, find_id=find_id, run_id=run_id,
        )
        assistant_message_id = repo.insert_chat_message(
            business_id, role="assistant", content=answer,
            created_at=timestamp, embedding=None, find_id=find_id, run_id=run_id,
        )
        action_receipt = json.dumps({
            "type": RECONSIDER_ACTION,
            "find_id": find_id,
            "previous_status": reopened.previous_status,
            "status": reopened.status,
            "previous_cycle": reopened.previous_cycle,
            "decision_cycle": reopened.decision_cycle,
            "reopened_at": reopened.reopened_at.isoformat(),
            "changed": reopened.changed,
            "cancelled_task_ids": list(reopened.cancelled_task_ids),
            "superseded_task_ids": list(reopened.superseded_task_ids),
            "superseded_artifact_ids": list(reopened.superseded_artifact_ids),
            "model_tokens": 0,
        }, separators=(",", ":"), sort_keys=True)
        note = encode_ask_trace(
            question=request_text, answer=answer, find_id=find_id,
            recent_message_ids=(), relevant_message_ids=(),
            stored_message_ids=(owner_message_id, assistant_message_id),
            queried_the_cluster=False,
        ) + f"\naction> {action_receipt}"
        repo.finish_run(
            run_id, status="ok", note=note, input_tokens=0, output_tokens=0,
        )
    except Exception as exc:
        storage_error = f"{type(exc).__name__}: {exc}"
        if run_id:
            try:
                repo.finish_run(
                    run_id, status="failed", error=storage_error,
                    note="Reconsider succeeded, but its conversation receipt was incomplete",
                    input_tokens=0, output_tokens=0,
                )
            except Exception:
                pass
    try:
        stored_total = repo.count_chat_messages(business_id)
    except Exception:
        stored_total = None
    return respond(200, {
        "answer": answer,
        "action": {
            "type": RECONSIDER_ACTION,
            "status": "completed",
            "changed": reopened.changed,
        },
        "find_id": find_id,
        "decision": RECONSIDER_ACTION,
        "status": reopened.status,
        "previous_status": reopened.previous_status,
        "reopened_at": reopened.reopened_at.isoformat(),
        "previous_cycle": reopened.previous_cycle,
        "decision_cycle": reopened.decision_cycle,
        "cancelled_task_ids": list(reopened.cancelled_task_ids),
        "superseded_task_ids": list(reopened.superseded_task_ids),
        "superseded_artifact_ids": list(reopened.superseded_artifact_ids),
        "queried_the_cluster": False,
        "trail": [],
        "run_id": run_id,
        "messages": {"owner": owner_message_id, "assistant": assistant_message_id},
        "memory": {
            "stored": assistant_message_id is not None,
            "ownerScoped": True,
            "analystBridge": True,
            "storedThisTurn": int(owner_message_id is not None) + int(assistant_message_id is not None),
            "storedMessages": stored_total,
            "recentMessages": 0,
            "relevantMessages": 0,
            "contextMessages": 0,
            "retrievalLimit": 0,
            "embeddingCalls": 0,
            "embeddingFallback": False,
            "storageError": storage_error,
        },
        "tokens": {"input": 0, "output": 0, "total": 0},
    })


def perform_revision(
    *,
    repo: Any,
    business_id: str,
    find_id: str,
    request_text: str,
    timestamp: datetime,
    queue_client: Any | None = None,
    maker_queue_url: str | None = None,
    requested_by_account_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Queue a new version of the current approved Maker draft.

    This command is deterministic and uses zero reasoning tokens. The previous
    artifact stays immutable while the same durable task is reopened for a new
    revision. Only the Maker generation run consumes model tokens.
    """
    context = repo.get_find_context(business_id, find_id)
    if context is None:
        return respond(404, {"error": "recommendation is no longer available"})
    if context.status != "accepted":
        return respond(409, {"error": "approve this recommendation before revising its draft"})
    try:
        task = repo.request_maker_revision(
            business_id=business_id,
            find_id=find_id,
            account_id=requested_by_account_id,
            instruction=request_text,
            requested_at=timestamp,
        )
    except RepositoryError as exc:
        return respond(409, {"error": str(exc)})

    dispatch = dispatch_task(
        repo=repo,
        queue_client=queue_client,
        queue_url=maker_queue_url,
        task=task,
        source="chat_revision",
    )
    revision = int(task.input_data.get("revision") or 2)
    answer = (
        f"Revision {revision} is queued. Maker will keep the previous draft in "
        "history and replace the current review version when the revision is ready."
    )

    run_id = None
    owner_message_id = None
    assistant_message_id = None
    storage_error = None
    try:
        run_id = repo.start_run(business_id, agent="ask", model_id=model_id)
        owner_message_id = repo.insert_chat_message(
            business_id, role="user", content=request_text,
            created_at=timestamp, embedding=None, find_id=find_id, run_id=run_id,
        )
        assistant_message_id = repo.insert_chat_message(
            business_id, role="assistant", content=answer,
            created_at=timestamp, embedding=None, find_id=find_id, run_id=run_id,
        )
        action_receipt = json.dumps({
            "type": REVISE_DRAFT_ACTION,
            "find_id": find_id,
            "task_id": task.task_id,
            "revision": revision,
            "base_artifact_id": task.input_data.get("base_artifact_id"),
            "instruction": request_text,
            "dispatch": dispatch.as_dict(),
            "model_tokens": 0,
        }, separators=(",", ":"), sort_keys=True)
        note = encode_ask_trace(
            question=request_text, answer=answer, find_id=find_id,
            recent_message_ids=(), relevant_message_ids=(),
            stored_message_ids=(owner_message_id, assistant_message_id),
            queried_the_cluster=False,
        ) + f"\naction> {action_receipt}"
        repo.finish_run(
            run_id, status="ok", note=note, input_tokens=0, output_tokens=0,
        )
    except Exception as exc:
        storage_error = f"{type(exc).__name__}: {exc}"
        if run_id:
            try:
                repo.finish_run(
                    run_id, status="failed", error=storage_error,
                    note="Draft revision queued, but its chat receipt was incomplete",
                    input_tokens=0, output_tokens=0,
                )
            except Exception:
                pass

    try:
        stored_total = repo.count_chat_messages(business_id)
    except Exception:
        stored_total = None
    return respond(202, {
        "answer": answer,
        "action": {
            "type": REVISE_DRAFT_ACTION,
            "status": dispatch.status,
            "revision": revision,
            "task_id": task.task_id,
        },
        "find_id": find_id,
        "maker": dispatch.status,
        "maker_task": dispatch.as_dict(),
        "queried_the_cluster": False,
        "trail": [],
        "run_id": run_id,
        "messages": {"owner": owner_message_id, "assistant": assistant_message_id},
        "memory": {
            "stored": assistant_message_id is not None,
            "ownerScoped": True,
            "analystBridge": True,
            "storedThisTurn": int(owner_message_id is not None) + int(assistant_message_id is not None),
            "storedMessages": stored_total,
            "recentMessages": 0,
            "relevantMessages": 0,
            "contextMessages": 0,
            "retrievalLimit": 0,
            "embeddingCalls": 0,
            "embeddingFallback": False,
            "storageError": storage_error,
        },
        "tokens": {"input": 0, "output": 0, "total": 0},
    })


def revise_draft_action(
    event: Any,
    *,
    repo: Any,
    queue_client: Any | None = None,
    maker_queue_url: str | None = None,
    model_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        parsed = parse_action(event)
    except ValueError as exc:
        return respond(400, {"error": str(exc)})
    if parsed is None:
        return respond(400, {"error": "an action is required"})
    _, find_id, question = parsed
    if not question:
        return respond(400, {"error": "tell Maker what you want changed"})
    timestamp = now or datetime.now(timezone.utc)
    account, error = _business_for_event(event, repo=repo, now=timestamp)
    if error:
        return error
    return perform_revision(
        repo=repo,
        business_id=account["business_id"],
        find_id=find_id,
        request_text=question,
        timestamp=timestamp,
        queue_client=queue_client,
        maker_queue_url=maker_queue_url,
        requested_by_account_id=account.get("account_id"),
        model_id=model_id,
    )


def undo_pass_action(
    event: Any,
    *,
    repo: Any,
    queue_client: Any | None = None,
    maker_queue_url: str | None = None,
    model_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        parsed = parse_action(event)
    except ValueError as exc:
        return respond(400, {"error": str(exc)})
    if parsed is None:
        return respond(400, {"error": "an action is required"})
    _, find_id, question = parsed

    timestamp = now or datetime.now(timezone.utc)
    account, error = _business_for_event(event, repo=repo, now=timestamp)
    if error:
        return error
    return perform_undo_pass(
        repo=repo,
        business_id=account["business_id"],
        find_id=find_id,
        request_text=question or "I changed my mind. Do it.",
        timestamp=timestamp,
        queue_client=queue_client,
        maker_queue_url=maker_queue_url,
        requested_by_account_id=account.get("account_id"),
        model_id=model_id,
    )


def reconsider_action(
    event: Any,
    *,
    repo: Any,
    model_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        parsed = parse_action(event)
    except ValueError as exc:
        return respond(400, {"error": str(exc)})
    if parsed is None:
        return respond(400, {"error": "an action is required"})
    _, find_id, question = parsed
    timestamp = now or datetime.now(timezone.utc)
    account, error = _business_for_event(event, repo=repo, now=timestamp)
    if error:
        return error
    return perform_reconsider(
        repo=repo,
        business_id=account["business_id"],
        find_id=find_id,
        request_text=question or "Return this recommendation to For You.",
        timestamp=timestamp,
        requested_by_account_id=account.get("account_id"),
        reason_code="other",
        reason_note=question,
        model_id=model_id,
    )


def answer_question(
    event: Any,
    *,
    repo: Any,
    asker: Any,
    embedder: Any,
    settings: Any,
    queue_client: Any | None = None,
    maker_queue_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        question, find_id = parse_request(event)
    except ValueError as exc:
        return respond(400, {"error": str(exc)})

    timestamp = now or datetime.now(timezone.utc)
    account, error = _business_for_event(event, repo=repo, now=timestamp)
    if error:
        return error
    business_id = account["business_id"]

    find_context = None
    artifact_context = None
    if find_id:
        find_context = repo.get_find_context(business_id, find_id)
        if find_context is None:
            return respond(404, {"error": "recommendation is no longer available"})
        artifact_context = _latest_artifact_context(repo, find_id)

    # Chat can carry an explicit changed-mind instruction. The deterministic
    # action is handled before embedding or reasoning, so it cannot hallucinate
    # a state change and it consumes zero model tokens.
    if (
        find_context is not None
        and find_context.status == "rejected"
        and is_undo_pass_request(question)
    ):
        return perform_undo_pass(
            repo=repo,
            business_id=business_id,
            find_id=find_id,
            request_text=question,
            timestamp=timestamp,
            queue_client=queue_client,
            maker_queue_url=maker_queue_url,
            requested_by_account_id=account.get("account_id"),
            model_id=getattr(settings, "anthropic_model_id", None),
        )

    if (
        find_context is not None
        and find_context.status == "accepted"
        and is_reconsider_request(question)
    ):
        return perform_reconsider(
            repo=repo,
            business_id=business_id,
            find_id=find_id,
            request_text=question,
            timestamp=timestamp,
            requested_by_account_id=account.get("account_id"),
            reason_code="other",
            reason_note=question,
            model_id=getattr(settings, "anthropic_model_id", None),
        )

    if (
        find_context is not None
        and find_context.status == "accepted"
        and is_revision_request(question)
    ):
        return perform_revision(
            repo=repo,
            business_id=business_id,
            find_id=find_id,
            request_text=question,
            timestamp=timestamp,
            queue_client=queue_client,
            maker_queue_url=maker_queue_url,
            requested_by_account_id=account.get("account_id"),
            model_id=getattr(settings, "anthropic_model_id", None),
        )

    # Prefer semantic retrieval. A temporary embedding failure must not turn the
    # chat box into a dead control: recent durable memory plus live MCP queries
    # still produce a useful answer, and the new turn is stored without a vector.
    question_embedding = None
    relevant: list[Any] = []
    embedding_fallback = False
    embedding_calls = 0
    try:
        [question_embedding] = embedder.embed([question])
        embedding_calls = 1
        relevant = repo.search_chat_messages(
            business_id, question_embedding, limit=RELEVANT_MESSAGES_LIMIT
        )
    except Exception:
        embedding_fallback = True

    # Continuity belongs to the owner, not only to the recommendation drawer
    # currently open. The visible thread can stay find-specific, while the
    # reasoning context receives the owner's small global recent window plus
    # semantically relevant history from any prior conversation.
    recent = repo.recent_chat_messages(
        business_id, limit=RECENT_MESSAGES_LIMIT, find_id=None
    )
    context_messages = _unique_messages(recent, relevant)
    # Keep the two groups separate in the prompt so operators can explain which
    # memories were semantically retrieved and which exist only for continuity.
    relevant_ids = {message.message_id for message in relevant}
    recent_only = [message for message in recent if message.message_id not in relevant_ids]
    business = repo.get_business(business_id) if hasattr(repo, "get_business") else None
    facts = repo.get_business_facts(business_id)
    rules = repo.get_owner_rules(business_id)
    provider_question = build_context_question(
        question=question,
        business=business,
        facts=facts,
        rules=rules,
        find_context=find_context,
        recent_messages=recent_only,
        relevant_messages=relevant,
        artifact_context=artifact_context,
    )

    run_id = repo.start_run(
        business_id,
        agent="ask",
        model_id=getattr(settings, "anthropic_model_id", None),
    )
    try:
        owner_message_id = repo.insert_chat_message(
            business_id,
            role="user",
            content=question,
            created_at=timestamp,
            embedding=question_embedding,
            find_id=find_id,
            run_id=run_id,
        )
        result = run_ask(
            repo=repo,
            asker=asker,
            business_id=business_id,
            question=provider_question,
            owner_question=question,
            model_id=getattr(settings, "anthropic_model_id", None),
            system=ask_system_prompt(
                cluster_id=getattr(settings, "cockroach_cluster_id", None),
                database=getattr(settings, "cockroach_database", "defaultdb"),
                business_id=business_id,
                business_name=(business or {}).get("name"),
            ),
            find_id=find_id,
            recent_message_ids=[m.message_id for m in recent_only],
            relevant_message_ids=[m.message_id for m in relevant],
            stored_message_ids=[owner_message_id],
            run_id=run_id,
        )
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        try:
            repo.finish_run(
                run_id,
                status="failed",
                error=error_text,
                note="Ask turn failed before a complete answer was returned",
            )
        except Exception:
            pass
        return respond(502, {"error": f"Ask could not answer: {error_text}"})

    if result.answer is None:
        status = 422 if "Refused" in (result.error or "") else 502
        stored_total = repo.count_chat_messages(business_id)
        return respond(status, {
            "error": result.error,
            "run_id": result.run_id,
            "memory": {
                "stored": True,
                "storedThisTurn": 1,
                "storedMessages": stored_total,
                "ownerScoped": True,
                "analystBridge": True,
            },
        })

    assistant_message_id = None
    storage_error = None
    try:
        assistant_message_id = repo.insert_chat_message(
            business_id,
            role="assistant",
            content=result.answer.text,
            created_at=datetime.now(timezone.utc),
            embedding=None,
            find_id=find_id,
            run_id=result.run_id,
        )
    except Exception as exc:
        # The owner should still receive a completed model answer if the second
        # memory write fails. The response makes the partial receipt explicit so
        # the UI never claims both sides were persisted when they were not.
        storage_error = f"{type(exc).__name__}: {exc}"
    stored_total = repo.count_chat_messages(business_id)
    token_total = (
        int(result.input_tokens or 0) + int(result.output_tokens or 0)
        if result.input_tokens is not None or result.output_tokens is not None
        else None
    )
    return respond(200, {
        "answer": result.answer.text,
        "queried_the_cluster": result.answer.queried_the_cluster,
        "trail": trail_lines(result.answer),
        "run_id": result.run_id,
        "messages": {"owner": owner_message_id, "assistant": assistant_message_id},
        "memory": {
            "stored": assistant_message_id is not None,
            "ownerScoped": True,
            "analystBridge": True,
            "storedThisTurn": 2 if assistant_message_id is not None else 1,
            "storedMessages": stored_total,
            "recentMessages": len(recent),
            "relevantMessages": len(relevant),
            "contextMessages": len(context_messages),
            "retrievalLimit": RECENT_MESSAGES_LIMIT + RELEVANT_MESSAGES_LIMIT,
            "embeddingCalls": embedding_calls,
            "embeddingFallback": embedding_fallback,
            "storageError": storage_error,
        },
        "tokens": {
            "input": result.input_tokens,
            "output": result.output_tokens,
            "total": token_total,
        },
    })


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    method = _method(event)
    if method == "OPTIONS":
        return respond(204, {})

    hydrate_environment()
    settings = Settings.load()

    import boto3
    import psycopg
    from brasstacks.repository_pg import PostgresRepository

    try:
        with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
            repo = PostgresRepository(conn)
            if method == "GET":
                return read_chat_history(event, repo=repo)
            if method != "POST":
                return respond(405, {"error": "method not allowed"})
            queue_client = boto3.client("sqs", region_name=settings.aws_region)
            maker_queue_url = os.environ.get(MAKER_QUEUE_URL_VAR)
            try:
                action = parse_action(event)
            except ValueError as exc:
                return respond(400, {"error": str(exc)})
            if action is not None:
                if action[0] == RECONSIDER_ACTION:
                    return reconsider_action(
                        event,
                        repo=repo,
                        model_id=getattr(settings, "anthropic_model_id", None),
                    )
                if action[0] == REVISE_DRAFT_ACTION:
                    return revise_draft_action(
                        event,
                        repo=repo,
                        queue_client=queue_client,
                        maker_queue_url=maker_queue_url,
                        model_id=getattr(settings, "anthropic_model_id", None),
                    )
                return undo_pass_action(
                    event,
                    repo=repo,
                    queue_client=queue_client,
                    maker_queue_url=maker_queue_url,
                    model_id=getattr(settings, "anthropic_model_id", None),
                )
            return answer_question(
                event,
                repo=repo,
                asker=build_asker(settings),
                embedder=build_embedder(settings),
                settings=settings,
                queue_client=queue_client,
                maker_queue_url=maker_queue_url,
            )
    except psycopg.Error:
        return respond(503, {"error": "conversation memory could not be reached"})
    except Exception as exc:
        return respond(503, {"error": f"Ask is not configured: {exc}"})


__all__ = [
    "handler",
    "answer_question",
    "read_chat_history",
    "parse_request",
    "parse_action",
    "parse_question",
    "respond",
    "build_context_question",
    "undo_pass_action",
    "reconsider_action",
    "revise_draft_action",
    "perform_undo_pass",
    "perform_reconsider",
    "perform_revision",
    "is_undo_pass_request",
    "is_reconsider_request",
    "is_revision_request",
    "UNDO_PASS_ACTION",
    "RECONSIDER_ACTION",
    "REVISE_DRAFT_ACTION",
    "MAX_QUESTION_CHARS",
]
