"""A find the pipeline declines to show, and what the owner sees instead.

Three things have to be true at once, and they pull against each other.

1. The withheld find has to be **durable**. A gate that drops a candidate on the
   floor leaves nobody able to answer "why was the board empty on the 4th?" —
   not the owner, not the operator, not the audit trail the product is built on.

2. It must **never** reach the Analyst's "already proposed — do not repeat"
   list. It was not proposed to anyone. Tranche 2 made `recent_finds` filter on
   an allowlist of statuses the owner actually reached, exactly so a verdict
   invented later is never-shown by default; this file is where that promise is
   collected for the status that finally exists.

3. The board must **say so**. A night that ran and withheld everything is not
   the same state as a night that has not run, and it is not "all caught up".
   An owner who opens the app to nothing, with no reason, concludes the product
   is broken.

The measured trap this tranche was written against: an earlier design of these
gates withheld 9 of 9 real finds. Withholding is therefore never presented as
success. It is presented as what it is — the agents looked, and did not trust
what they saw enough to put money on it.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from brasstacks.analyst_trace import encode_analyst_trace
from brasstacks.config import REPO_ROOT
from brasstacks.repository import (
    JUDGEABLE_STATUSES,
    OWNER_SEEN_STATUSES,
    WITHHELD_STATUS,
    EvidenceRef,
    InMemoryRepository,
    RepositoryError,
)

TODAY = date(2026, 8, 4)
_EMBEDDING = [1.0] + [0.0] * 1023

REASON = ("only one source, and the capture was taken 52 minutes before the "
          "shop opened")


# ---------------------------------------------------------------------------
# One contract, both implementations — same arrangement as test_repository.py.
# The in-memory fake accepts any status string; only Postgres has the enum.
# ---------------------------------------------------------------------------

@pytest.fixture(params=[
    "memory",
    pytest.param("postgres", marks=pytest.mark.integration),
])
def repo(request):
    if request.param == "memory":
        yield InMemoryRepository()
        return

    import psycopg

    from brasstacks.config import Settings
    from brasstacks.repository_pg import PostgresRepository

    settings = Settings.load()
    with psycopg.connect(settings.cockroach_url, autocommit=False) as conn:
        yield PostgresRepository(conn)
        conn.rollback()


@pytest.fixture
def business(repo):
    return repo.create_business(
        name=f"Yellow Cow Korean BBQ {uuid.uuid4().hex[:8]}",
        category="restaurant", city="Columbus")


def _withhold(repo, business, *, title="Delivery menu is broken",
              reason=REASON, status=WITHHELD_STATUS):
    observation_id = repo.insert_observation(
        business, content="the note behind it", kind="trend",
        embedding=_EMBEDDING,
        observed_at=datetime(2026, 8, 3, 8, 7, tzinfo=timezone.utc))
    return repo.insert_find_with_evidence(
        business, title=title, rationale="r", move="m", emoji="x",
        predicted_daily_cents=2300, confidence=0.5,
        verify_after=TODAY + timedelta(days=14),
        status=status, withheld_reason=reason,
        evidence=[EvidenceRef(observation_id, 0.147, rank=0)])


# --------------------------------------------------------------- persistence

class TestTheReasonIsDurable:
    def test_a_withheld_find_keeps_the_reason_it_was_withheld_for(self, repo, business):
        find_id = _withhold(repo, business)

        context = repo.get_find_context(business, find_id)

        assert context.status == WITHHELD_STATUS
        assert context.withheld_reason == REASON

    def test_a_find_that_reached_the_board_has_no_withheld_reason(self, repo, business):
        # NULL means "nothing withheld this", not "withheld for no reason".
        find_id = _withhold(repo, business, status="proposed", reason=None)

        assert repo.get_find_context(business, find_id).withheld_reason is None

    def test_the_evidence_is_kept_too(self, repo, business):
        # The whole point of storing the loser is that someone can check the
        # gate's judgement against what it was judging.
        find_id = _withhold(repo, business)

        [cited] = repo.get_find_evidence(find_id)

        assert round(cited.similarity, 3) == 0.147


# ------------------------------------------------------- the do-not-repeat list

class TestItIsNeverFedBackAsForbidden:
    """The test that lets this tranche ship.

    `build_prompt` renders every row `recent_finds` returns under "Already
    proposed — do not repeat any of these". A withheld find landing there is a
    permanent ban on ever raising the corrected version, and at three a night
    the twenty-row window fills in a week: one quiet night becomes an
    indefinitely empty board.
    """

    def test_a_withheld_find_does_not_reach_the_prompt(self, repo, business):
        from brasstacks.agents.analyst import RECENT_FINDS_SHOWN, build_prompt

        _withhold(repo, business, title="Open for lunch on weekdays")

        prompt = build_prompt(
            business=repo.get_business(business), facts=[], rules=[],
            retrieved=[], today=TODAY,
            recent_finds=repo.recent_finds(business, limit=RECENT_FINDS_SHOWN))

        assert "Open for lunch on weekdays" not in prompt

    def test_recent_finds_excludes_it_by_default(self, repo, business):
        _withhold(repo, business)

        assert repo.recent_finds(business, limit=20) == []

    def test_the_status_is_outside_the_allowlist(self):
        # Belt and braces: the filter is a membership test against these two
        # tuples, so the tuples themselves are the invariant worth pinning.
        assert WITHHELD_STATUS not in OWNER_SEEN_STATUSES
        assert WITHHELD_STATUS not in JUDGEABLE_STATUSES

    def test_a_caller_that_asks_can_still_see_it(self, repo, business):
        _withhold(repo, business, title="held back")

        rows = repo.recent_finds(business, limit=20, include_unseen=True)

        assert [(row.title, row.seen_by_owner) for row in rows] == [("held back", False)]


class TestTheOwnerCannotDecideOnWhatSheNeverSaw:
    def test_a_withheld_find_cannot_be_accepted(self, repo, business):
        """A stale tab or a crafted request must not put it on the board.

        Accepting it would queue Maker work and add its prediction to the
        forecast — money moving on a recommendation the pipeline itself refused
        to stand behind. The handlers turn RepositoryError into a 409.
        """
        find_id = _withhold(repo, business)

        with pytest.raises(RepositoryError) as raised:
            repo.set_find_status(find_id, status="accepted", business_id=business)

        assert repo.get_find_context(business, find_id).status == WITHHELD_STATUS
        # The message matters, and asserting only on the exception type let the
        # guard be deleted with the suite still green: without it the call falls
        # through to "find already decided as withheld", which raises the same
        # class and leaks a status the owner was never shown. The guard answers
        # exactly as it does for an id that does not exist.
        assert "no longer available" in str(raised.value)
        assert "withheld" not in str(raised.value)


# ------------------------------------------------------------------- migration

class TestBothMigrationsCarryIt:
    """db/schema.sql and the request-time bootstrap, or a deploy outruns it.

    This has cost us twice: once when `find.alternative_explanation` was named
    in every INSERT before the column existed, and once when the ledger's
    actual column was still NOT NULL. The night writes `status` and
    `withheld_reason` in the same INSERT, so a cluster the migration has not
    reached stores no finds at all.
    """

    def _schema_sql(self) -> str:
        return (Path(REPO_ROOT) / "db" / "schema.sql").read_text(encoding="utf-8")

    def _statements(self):
        from brasstacks.decision_schema import DECISION_SCHEMA_STATEMENTS

        return DECISION_SCHEMA_STATEMENTS

    def test_the_status_is_a_value_the_enum_accepts(self):
        # `find.status` is a database ENUM and InMemoryRepository takes any
        # string, so the unit suite would pass while production raised
        # `invalid input value for enum find_status`. Reading the enum out of
        # schema.sql is the only version of this test that catches it — the
        # same gap that let a 500 reach production for fact_source.
        # Anchored on the closing `);` rather than the first `)`, because the
        # comments inside the block contain parentheses of their own.
        block = re.search(
            r"CREATE TYPE IF NOT EXISTS find_status AS ENUM\s*\((.*?)\n\);",
            self._schema_sql(), re.S).group(1)

        assert WITHHELD_STATUS in set(re.findall(r"'([a-z_]+)'", block))

    def test_schema_sql_adds_the_value_to_a_cluster_that_already_exists(self):
        # CREATE TYPE IF NOT EXISTS does nothing to a type that exists, and all
        # three live tenants' find_status predates this value.
        assert (f"ALTER TYPE find_status ADD VALUE IF NOT EXISTS '{WITHHELD_STATUS}'"
                in self._schema_sql())

    def test_the_enum_value_is_added_outside_the_transaction(self):
        # CockroachDB refuses ALTER TYPE ... ADD VALUE inside a multi-statement
        # transaction. schema.sql runs BEGIN ... COMMIT, so this one statement
        # has to sit after the COMMIT or the whole file fails to apply.
        sql = self._schema_sql()

        assert sql.index("ALTER TYPE find_status ADD VALUE") > sql.rindex("\nCOMMIT;")

    def test_schema_sql_adds_the_reason_column(self):
        assert ("ALTER TABLE find ADD COLUMN IF NOT EXISTS withheld_reason"
                in self._schema_sql())

    def test_the_runtime_bootstrap_adds_both(self):
        statements = self._statements()

        assert any("ADD COLUMN IF NOT EXISTS withheld_reason" in s for s in statements)
        assert any("ALTER TYPE find_status ADD VALUE" in s for s in statements)

    def test_the_untested_statement_runs_last(self):
        """ensure_decision_schema has no error isolation.

        One statement that raises takes the whole request path with it, and
        `_READY` is only set once all of them have run. ALTER TYPE ... ADD VALUE
        has never executed against the live cluster, so it goes at the end,
        where every already-proven migration has applied before it.
        """
        assert "ALTER TYPE find_status ADD VALUE" in self._statements()[-1]

    def test_the_reason_column_is_additive(self):
        # Every find written before the gates existed keeps NULL, which reads
        # as "nothing withheld this" — never as an empty reason.
        [added] = [s for s in self._statements()
                   if "ADD COLUMN IF NOT EXISTS withheld_reason" in s]

        assert "NOT NULL" not in added
        assert "DEFAULT" not in added


# -------------------------------------------------------------- operator view

def _snapshot_rows(*, withheld=1, proposed=0, unique_hits=41):
    """The shape `load_workspaces` hands `build_workspace`, minimal."""
    run_id = "aaaaaaaa-0000-0000-0000-000000000001"
    finds = []
    for index in range(withheld):
        finds.append({
            "id": f"1111111{index}-2222-3333-4444-555555555555",
            "run_id": run_id,
            "emoji": "🚚", "title": f"Held back {index}",
            "summary": "", "rationale": "Because.", "move": "Do a thing.",
            "predicted_daily_cents": 2300, "confidence": 0.5,
            "verify_after": "2026-08-18", "status": WITHHELD_STATUS,
            "withheld_reason": REASON,
            "created_at": "2026-08-04T06:02:00+00:00",
            "evidence": [],
        })
    for index in range(proposed):
        finds.append({
            "id": f"2222222{index}-2222-3333-4444-555555555555",
            "run_id": run_id,
            "emoji": "🍱", "title": f"On the board {index}",
            "summary": "", "rationale": "Because.", "move": "Do a thing.",
            "predicted_daily_cents": 4200, "confidence": 0.6,
            "verify_after": "2026-08-18", "status": "proposed",
            "withheld_reason": None,
            "created_at": "2026-08-04T06:02:00+00:00",
            "evidence": [],
        })
    return {
        "business": {"id": "b0000000-0000-0000-0000-000000000000",
                     "name": "Yellow Cow Korean BBQ", "category": "restaurant",
                     "city": "Columbus", "region": "OH",
                     "goal_monthly_cents": 800000, "goal_note": ""},
        "summary": {}, "corpus": {}, "finds": finds, "kinds": [],
        "runs": [{
            "id": run_id, "agent": "analyst", "status": "ok",
            "started_at": "2026-08-04T06:00:00+00:00",
            "finished_at": "2026-08-04T06:02:00+00:00",
            # The real receipt format, produced by the real encoder, so this
            # cannot pass against a note the Analyst would never write.
            "note": "withheld everything\n" + encode_analyst_trace(
                query_hits=[unique_hits], raw_hits=unique_hits,
                unique_hits=unique_hits, cited_hits=0, find_id=None),
        }],
    }


class TestTheOperatorCanSeeWhatWasHeldBack:
    def test_the_live_read_actually_selects_the_column(self):
        """`build_workspace` can only pass through what `load_workspaces` fetched.

        Shaping the key without adding it to the SELECT would give the operator
        a `withheldReason` that is None on every row — a silent, permanent
        "no reason recorded" that looks like the pipeline failed to store one.
        """
        import inspect

        from brasstacks.workflow_snapshot import load_workspaces

        source = inspect.getsource(load_workspaces)
        finds_query = source.split("finds = _rows(", 1)[1].split('""", ids)', 1)[0]

        assert "withheld_reason" in finds_query

    def test_the_workspace_carries_the_reason(self):
        from brasstacks.workflow_snapshot import build_workspace

        workspace = build_workspace(_snapshot_rows())

        [found] = workspace["finds"]
        assert found["status"] == WITHHELD_STATUS
        assert found["withheldReason"] == REASON

    def test_a_withheld_find_is_in_none_of_the_owner_buckets(self):
        from brasstacks.workflow_snapshot import build_workspace

        workspace = build_workspace(_snapshot_rows())

        [found] = workspace["finds"]
        for bucket in ("proposed", "saved", "measuring", "earning", "judged"):
            assert found["id"] not in workspace[bucket], bucket
        assert workspace["withheld"] == [found["id"]]

    def test_the_workspace_reports_the_night_that_held_them_back(self):
        from brasstacks.workflow_snapshot import build_workspace

        workspace = build_workspace(_snapshot_rows(withheld=2, unique_hits=41))
        held = workspace["heldBack"]

        assert held["count"] == 2
        assert held["signalsConsidered"] == 41
        assert held["signalsSource"] == "cluster"
        assert [row["reason"] for row in held["finds"]] == [REASON, REASON]
        assert held["at"] == "2026-08-04T06:02:00+00:00"

    def test_a_night_that_proposed_something_is_not_a_withheld_night(self):
        # The board only earns the "we held everything back" sentence when the
        # same run put nothing in front of the owner. One good find and this is
        # an ordinary night with a footnote.
        from brasstacks.workflow_snapshot import build_workspace

        workspace = build_workspace(_snapshot_rows(withheld=1, proposed=1))

        assert workspace["heldBack"]["count"] == 1
        assert workspace["heldBack"]["everything"] is False

    def test_it_claims_everything_when_the_run_showed_nothing(self):
        from brasstacks.workflow_snapshot import build_workspace

        assert build_workspace(_snapshot_rows())["heldBack"]["everything"] is True

    def test_an_unrecorded_signal_count_is_not_invented(self):
        # No structured trace means we do not know how many rows were read, and
        # "we checked 0 signals" is a worse lie than saying nothing.
        from brasstacks.workflow_snapshot import build_workspace

        rows = _snapshot_rows()
        rows["runs"][0]["note"] = "wrote a find"
        held = build_workspace(rows)["heldBack"]

        assert held["signalsConsidered"] is None
        assert held["signalsSource"] == "unrecorded"

    def test_the_night_is_flagged_as_the_current_state(self):
        from brasstacks.workflow_snapshot import build_workspace

        assert build_workspace(_snapshot_rows())["heldBack"]["isLatestNight"] is True

    def test_an_older_withheld_night_is_not_the_current_state(self):
        """Three weeks ago is not "we held everything back last night".

        The board reads this flag to decide whether to explain an empty feed.
        Without it, one quiet night in July would go on explaining every empty
        morning after it, which is the same class of untruth as claiming work is
        in progress when none is.
        """
        from brasstacks.workflow_snapshot import build_workspace

        rows = _snapshot_rows()
        rows["runs"].insert(0, {
            "id": "aaaaaaaa-0000-0000-0000-000000000002", "agent": "analyst",
            "status": "ok", "started_at": "2026-08-25T06:00:00+00:00",
            "finished_at": "2026-08-25T06:02:00+00:00", "note": "",
        })

        assert build_workspace(rows)["heldBack"]["isLatestNight"] is False

    def test_a_workspace_with_nothing_withheld_says_so_without_a_block(self):
        from brasstacks.workflow_snapshot import build_workspace

        workspace = build_workspace(_snapshot_rows(withheld=0, proposed=1))

        assert workspace["withheld"] == []
        assert workspace["heldBack"] is None


class TestTheTierSurvivesTheNight:
    """`claim_type` is the strongest statement the system makes about a find.

    It was required of the model, computed, demoted where the standards said so,
    fed to the refuter — and then dropped. Nothing downstream could say whether
    a card was a checkable listing_fact or a current_state claim about the
    owner's storefront, which is the difference between "worth a look" and "your
    delivery menu is broken". The same shape as the tranche where Radar
    classified every observation and the write path discarded the answer.
    """

    def test_the_repository_stores_the_tier_a_find_was_published_at(self, repo):
        business = repo.create_business(name="Palsaik", category="restaurant")
        observation = repo.insert_observation(
            business, content="Apple Maps shows Claim This Place",
            kind="trend", embedding=_EMBEDDING,
            observed_at=datetime(2026, 8, 4, tzinfo=timezone.utc))

        find_id = repo.insert_find_with_evidence(
            business, title="Claim the Apple Maps listing", rationale="Because.",
            move="Claim it.", emoji="📍", predicted_daily_cents=0,
            confidence=0.5, verify_after=TODAY, claim_type="listing_fact",
            evidence=[EvidenceRef(observation, 0.147)])

        assert repo.get_find_context(business, find_id).claim_type == "listing_fact"

    def test_a_find_written_before_tiers_existed_reports_none(self, repo):
        business = repo.create_business(name="Palsaik", category="restaurant")
        observation = repo.insert_observation(
            business, content="something", kind="trend",
            embedding=_EMBEDDING,
            observed_at=datetime(2026, 8, 4, tzinfo=timezone.utc))

        find_id = repo.insert_find_with_evidence(
            business, title="Older find", rationale="Because.", move="Do it.",
            emoji="x", predicted_daily_cents=100, confidence=0.5,
            verify_after=TODAY, evidence=[EvidenceRef(observation, 0.5)])

        assert repo.get_find_context(business, find_id).claim_type is None
