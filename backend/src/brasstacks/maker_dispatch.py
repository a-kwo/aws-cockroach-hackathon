"""Start the Maker without making an owner wait for the nightly loop.

The decision and Ask handlers both need the same tiny handoff: persist the
owner's choice first, then asynchronously wake the dedicated Maker Lambda.  The
worker re-checks CockroachDB before spending model tokens, so retries are safe
and a missed invocation is recovered by the scheduled reconciliation sweep.
"""

from __future__ import annotations

import json
from typing import Any

MAKER_FUNCTION_VAR = "BRASSTACKS_MAKER_FUNCTION"


def dispatch_maker(
    *,
    invoker: Any | None,
    function_name: str | None,
    business_id: str,
    find_id: str,
) -> str:
    """Asynchronously request a draft for one accepted recommendation.

    Returns an owner-safe receipt string.  The decision remains durable even if
    Lambda invocation is temporarily unavailable; the Maker reconciliation
    schedule will find the accepted, undrafted row later.
    """
    if invoker is None or not str(function_name or "").strip():
        return "not_configured"

    try:
        invoker.invoke(
            FunctionName=str(function_name),
            InvocationType="Event",
            Payload=json.dumps({
                "business_id": business_id,
                "find_id": find_id,
                "source": "owner_decision",
            }).encode("utf-8"),
        )
    except Exception:
        # Do not leak AWS implementation details into the public decision API.
        # The accepted row is still the durable queue and will be reconciled.
        return "start_failed"
    return "started"


__all__ = ["dispatch_maker", "MAKER_FUNCTION_VAR"]
