"""A find the pipeline declines to show must not become a forbidden topic.

`build_prompt` renders every row `recent_finds` returns under "Already proposed
— do not repeat any of these", and SYSTEM_PROMPT tells the model to obey that
list. That is correct while every stored find has been on the owner's board.

It stops being correct the moment a quality gate withholds one. A find held back
tonight for a fixable reason — a capture taken while the shop was shut, a second
source that was never found — comes back tomorrow as an instruction never to
raise it, and the corrected version can never be proposed. RECENT_FINDS_SHOWN is
20 against a retrievable corpus of 34–46 rows, so at three withheld finds a night
the window fills in a week: a board that went quiet for one night stays empty.

The gates do not exist yet. This file exists so they cannot be built on top of
the ratchet.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from brasstacks.agents.analyst import RECENT_FINDS_SHOWN, build_prompt
from brasstacks.repository import (
    JUDGEABLE_STATUSES,
    OWNER_SEEN_STATUSES,
    EvidenceRef,
    InMemoryRepository,
)

TODAY = date(2026, 8, 3)

#: The name the withholding gate will use. Nothing in this repository knows it
#: yet — and that is the point of the test below that uses a different, equally
#: unheard-of string: the rule is an allowlist of statuses the owner reached, so
#: whatever tranche 3 calls its verdict is "never shown" without anyone
#: remembering to come back here.
NEVER_SHOWN = "withheld"

_EMBEDDING = [1.0] + [0.0] * 1023


def _seed(finds):
    repo = InMemoryRepository()
    business = repo.create_business(
        name="Yellow Cow Korean BBQ", category="restaurant", city="Columbus")
    for title, status in finds:
        observation_id = repo.insert_observation(
            business, content=f"the note behind {title}", kind="trend",
            embedding=_EMBEDDING,
            observed_at=datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc))
        repo.insert_find_with_evidence(
            business, title=title, rationale="r", move="m", emoji="x",
            predicted_daily_cents=2300, confidence=0.5,
            verify_after=TODAY + timedelta(days=14), status=status,
            evidence=[EvidenceRef(observation_id, 0.6)])
    return repo, business


def _prompt(repo, business):
    return build_prompt(
        business=repo.get_business(business), facts=[], rules=[], retrieved=[],
        today=TODAY,
        recent_finds=repo.recent_finds(business, limit=RECENT_FINDS_SHOWN),
    )


def test_a_never_shown_find_is_not_fed_back_as_forbidden():
    repo, business = _seed([("Open for lunch on weekdays", NEVER_SHOWN)])

    assert "Open for lunch on weekdays" not in _prompt(repo, business), (
        "a find the owner was never shown reached the do-not-repeat list — "
        "the Analyst can now never raise the corrected version of it"
    )


def test_a_find_the_owner_saw_is_still_forbidden():
    # The other half of the contract. Withholding must not become an excuse to
    # re-propose a move she already passed on.
    repo, business = _seed([("Saturday waitlist", "rejected")])
    prompt = _prompt(repo, business)

    assert "Already proposed" in prompt
    assert "Saturday waitlist" in prompt


def test_a_week_of_withheld_nights_does_not_evict_the_board():
    # The mechanism, at the scale it happens: three a night for a week fills the
    # window and every proposal the owner can actually see falls out of it.
    on_the_board = [(f"on the board {n}", "proposed") for n in range(3)]
    withheld = [(f"held back {n}", NEVER_SHOWN) for n in range(RECENT_FINDS_SHOWN)]
    repo, business = _seed(on_the_board + withheld)

    titles = {f.title for f in repo.recent_finds(business, limit=RECENT_FINDS_SHOWN)}

    assert {"on the board 0", "on the board 1", "on the board 2"} <= titles


def test_a_status_nobody_anticipated_is_treated_as_never_shown():
    # Default-deny. If the rule were a list of withheld statuses instead of a
    # list of seen ones, a verdict nobody remembered to register here would
    # silently become a do-not-repeat instruction.
    repo, business = _seed([("quarantined by a gate not yet written",
                             "some_future_verdict")])

    assert repo.recent_finds(business, limit=RECENT_FINDS_SHOWN) == []


def test_the_caller_can_tell_the_board_from_what_was_held_back():
    # Two different sentences want writing in the prompt, so the difference has
    # to survive as far as the caller rather than only as a filter here.
    repo, business = _seed([("on the board", "proposed"), ("held back", NEVER_SHOWN)])

    rows = repo.recent_finds(business, limit=10, include_unseen=True)

    assert {f.title: f.seen_by_owner for f in rows} == {
        "on the board": True, "held back": False}


def test_what_was_never_shown_sorts_behind_what_was():
    # Even on the opt-in path, a night of withheld finds must not push the
    # owner's live moves out of a bounded window.
    repo, business = _seed([("running for six weeks", "live"),
                            ("held back last night", NEVER_SHOWN)])

    rows = repo.recent_finds(business, limit=1, include_unseen=True)

    assert [f.title for f in rows] == ["running for six weeks"]


def test_anything_the_meter_may_judge_is_something_the_owner_saw():
    # A find cannot be measurable without having been acted on, so this
    # inclusion has to hold or `due_finds` and `recent_finds` disagree about
    # what happened to the same row.
    assert set(JUDGEABLE_STATUSES) <= set(OWNER_SEEN_STATUSES)
