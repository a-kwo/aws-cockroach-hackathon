"""Structured, durable receipts for Analyst retrieval runs.

`agent_run.note` is intentionally the audit trail for lightweight agent-specific
receipts.  The human-readable first line remains useful in logs, while the
prefixed JSON line below gives the operator UI an exact, versioned contract.

The receipt contains counts and identifiers only.  Observation text stays in
CockroachDB and is exposed through the find's existing evidence rows, so adding
traceability does not duplicate the memory corpus or increase model context.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

TRACE_PREFIX = "analyst_trace_v1:"


def _non_negative(value: int, name: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def encode_analyst_trace(
    *,
    query_hits: Sequence[int],
    raw_hits: int,
    unique_hits: int,
    cited_hits: int,
    find_id: str | None,
    queries: Sequence[str] | None = None,
    per_query_limit: int | None = None,
    owner_memory_ids: Sequence[str] = (),
) -> str:
    """Encode one compact, versioned Analyst run receipt.

    `query_hits` counts what each vector search returned before the union is
    deduplicated.  `unique_hits` is the number of observations actually placed
    in the model prompt, and `cited_hits` is the smaller set persisted on the
    resulting find.
    """
    hits = [_non_negative(value, "query_hits") for value in query_hits]
    raw = _non_negative(raw_hits, "raw_hits")
    unique = _non_negative(unique_hits, "unique_hits")
    cited = _non_negative(cited_hits, "cited_hits")
    if raw != sum(hits):
        raise ValueError("raw_hits must equal the sum of query_hits")
    if unique > raw:
        raise ValueError("unique_hits cannot exceed raw_hits")
    if cited > unique:
        raise ValueError("cited_hits cannot exceed unique_hits")

    payload: dict[str, Any] = {
        "query_hits": hits,
        "raw_hits": raw,
        "unique_hits": unique,
        "cited_hits": cited,
        "find_id": str(find_id) if find_id else None,
    }
    if queries is not None:
        payload["queries"] = [str(query) for query in queries]
    if per_query_limit is not None:
        payload["per_query_limit"] = _non_negative(per_query_limit, "per_query_limit")
    if owner_memory_ids:
        payload["owner_memory_ids"] = [str(value) for value in owner_memory_ids]

    return TRACE_PREFIX + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def parse_analyst_trace(note: str | None) -> dict[str, Any] | None:
    """Return the first valid Analyst receipt in *note*, or ``None``.

    Bad or older notes remain readable by the rest of the product.  A malformed
    structured line is ignored rather than making the workflow endpoint fail.
    """
    for line in (note or "").splitlines():
        if not line.startswith(TRACE_PREFIX):
            continue
        try:
            payload = json.loads(line[len(TRACE_PREFIX):])
            hits = [int(value) for value in payload["query_hits"]]
            raw = int(payload["raw_hits"])
            unique = int(payload["unique_hits"])
            cited = int(payload["cited_hits"])
            if any(value < 0 for value in (*hits, raw, unique, cited)):
                return None
            if raw != sum(hits) or unique > raw or cited > unique:
                return None
            result: dict[str, Any] = {
                "query_hits": hits,
                "raw_hits": raw,
                "unique_hits": unique,
                "cited_hits": cited,
                "find_id": payload.get("find_id"),
            }
            if isinstance(payload.get("queries"), list):
                result["queries"] = [str(query) for query in payload["queries"]]
            if payload.get("per_query_limit") is not None:
                result["per_query_limit"] = int(payload["per_query_limit"])
            if isinstance(payload.get("owner_memory_ids"), list):
                result["owner_memory_ids"] = [
                    str(value) for value in payload["owner_memory_ids"]
                ]
            return result
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


__all__ = ["TRACE_PREFIX", "encode_analyst_trace", "parse_analyst_trace"]
