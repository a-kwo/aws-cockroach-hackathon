"""The Meter — the agent that holds the others to account.

This is the agent that cannot exist without persistent memory. It reads
predictions made on earlier nights by agent runs that are long gone, measures
what actually happened, and writes a verdict that stays on the record.

A stateless advisor can give advice forever and never be wrong, because nothing
it said was written down. The Meter is what makes being wrong possible.

It never invents an outcome. If no measurement is available the verdict is an
ESTIMATE, not a win — see ``brasstacks.outcomes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from brasstacks.agent_runs import closing_run
from brasstacks.meter import Verdict, judge
from brasstacks.outcomes import OutcomeSource
from brasstacks.repository import Repository, RepositoryError


@dataclass(frozen=True)
class MeterResult:
    run_id: str
    judged: int
    verified: int
    misses: int
    estimated: int
    failed: int
    #: How many of the above were estimates replaced by a figure the owner
    #: reported afterwards. Counted separately because a night that re-scored
    #: yesterday's placeholder on real numbers is the most interesting thing
    #: the Meter does, and it looks identical to a quiet night otherwise.
    remeasured: int = 0

    @property
    def note(self) -> str:
        if not self.judged and not self.failed:
            return "nothing due"
        parts = [f"{self.judged} judged"]
        if self.verified:
            parts.append(f"{self.verified} verified")
        if self.misses:
            parts.append(f"{self.misses} miss")
        if self.estimated:
            parts.append(f"{self.estimated} estimated")
        if self.remeasured:
            parts.append(f"{self.remeasured} re-scored on owner figures")
        if self.failed:
            parts.append(f"{self.failed} could not be measured")
        return "; ".join(parts)


def run_meter(
    *,
    repo: Repository,
    outcomes: OutcomeSource,
    business_id: str,
    today: date,
) -> MeterResult:
    run_id = repo.start_run(business_id, agent="meter")
    with closing_run(repo, run_id):
        return _meter_pass(run_id=run_id, repo=repo, outcomes=outcomes,
                           business_id=business_id, today=today)


def _meter_pass(
    *,
    run_id: str,
    repo: Repository,
    outcomes: OutcomeSource,
    business_id: str,
    today: date,
) -> MeterResult:
    due = repo.due_finds(business_id, today=today)
    verified = misses = estimated = failed = remeasured = 0

    for find in due:
        try:
            outcome = outcomes.measure(find, business_id=business_id)
        except Exception:
            # One unmeasurable find must not cost us the verdicts on the others.
            # It stays due and will be picked up on a later run.
            failed += 1
            continue

        verdict = judge(
            predicted_daily_cents=find.predicted_daily_cents,
            actual_daily_cents=outcome.actual_daily_cents,
            has_outcome_data=outcome.has_outcome_data,
        )

        # The judge already ignores a figure a source supplied while admitting
        # it has no data; the write has to ignore it too, or the ledger stores
        # a number its own verdict says does not exist. A measured zero is a
        # different fact and survives — that is what a published miss is.
        measured = outcome.actual_daily_cents if outcome.has_outcome_data else None

        try:
            repo.insert_ledger_entry(
                business_id,
                find_id=find.find_id,
                verdict=verdict.value,
                # Snapshot the prediction onto the verdict so the ledger stays
                # honest even if the find is edited later. A miss must remain a
                # miss against the number that was actually predicted.
                predicted_daily_cents=find.predicted_daily_cents,
                actual_daily_cents=measured,
                period_start=find.measurement_start or find.created_at.date(),
                period_end=find.verify_after,
                method=outcome.method,
                note=outcome.note,
                run_id=run_id,
            )
        except RepositoryError:
            # Already judged for this window — another run got there first.
            failed += 1
            continue

        verified += verdict is Verdict.VERIFIED
        misses += verdict is Verdict.MISS
        estimated += verdict is Verdict.ESTIMATED

    # Second pass: estimates the owner has since put a real number against.
    #
    # These are not in `due_finds` — they already carry a verdict, and the
    # ledger is append-only per window, so they cannot be judged again by
    # inserting. They are the common case rather than an edge one: a find comes
    # due on a night when nothing was connected, gets the honest placeholder,
    # and the owner has their sales figures a week later. Without this pass that
    # figure could never reach the ledger at all.
    for find in repo.remeasurable_finds(business_id):
        try:
            outcome = outcomes.measure(find, business_id=business_id)
        except Exception:
            failed += 1
            continue

        # Nothing to replace an estimate with. Leave the row exactly as it is
        # rather than restamping it, which would only hide the fact that this
        # find is still waiting for a number.
        if not outcome.has_outcome_data:
            continue

        verdict = judge(
            predicted_daily_cents=find.predicted_daily_cents,
            actual_daily_cents=outcome.actual_daily_cents,
            has_outcome_data=True,
        )

        try:
            repo.update_ledger_entry(
                business_id,
                find_id=find.find_id,
                # The window already on the row. Recomputing it would write a
                # second verdict for one find instead of replacing the first.
                period_start=find.measurement_start or find.created_at.date(),
                period_end=find.verify_after,
                verdict=verdict.value,
                actual_daily_cents=outcome.actual_daily_cents,
                method=outcome.method,
                note=outcome.note,
                run_id=run_id,
            )
        except RepositoryError:
            # The verdict stopped being an estimate between the two statements —
            # another run got there first. Not an error; there is simply nothing
            # left to replace.
            continue

        remeasured += 1
        verified += verdict is Verdict.VERIFIED
        misses += verdict is Verdict.MISS

    result = MeterResult(
        run_id=run_id,
        judged=verified + misses + estimated,
        verified=verified,
        misses=misses,
        estimated=estimated,
        failed=failed,
        remeasured=remeasured,
    )
    # Partial measurement is a normal night, not a failed one.
    repo.finish_run(run_id, status="ok", note=result.note)
    return result
