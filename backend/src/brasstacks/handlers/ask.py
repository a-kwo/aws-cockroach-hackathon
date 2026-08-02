"""Authenticated Ask API with durable, owner-scoped conversation memory.

``POST /ask`` retrieves a compact slice of relevant CockroachDB conversation
memory, asks Claude over the read-only CockroachDB MCP connector, stores both
sides of the turn, and records model tokens. ``GET /ask`` returns the durable
history for the signed-in owner (optionally scoped to one recommendation).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from brasstacks.agents.ask import ask_system_prompt, run_ask, trail_lines
from brasstacks.auth import token_fingerprint
from brasstacks.config import Settings
from brasstacks.handlers.login import bearer_token
from brasstacks.providers import build_asker, build_embedder
from brasstacks.secrets import hydrate_environment

MAX_QUESTION_CHARS = 500
RECENT_MESSAGES_LIMIT = 6
RELEVANT_MESSAGES_LIMIT = 3
HISTORY_LIMIT = 30
MESSAGE_PREVIEW_CHARS = 360
CONTEXT_CHAR_BUDGET = 3600

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


def parse_request(event: Any) -> tuple[str, str | None]:
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
        raise ValueError("body must be a JSON object with a 'question' field")
    question = str(payload.get("question") or "").strip()
    if not question:
        raise ValueError("'question' is required and cannot be blank")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"question is too long ({len(question)} characters, limit {MAX_QUESTION_CHARS})"
        )
    find_id = str(payload.get("find_id") or "").strip() or None
    return question, find_id


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


def build_context_question(
    *,
    question: str,
    find_context: Any | None,
    facts: list[str],
    rules: list[Any],
    recent_messages: list[Any],
    relevant_messages: list[Any],
    business: dict[str, Any] | None = None,
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


def answer_question(
    event: Any,
    *,
    repo: Any,
    asker: Any,
    embedder: Any,
    settings: Any,
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
    if find_id:
        find_context = repo.get_find_context(business_id, find_id)
        if find_context is None:
            return respond(404, {"error": "recommendation is no longer available"})

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

    import psycopg
    from brasstacks.repository_pg import PostgresRepository

    try:
        with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
            repo = PostgresRepository(conn)
            if method == "GET":
                return read_chat_history(event, repo=repo)
            if method != "POST":
                return respond(405, {"error": "method not allowed"})
            return answer_question(
                event,
                repo=repo,
                asker=build_asker(settings),
                embedder=build_embedder(settings),
                settings=settings,
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
    "parse_question",
    "respond",
    "build_context_question",
    "MAX_QUESTION_CHARS",
]
