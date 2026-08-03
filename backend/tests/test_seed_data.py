"""The seeded history is demo data, but it is still money on the owner's screen.

`scripts/seed.py` validates before it spends anything on embeddings, and the
rules it checks are the Meter's rules. A seeded row the Meter itself would never
have written is a lie the demo tells on its behalf — which is exactly what the
estimated row did: it carried an actual equal to the prediction, the shape this
whole fix exists to remove.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import seed

HISTORY = Path(__file__).resolve().parents[2] / "db" / "seed" / "history.json"


def history() -> dict:
    return json.loads(HISTORY.read_text(encoding="utf-8"))


def test_no_seeded_estimate_claims_a_measured_number():
    """The shipped seed, checked against its own rule."""
    for find in history()["finds"]:
        ledger = find.get("ledger")
        if ledger and ledger["verdict"] == "estimated":
            assert ledger["actual_daily_cents"] is None, find["id"]


def test_validate_rejects_an_estimate_that_carries_an_actual():
    data = history()
    observations = json.loads(
        (HISTORY.parent / "observations.json").read_text(encoding="utf-8")
    )
    finds = copy.deepcopy(data["finds"])
    estimated = next(f for f in finds if (f.get("ledger") or {}).get("verdict") == "estimated")
    estimated["ledger"]["actual_daily_cents"] = estimated["predicted_daily_cents"]

    with pytest.raises(SystemExit) as raised:
        seed.validate(observations["observations"], finds)

    assert "estimate" in str(raised.value)
