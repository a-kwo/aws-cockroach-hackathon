"""The event-driven Maker starts immediately and safely drains backlog."""

from datetime import date, datetime, timedelta, timezone

from brasstacks.artifacts import FakeArtifactStore
from brasstacks.handlers.maker import process_maker_queue, queued_finds
from brasstacks.providers import FakeReasoner
from brasstacks.repository import EvidenceRef, InMemoryRepository

TODAY = date(2026, 8, 2)
VECTOR = [1.0] + [0.0] * 1023
DRAFT = {
    "title": "Draft launch plan",
    "body": "Review this draft, fill in the final price, and publish only after approval.",
}


def make_find(repo, business_id, title, *, created_at, status="accepted"):
    observation_id = repo.insert_observation(
        business_id,
        content=f"Evidence for {title}",
        kind="review",
        embedding=VECTOR,
        observed_at=created_at,
    )
    return repo.insert_find_with_evidence(
        business_id,
        title=title,
        rationale="A citable opportunity exists.",
        move="Prepare the owner-ready draft.",
        emoji="↗",
        predicted_daily_cents=1000,
        confidence=.7,
        verify_after=TODAY + timedelta(days=14),
        status=status,
        created_at=created_at,
        evidence=[EvidenceRef(observation_id, .9)],
    )


def test_directly_approved_find_is_prioritised_then_backlog_is_oldest_first():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    old = make_find(
        repo, business_id, "Old accepted move",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    new = make_find(
        repo, business_id, "Just approved",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    queue = queued_finds(repo, business_id, preferred_find_id=new, limit=10)

    assert [find.find_id for find in queue] == [new, old]


def test_process_queue_creates_drafts_for_every_selected_accepted_move():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    first = make_find(
        repo, business_id, "First",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    second = make_find(
        repo, business_id, "Second",
        created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    receipts = process_maker_queue(
        repo=repo,
        reasoner=FakeReasoner([DRAFT, DRAFT]),
        store=FakeArtifactStore(),
        business_id=business_id,
        limit=10,
        model_id="claude-opus-5",
    )

    assert [receipt["find_id"] for receipt in receipts] == [first, second]
    assert all(receipt["status"] == "completed" for receipt in receipts)
    assert repo.get_artifacts(first)
    assert repo.get_artifacts(second)


def test_queue_ignores_passed_proposals_and_already_drafted_work():
    repo = InMemoryRepository()
    business_id = repo.create_business(name="Rosa's", category="restaurant")
    drafted = make_find(
        repo, business_id, "Already drafted",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    repo.insert_artifact(find_id=drafted, kind="review_reply", title="Existing")
    make_find(
        repo, business_id, "Passed",
        status="rejected",
        created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    assert queued_finds(repo, business_id) == []
