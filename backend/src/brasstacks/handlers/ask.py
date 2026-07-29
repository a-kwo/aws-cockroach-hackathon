"""The Ask endpoint, as a Lambda behind API Gateway.

The board is static — it is read-only by design and a build-time splice serves
it fine. Ask is the one thing that genuinely cannot be, because it is a live
round trip by definition. That is what makes API Gateway load-bearing here
rather than decorative.

The response carries the tool trail alongside the answer. Same principle as
`find_evidence`: an answer that cannot show the query behind it is a chatbot
answer, and the trail is what the demo puts on screen.
"""

from __future__ import annotations

import json
from typing import Any

from brasstacks.agents.ask import ask_system_prompt, run_ask, trail_lines
from brasstacks.config import Settings
from brasstacks.providers import ModelRefusedError, ProviderError, build_asker
from brasstacks.secrets import hydrate_environment

#: The endpoint proxies a paid model, so the prompt is bounded here as well as
#: by API Gateway throttling. An unbounded question is a bill.
MAX_QUESTION_CHARS = 500

CORS_HEADERS = {
    "Content-Type": "application/json",
    # The board is static and served from a different origin. Without this the
    # Ask box fails in the browser and works in curl, which is the most
    # confusing possible way for it to break.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": dict(CORS_HEADERS),
        "body": json.dumps(body),
    }


def parse_question(event: Any) -> str:
    """Pull the question out of an API Gateway event.

    Raises ValueError for anything the caller can fix, so it becomes a 400
    rather than a 500 that reads as our fault.
    """
    raw = (event or {}).get("body")
    if raw is None:
        raise ValueError("send a JSON body with a 'question' field")

    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"body is not valid JSON ({e})") from None
    else:
        # Some API Gateway configurations hand the body over pre-parsed.
        payload = raw

    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object with a 'question' field")

    question = str(payload.get("question") or "").strip()
    if not question:
        raise ValueError("'question' is required and cannot be blank")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"question is too long ({len(question)} characters, "
            f"limit {MAX_QUESTION_CHARS})"
        )
    return question


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    try:
        question = parse_question(event)
    except ValueError as e:
        return respond(400, {"error": str(e)})

    hydrate_environment()
    settings = Settings.load()

    if not settings.business_id:
        return respond(500, {"error": "no tenant configured"})

    import psycopg

    from brasstacks.repository_pg import PostgresRepository

    try:
        asker = build_asker(settings)
    except Exception as e:
        # Missing MCP credentials is an operator problem, not a caller one.
        return respond(503, {"error": f"Ask is not configured: {e}"})

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        result = run_ask(
            repo=PostgresRepository(conn),
            asker=asker,
            business_id=settings.business_id,
            question=question,
            model_id=settings.anthropic_model_id,
            system=ask_system_prompt(
                cluster_id=settings.cockroach_cluster_id,
                database=settings.cockroach_database,
            ),
        )

    if result.answer is None:
        # A refusal is a final answer, not a transient fault — retrying the
        # same question will not help, so it must not look retryable.
        status = 422 if "Refused" in (result.error or "") else 502
        return respond(status, {"error": result.error, "run_id": result.run_id})

    return respond(200, {
        "answer": result.answer.text,
        # False here means the model answered from its own knowledge instead of
        # her data. The UI needs to be able to say so.
        "queried_the_cluster": result.answer.queried_the_cluster,
        # The receipt: the SQL that produced the answer, in the order it ran.
        "trail": trail_lines(result.answer),
        "run_id": result.run_id,
    })


__all__ = ["handler", "parse_question", "respond", "MAX_QUESTION_CHARS"]
