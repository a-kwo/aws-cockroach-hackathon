"""Versioned, compact receipts for owner conversations.

The conversation itself stays in CockroachDB.  ``agent_run.note`` carries only
identifiers, counts and the question/answer preview needed by Memory Engine to
prove which memories entered one model call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

TRACE_PREFIX = "ask_trace_v2:"
MAX_QUESTION_CHARS = 500
MAX_ANSWER_PREVIEW_CHARS = 1000


def _preview(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def encode_ask_trace(
    *,
    question: str,
    answer: str | None,
    find_id: str | None,
    recent_message_ids: Sequence[str],
    relevant_message_ids: Sequence[str],
    stored_message_ids: Sequence[str] = (),
    queried_the_cluster: bool,
) -> str:
    payload = {
        # Full conversation text already lives in observation rows. The run
        # receipt carries bounded previews so workflow reads remain compact.
        "question": _preview(question, MAX_QUESTION_CHARS),
        "answer": _preview(answer, MAX_ANSWER_PREVIEW_CHARS),
        "find_id": str(find_id) if find_id else None,
        "recent_message_ids": [str(value) for value in recent_message_ids],
        "relevant_message_ids": [str(value) for value in relevant_message_ids],
        "stored_message_ids": [str(value) for value in stored_message_ids],
        "queried_the_cluster": bool(queried_the_cluster),
    }
    return TRACE_PREFIX + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def parse_ask_trace(note: str | None) -> dict[str, Any] | None:
    for line in (note or "").splitlines():
        if not line.startswith(TRACE_PREFIX):
            continue
        try:
            payload = json.loads(line[len(TRACE_PREFIX):])
            return {
                "question": str(payload["question"]),
                "answer": None if payload.get("answer") is None else str(payload["answer"]),
                "find_id": None if payload.get("find_id") is None else str(payload["find_id"]),
                "recent_message_ids": [str(v) for v in payload.get("recent_message_ids", [])],
                "relevant_message_ids": [str(v) for v in payload.get("relevant_message_ids", [])],
                "stored_message_ids": [str(v) for v in payload.get("stored_message_ids", [])],
                "queried_the_cluster": bool(payload.get("queried_the_cluster")),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


__all__ = ["TRACE_PREFIX", "encode_ask_trace", "parse_ask_trace"]
