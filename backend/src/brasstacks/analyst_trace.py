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
    source_capped: int = 0,
    floor_removed: int = 0,
    floor_threshold: float | None = None,
    low_confidence: bool = False,
    claims_withheld: int = 0,
    claims_demoted: int = 0,
    refutation: str | None = None,
    refuted_withheld: int = 0,
    refuted_demoted: int = 0,
    refuted_unpriced: int = 0,
) -> str:
    """Encode one compact, versioned Analyst run receipt.

    `query_hits` counts what each vector search returned before the union is
    deduplicated.  `unique_hits` is the number of observations actually placed
    in the model prompt, and `cited_hits` is the smaller set persisted on the
    resulting find.  `source_capped` is how many deduplicated rows were dropped
    because one source had already contributed its limit — a row the model
    never saw has to be visible as removed rather than quietly absent.

    `refutation` is whether the adversarial pass actually ran: "ok" when a model
    read this deck and returned verdicts, "unavailable" when the call failed or
    came back unreadable, and absent when no adversarial call was made at all —
    either nothing is wired for this deployment, or every move had already been
    withheld before one could be challenged. The three states must stay
    distinguishable: a night where nobody could fault a find and a night where
    nobody looked publish the same deck, and only the receipt says which.

    `floor_removed` is the same promise for the similarity bar, and
    `floor_threshold` is where that bar actually sat for this tenant tonight —
    it moves with the tenant's own top similarity, so a number that is not
    recorded cannot be reconstructed afterwards.  `low_confidence` means the
    backstop had to put rows back to fill the prompt: the night ran on thinner
    retrieval than the bar wanted, which is a fact about the night rather than a
    reason to show the owner nothing.
    """
    hits = [_non_negative(value, "query_hits") for value in query_hits]
    raw = _non_negative(raw_hits, "raw_hits")
    unique = _non_negative(unique_hits, "unique_hits")
    cited = _non_negative(cited_hits, "cited_hits")
    capped = _non_negative(source_capped, "source_capped")
    floored = _non_negative(floor_removed, "floor_removed")
    if raw != sum(hits):
        raise ValueError("raw_hits must equal the sum of query_hits")
    if unique > raw:
        raise ValueError("unique_hits cannot exceed raw_hits")
    if cited > unique:
        raise ValueError("cited_hits cannot exceed unique_hits")
    if unique + capped + floored > raw:
        raise ValueError(
            "unique_hits plus source_capped plus floor_removed cannot exceed "
            "raw_hits"
        )

    payload: dict[str, Any] = {
        "query_hits": hits,
        "raw_hits": raw,
        "unique_hits": unique,
        "cited_hits": cited,
        "source_capped": capped,
        "floor_removed": floored,
        "low_confidence": bool(low_confidence),
        # How many of the night's moves the claim standards moved down a tier
        # or refused outright.  A board that is one card short has to be
        # explainable from the run row alone.
        "claims_withheld": _non_negative(claims_withheld, "claims_withheld"),
        "claims_demoted": _non_negative(claims_demoted, "claims_demoted"),
        "find_id": str(find_id) if find_id else None,
    }
    if refutation is not None:
        # Only written when a refuter was wired. An absent key means "no
        # adversary in this deployment"; a zero count would mean "one looked and
        # faulted nothing", which is a much stronger statement than we have.
        payload["refutation"] = str(refutation)
        payload["refuted_withheld"] = _non_negative(
            refuted_withheld, "refuted_withheld")
        payload["refuted_demoted"] = _non_negative(
            refuted_demoted, "refuted_demoted")
        # Every move that reached the board with its dollar figure cleared,
        # whatever cleared it. A demotion is the usual cause; a find the model
        # skipped is the other, and it leaves an unpriced card that
        # `refuted_demoted` alone reports as zero. An operator asking "why is
        # there no money on this one?" has to be able to answer it from here.
        payload["refuted_unpriced"] = _non_negative(
            refuted_unpriced, "refuted_unpriced")
    if floor_threshold is not None:
        payload["floor_threshold"] = round(float(floor_threshold), 4)
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
            # Runs recorded before the per-source cap existed have no such key,
            # and for them zero is the truth rather than a default.
            capped = int(payload.get("source_capped") or 0)
            # Runs recorded before the similarity bar existed have no such key
            # either, and for them zero removed and full confidence is the
            # truth: no bar ran, so no row was withheld by one.
            floored = int(payload.get("floor_removed") or 0)
            if any(value < 0 for value in (*hits, raw, unique, cited, capped,
                                           floored)):
                return None
            if raw != sum(hits) or unique > raw or cited > unique:
                return None
            if unique + capped + floored > raw:
                return None
            result: dict[str, Any] = {
                "query_hits": hits,
                "raw_hits": raw,
                "unique_hits": unique,
                "cited_hits": cited,
                "source_capped": capped,
                "floor_removed": floored,
                "low_confidence": bool(payload.get("low_confidence")),
                "claims_withheld": max(0, int(payload.get("claims_withheld") or 0)),
                "claims_demoted": max(0, int(payload.get("claims_demoted") or 0)),
                "find_id": payload.get("find_id"),
            }
            if payload.get("refutation") is not None:
                result["refutation"] = str(payload["refutation"])
                result["refuted_withheld"] = max(
                    0, int(payload.get("refuted_withheld") or 0))
                result["refuted_demoted"] = max(
                    0, int(payload.get("refuted_demoted") or 0))
                result["refuted_unpriced"] = max(
                    0, int(payload.get("refuted_unpriced") or 0))
            if payload.get("floor_threshold") is not None:
                result["floor_threshold"] = float(payload["floor_threshold"])
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
