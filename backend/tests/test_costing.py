"""The costing pass — a second model call that prices what a move will cost.

Until now `predicted_daily_cents` was the only money in the system, and it is
**gross revenue**. The product could tell an owner a move was worth +$23/day
while it burned $400 of ingredients and six hours of labour, and nothing in the
pipeline noticed. That is the hole this closes.

Three things make it a separate agent rather than four more fields on
`FIND_SCHEMA`, and every one of them is a test below:

* **It never sees the revenue figure.** A model shown "this is worth 2300c/day"
  and asked what it costs will produce a number that makes the move look worth
  it. Separation buys nothing on its own — the Refuter's docstring already says
  the framing is the mechanism — so the independence is enforced in
  `build_costing_prompt` and asserted here, not requested in the prompt.
* **It fails to UNKNOWN, never to zero.** The Refuter can fail open by
  publishing a find without its price. A coster that failed to zero would print
  "costs nothing" on every card during an outage, which is a lie that makes
  every move look free. Absent and free are different facts.
* **A cost is a prediction too.** Nothing measures it yet, so nothing may treat
  it as measured. The estimate carries `sourced` per line and stays modelled
  until an owner confirms real spend.

And the reason the two halves are kept apart until the last moment: payback is
`setup / (daily revenue - daily cost)`, and it is computed by
`payback_days()` in ordinary Python from both agents' outputs. No model is ever
asked for it. That matters more than it looks — a "+$23/day" find with $900 of
setup takes 40 days to break even, and `verify_after_days` defaults to 14, so
the Meter would stamp it VERIFIED two weeks before it had paid for itself.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from brasstacks.agents.coster import (
    COSTER_SYSTEM_PROMPT,
    COSTING_SCHEMA,
    COST_KINDS,
    NOT_COSTED_REASON,
    CostEstimate,
    CostLine,
    CostingResult,
    build_costing_prompt,
    cost_finds,
    payback_days,
)
from brasstacks.finds import ParsedFind
from brasstacks.providers import FakeReasoner, ReasoningError
from brasstacks.repository import Retrieved

CAPTURE = datetime(2026, 8, 2, 15, 40, tzinfo=timezone.utc)

BENTO_ROW = (
    "Asaka Japanese Restaurant — Lunch Bento Box $18.95. Photo of the bento "
    "box with salmon teriyaki, rice and salad. 4.5 stars, 212 reviews."
)


def row(observation_id, content, *, kind="trend", subject=None,
        similarity=0.3, rank=0, at=CAPTURE, url=None):
    return Retrieved(
        observation_id=observation_id, content=content, kind=kind,
        similarity=similarity, rank=rank, observed_at=at,
        source_name="web", source_url=url, subject=subject,
    )


def find(*, title="Open for weekday lunch",
         summary="Three rivals serve midday and this block does not.",
         move="Hire one extra midday server. Print a lunch menu.",
         rationale="Reviewers ask for midday service on weekdays.",
         cents=2300, cites=("obs-1",), claim_type="opportunity"):
    return ParsedFind(
        emoji="🍱", title=title, summary=summary, rationale=rationale,
        move=move, predicted_daily_cents=cents, confidence=0.5,
        verify_after=date(2026, 8, 20),
        evidence_observation_ids=tuple(cites), claim_type=claim_type,
    )


def line(label="Extra midday server", cents=18000, kind="daily", cites=()):
    return {"label": label, "cents": cents, "kind": kind,
            "cited_observation_ids": list(cites)}


def estimate(index, *lines, basis="Wages at the local rate."):
    return {"index": index, "basis": basis, "lines": list(lines)}


def reply(*estimates):
    return {"estimates": list(estimates)}


# ---------------------------------------------------------------------------
# Independence — the reason this is a separate agent at all
# ---------------------------------------------------------------------------

class TestTheCosterCannotSeeWhatTheMoveIsWorth:
    def test_the_prompt_never_carries_the_predicted_revenue(self):
        # The whole argument for a second agent. If this string reaches the
        # prompt, the costing is anchored on the Analyst's number and the
        # second call is an expensive way to agree with the first one.
        prompt = build_costing_prompt(
            finds=[find(cents=2300)],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert "2300" not in prompt
        assert "23.00" not in prompt

    def test_changing_the_revenue_figure_changes_nothing_in_the_prompt(self):
        # Stronger than grepping for the number, and it is the guarantee that
        # actually matters: the prompt is a pure function of everything EXCEPT
        # what the move is worth. A substring check passes by luck when the
        # figure is 7 and every timestamp contains a 7; byte-identity cannot.
        rows = {"obs-1": row("obs-1", BENTO_ROW)}

        prompts = {
            build_costing_prompt(finds=[find(cents=cents)], retrieved=rows)
            for cents in (0, 7, 2300, 125_000, 9_999_999)
        }

        assert len(prompts) == 1

    def test_it_is_blind_to_confidence_and_claim_tier(self):
        # Both are the Analyst's own assessment of its own find. A coster that
        # reads "confidence 0.9, verified_fact" is being told how much to
        # believe the thing it is supposed to price independently.
        prompt = build_costing_prompt(
            finds=[find(claim_type="verified_fact")],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert "confidence" not in prompt.lower()
        assert "verified_fact" not in prompt


class TestTheCosterSeesWhatItNeedsToPrice:
    def test_it_carries_the_move_and_the_title(self):
        prompt = build_costing_prompt(
            finds=[find()], retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert "Open for weekday lunch" in prompt
        assert "Hire one extra midday server." in prompt

    def test_it_carries_the_rows_the_find_cites(self):
        # A price in the evidence is the only honest source for a price in the
        # estimate. Untruncated, for the same reason the Refuter's rows are.
        prompt = build_costing_prompt(
            finds=[find()], retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert BENTO_ROW in prompt
        assert "obs-1" in prompt

    def test_it_says_so_when_a_find_cites_nothing_it_can_show(self):
        prompt = build_costing_prompt(finds=[find()], retrieved={})

        assert "no rows" in prompt.lower()


# ---------------------------------------------------------------------------
# The money math — integer cents, and the unhappy paths
# ---------------------------------------------------------------------------

class TestSetupAndRecurringCostsStaySeparate:
    def test_a_one_time_cost_does_not_become_a_daily_one(self):
        # The distinction the whole feature rests on. Revenue in this system is
        # per-day; costs are not. Collapsing a $900 menu reprint into a daily
        # figure would make a one-off look like a permanent drain, and folding
        # a daily wage into setup would make an ongoing cost look survivable.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0,
                line("Menu reprint", 90000, "setup"),
                line("Extra midday server", 18000, "daily"),
            ))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert found.setup_cost_cents == 90000
        assert found.recurring_daily_cost_cents == 18000

    def test_several_lines_of_one_kind_add_up(self):
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0,
                line("Menu reprint", 90000, "setup"),
                line("Photographer", 25000, "setup"),
                line("Server", 18000, "daily"),
                line("Extra prep", 4000, "daily"),
            ))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert found.setup_cost_cents == 115000
        assert found.recurring_daily_cost_cents == 22000

    def test_totals_are_integers_not_floats(self):
        # Money is integer cents everywhere in this system. A float total is how
        # a ledger starts disagreeing with itself three decimal places down.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0, line("Server", 18000, "daily")))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert isinstance(found.setup_cost_cents, int)
        assert isinstance(found.recurring_daily_cost_cents, int)
        assert not isinstance(found.recurring_daily_cost_cents, bool)


class TestALineTheModelGotWrongIsDroppedNotTrusted:
    def test_a_negative_cost_is_refused(self):
        # A negative cost is a revenue claim wearing a cost's clothes, and it
        # would flatter the move by reducing its total. The Analyst is the only
        # agent allowed to say a move earns anything.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0,
                line("Server", 18000, "daily"),
                line("Savings on waste", -5000, "daily"),
            ))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert found.recurring_daily_cost_cents == 18000
        assert [item.label for item in found.lines] == ["Server"]

    def test_a_fractional_cost_is_refused_rather_than_rounded(self):
        # Rounding would be a quiet decision about somebody's money made by a
        # parser. If the model could not answer in whole cents the line is not
        # trustworthy enough to print.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0,
                line("Server", 18000.5, "daily"),
                line("Menu reprint", 90000, "setup"),
            ))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert found.recurring_daily_cost_cents == 0
        assert found.setup_cost_cents == 90000

    def test_a_boolean_is_not_a_number(self):
        # `True` is an int in Python and would price a line at one cent.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0, line("Server", True, "daily")))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.for_index(0).recurring_daily_cost_cents == 0

    def test_an_unrecognised_kind_is_dropped(self):
        # No `enum` in the schema — the structured-output endpoint has rejected
        # keywords on FIND_SCHEMA before, and a 400 here costs every card its
        # costing. The vocabulary is stated in the prompt and enforced on read.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0,
                line("Server", 18000, "daily"),
                line("Vibes", 5000, "monthly"),
            ))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert [item.label for item in found.lines] == ["Server"]
        assert set(COST_KINDS) == {"setup", "daily"}

    def test_an_estimate_naming_a_find_nobody_proposed_is_ignored(self):
        result = cost_finds(
            reasoner=FakeReasoner([reply(
                estimate(0, line("Server", 18000, "daily")),
                estimate(7, line("Nonsense", 999, "daily")),
            )]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert len(result.estimates) == 1
        assert result.for_index(0).recurring_daily_cost_cents == 18000

    def test_the_first_estimate_for_an_index_wins(self):
        # Same rule as the Refuter's verdicts. A model that answers twice has
        # contradicted itself, and a trailing answer must not quietly overwrite
        # a considered one.
        result = cost_finds(
            reasoner=FakeReasoner([reply(
                estimate(0, line("Server", 18000, "daily")),
                estimate(0, line("Server", 90000, "daily")),
            )]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.for_index(0).recurring_daily_cost_cents == 18000


# ---------------------------------------------------------------------------
# Failing to unknown, never to zero
# ---------------------------------------------------------------------------

class TestAnOutageSaysNothingRatherThanFree:
    def test_a_provider_error_produces_no_estimate(self):
        result = cost_finds(
            reasoner=FakeReasoner([ReasoningError("upstream is down")]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert result.checked is False
        assert found.checked is False
        assert found.has_estimate is False
        assert found.basis == NOT_COSTED_REASON

    def test_an_outage_is_not_a_free_move(self):
        # The asymmetry that separates this from the Refuter. Zero here would
        # print "costs nothing" on every card in the deck during an outage.
        result = cost_finds(
            reasoner=FakeReasoner([ReasoningError("upstream is down")]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert found.has_estimate is False
        assert found.lines == ()

    def test_a_body_that_is_not_a_list_of_estimates_fails_open(self):
        result = cost_finds(
            reasoner=FakeReasoner([{"estimates": "not a list"}]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.checked is False
        assert result.for_index(0).has_estimate is False

    def test_a_find_the_model_skipped_is_unknown_not_free(self):
        result = cost_finds(
            reasoner=FakeReasoner([reply(
                estimate(0, line("Server", 18000, "daily")))]),
            finds=[find(), find(title="Second move")],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.for_index(0).has_estimate is True
        assert result.for_index(1).has_estimate is False
        assert result.for_index(1).checked is False

    def test_an_index_nobody_asked_about_is_unknown(self):
        # Never raises and never returns None, for the same reason
        # RefutationResult.for_index does not: the caller reaching past the end
        # is exactly when the board must not go dark.
        result = CostingResult()

        assert result.for_index(3).has_estimate is False

    def test_an_empty_deck_costs_nothing_and_calls_nobody(self):
        reasoner = FakeReasoner([])

        result = cost_finds(reasoner=reasoner, finds=[], retrieved={})

        assert result.checked is True
        assert reasoner.calls == []

    def test_an_estimate_with_no_usable_line_is_not_an_estimate(self):
        # Every line was refused. That is not "this move is free", it is "the
        # coster produced nothing we can stand behind".
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0, line("Vibes", 5000, "monthly")))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.for_index(0).has_estimate is False


# ---------------------------------------------------------------------------
# Sourcing — a cost is a prediction, and says where it came from
# ---------------------------------------------------------------------------

class TestALineSaysWhetherEvidenceBacksIt:
    def test_a_line_citing_a_cited_row_is_sourced(self):
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0, line("Bento ingredients", 1895, "daily", cites=("obs-1",))))]),
            finds=[find(cites=("obs-1",))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.for_index(0).lines[0].sourced is True

    def test_a_line_citing_nothing_is_kept_but_marked_unsourced(self):
        # Kept, deliberately. Most real costs — wages, ingredients, a printer —
        # are not in a restaurant's review corpus, and dropping every unsourced
        # line would leave the owner with an estimate that omits the wages. The
        # honest move is to show it and say it is a judgement, not a citation.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0, line("Extra midday server", 18000, "daily")))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        found = result.for_index(0)
        assert found.lines[0].sourced is False
        assert found.recurring_daily_cost_cents == 18000

    def test_a_line_citing_a_row_this_find_never_cited_is_not_sourced(self):
        # Same discipline as the Refuter's `contradicted_by`: an id the agent
        # was not shown under this find cannot be what it read.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0, line("Server", 18000, "daily", cites=("obs-99",))))]),
            finds=[find(cites=("obs-1",))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.for_index(0).lines[0].sourced is False

    def test_the_estimate_is_never_marked_measured(self):
        # Nothing measures a cost yet. Until an owner confirms real spend this
        # is modelled, and the flag exists so no caller can forget.
        result = cost_finds(
            reasoner=FakeReasoner([reply(estimate(
                0, line("Server", 18000, "daily")))]),
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
        )

        assert result.for_index(0).measured is False


# ---------------------------------------------------------------------------
# Payback — computed in Python from both agents, never asked of a model
# ---------------------------------------------------------------------------

class TestPaybackDays:
    def test_it_divides_setup_by_the_daily_net(self):
        assert payback_days(setup_cost_cents=90000,
                            daily_revenue_cents=2300,
                            daily_cost_cents=0) == 40

    def test_a_partial_day_rounds_up(self):
        # 39.1 days is not paid back on day 39. Rounding down would let the
        # ledger call a move square before it was.
        assert payback_days(setup_cost_cents=90000,
                            daily_revenue_cents=2301,
                            daily_cost_cents=0) == 40

    def test_the_daily_cost_comes_off_the_daily_revenue(self):
        assert payback_days(setup_cost_cents=90000,
                            daily_revenue_cents=5000,
                            daily_cost_cents=2700) == 40

    def test_no_setup_cost_pays_back_immediately(self):
        assert payback_days(setup_cost_cents=0,
                            daily_revenue_cents=2300,
                            daily_cost_cents=0) == 0

    def test_a_move_that_loses_money_daily_never_pays_back(self):
        # None, not a large number. "Never" is a different fact from "eventually"
        # and the card must be able to say so.
        assert payback_days(setup_cost_cents=90000,
                            daily_revenue_cents=1800,
                            daily_cost_cents=1800) is None
        assert payback_days(setup_cost_cents=90000,
                            daily_revenue_cents=1000,
                            daily_cost_cents=1800) is None

    def test_an_unpriced_find_has_no_payback(self):
        # A demoted or unchecked find carries 0 revenue. Dividing by it would
        # raise; claiming instant payback would be worse.
        assert payback_days(setup_cost_cents=90000,
                            daily_revenue_cents=0,
                            daily_cost_cents=0) is None

    def test_an_unknown_cost_has_no_payback(self):
        assert payback_days(setup_cost_cents=None,
                            daily_revenue_cents=2300,
                            daily_cost_cents=None) is None


class TestTheMeasurementWindowIsNotThePaybackPeriod:
    def test_a_move_can_verify_before_it_breaks_even(self):
        # The sharp edge this whole feature exposed. verify_after_days defaults
        # to 14; a $900 setup against +$23/day takes 40. The Meter would stamp
        # it VERIFIED 26 days before the owner was square.
        found = CostEstimate(
            index=0,
            lines=(CostLine(label="Menu reprint", cents=90000, kind="setup"),),
            basis="A reprint at the local rate.",
        )

        assert found.pays_back_within(
            days=14, daily_revenue_cents=2300) is False

    def test_a_cheap_move_clears_its_window(self):
        found = CostEstimate(
            index=0,
            lines=(CostLine(label="Menu reprint", cents=9000, kind="setup"),),
            basis="A reprint at the local rate.",
        )

        assert found.pays_back_within(
            days=14, daily_revenue_cents=2300) is True

    def test_an_unknown_cost_answers_neither_way(self):
        # None, not False. "We do not know" must not render as "it does not pay
        # back", which is an accusation the system cannot support.
        found = CostEstimate(index=0, lines=(), basis=NOT_COSTED_REASON,
                             checked=False)

        assert found.pays_back_within(
            days=14, daily_revenue_cents=2300) is None


# ---------------------------------------------------------------------------
# The stored shape
# ---------------------------------------------------------------------------

class TestWhatGoesIntoCockroachDB:
    def test_it_is_json_safe(self):
        import json

        found = CostEstimate(
            index=0,
            lines=(CostLine(label="Menu reprint", cents=90000, kind="setup"),
                   CostLine(label="Server", cents=18000, kind="daily",
                            sourced=True)),
            basis="Wages at the local rate.",
        )

        payload = found.as_dict()

        assert json.loads(json.dumps(payload)) == payload

    def test_it_carries_both_totals_so_a_reader_never_re_sums_lines(self):
        found = CostEstimate(
            index=0,
            lines=(CostLine(label="Menu reprint", cents=90000, kind="setup"),
                   CostLine(label="Server", cents=18000, kind="daily")),
            basis="Wages at the local rate.",
        )

        payload = found.as_dict()

        assert payload["setup_cost_cents"] == 90000
        assert payload["recurring_daily_cost_cents"] == 18000
        assert payload["measured"] is False

    def test_an_unknown_estimate_stores_nothing_rather_than_zeroes(self):
        # The column stays NULL. A row of zeroes would be indistinguishable
        # from a genuinely free move once it is in the database.
        found = CostEstimate(index=0, lines=(), basis=NOT_COSTED_REASON,
                             checked=False)

        assert found.as_dict() is None


class TestTheSystemPromptSaysWhatItMustNotDo:
    def test_it_forbids_inventing_a_revenue_figure(self):
        assert "revenue" in COSTER_SYSTEM_PROMPT.lower()

    def test_the_schema_asks_for_lines_not_a_bare_total(self):
        # A bare total cannot be argued with. Lines can be read, disputed and
        # corrected by an owner who knows what a printer charges.
        items = COSTING_SCHEMA["properties"]["estimates"]["items"]
        assert "lines" in items["required"]
        assert "basis" in items["required"]

    def test_the_schema_uses_no_enum_keyword(self):
        # Matching FIND_SCHEMA and REFUTATION_SCHEMA. One unsupported keyword
        # costs the whole call, and this one fails to "no cost shown at all".
        import json

        assert '"enum"' not in json.dumps(COSTING_SCHEMA)
        assert "maxLength" not in json.dumps(COSTING_SCHEMA)


# ---------------------------------------------------------------------------
# Persistence and wiring — the estimate has to survive the process
# ---------------------------------------------------------------------------

from brasstacks.agents.analyst import run_analyst  # noqa: E402
from brasstacks.decision_schema import DECISION_SCHEMA_STATEMENTS  # noqa: E402
from brasstacks.providers import FakeEmbedder  # noqa: E402
from brasstacks.repository import EvidenceRef, InMemoryRepository  # noqa: E402

STORE_TODAY = date(2026, 7, 28)
STORE_NOW = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def business(repo):
    return repo.create_business(name="Rosa's Trattoria", category="restaurant",
                                city="Columbus", goal_monthly_cents=800000)


def find_payload(**overrides):
    base = {
        "emoji": "🍰",
        "title": "Tiramisu → $9",
        "rationale": "Reviews call it the best in the city and rivals charge more.",
        "move": "Reprice tiramisu to $9.",
        "predicted_daily_cents": 2300,
        "confidence": 0.85,
        "verify_after_days": 14,
        "evidence_observation_ids": [],
    }
    base.update(overrides)
    return base


class TestTheEstimateSurvivesTheProcess:
    def test_a_find_round_trips_its_cost_estimate(self, repo, business):
        observation_id = repo.insert_observation(
            business, content="Rivals charge $9 for dessert", kind="review",
            embedding=FakeEmbedder().embed(["x"])[0], observed_at=STORE_NOW,
        )
        stored = {
            "setup_cost_cents": 90000,
            "recurring_daily_cost_cents": 18000,
            "basis": "Wages at the local rate.",
            "measured": False,
            "lines": [{"label": "Menu reprint", "cents": 90000,
                       "kind": "setup", "sourced": False}],
        }

        find_id = repo.insert_find_with_evidence(
            business, title="Add a lunch service", rationale="Customers asked.",
            move="Hire one midday server.", emoji="🍱",
            predicted_daily_cents=2300, confidence=.72, verify_after=STORE_TODAY,
            evidence=[EvidenceRef(observation_id, .8)], cost_estimate=stored,
        )

        assert repo.get_find_context(business, find_id).cost_estimate == stored

    def test_a_find_written_without_one_reads_back_as_none(self, repo, business):
        # NULL, not an empty dict. Every find written before this column existed
        # is unpriced, and unpriced must not render as free.
        observation_id = repo.insert_observation(
            business, content="Rivals charge $9", kind="review",
            embedding=FakeEmbedder().embed(["x"])[0], observed_at=STORE_NOW,
        )

        find_id = repo.insert_find_with_evidence(
            business, title="Add a lunch service", rationale="Customers asked.",
            move="Hire one midday server.", emoji="🍱",
            predicted_daily_cents=2300, confidence=.72, verify_after=STORE_TODAY,
            evidence=[EvidenceRef(observation_id, .8)],
        )

        assert repo.get_find_context(business, find_id).cost_estimate is None

    def test_the_column_is_added_by_the_self_healing_migration(self):
        # Same hazard the feed_brief column had: a Lambda that deploys ahead of
        # the migration would INSERT a column the cluster has never heard of and
        # store nothing at all, which costs the whole night rather than the
        # costing.
        statements = " ".join(DECISION_SCHEMA_STATEMENTS)

        assert "cost_estimate" in statements
        assert "ADD COLUMN IF NOT EXISTS cost_estimate" in statements


class TestTheAnalystRunsTheCoster:
    def _corpus(self, repo, business):
        embedder = FakeEmbedder()
        [vector] = embedder.embed(["rivals charge nine dollars for dessert"])
        observation_id = repo.insert_observation(
            business, content="rivals charge nine dollars for dessert",
            kind="review", embedding=vector, observed_at=STORE_NOW,
        )
        return embedder, observation_id

    def test_a_wired_coster_prices_the_find_it_stores(self, repo, business):
        embedder, observation_id = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(
            evidence_observation_ids=[observation_id])])
        coster = FakeReasoner([reply(estimate(
            0,
            line("Menu reprint", 90000, "setup"),
            line("Extra prep", 1200, "daily"),
        ))])

        result = run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                             business_id=business, today=STORE_TODAY,
                             coster=coster)
        context = repo.get_find_context(business, result.find_id)

        assert context.cost_estimate["setup_cost_cents"] == 90000
        assert context.cost_estimate["recurring_daily_cost_cents"] == 1200
        assert context.cost_estimate["measured"] is False

    def test_no_coster_means_no_cost_rather_than_a_free_move(self, repo, business):
        embedder, observation_id = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(
            evidence_observation_ids=[observation_id])])

        result = run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                             business_id=business, today=STORE_TODAY)

        assert repo.get_find_context(business, result.find_id).cost_estimate is None

    def test_a_costing_outage_does_not_cost_the_night_its_find(self, repo, business):
        # The failure this whole fail-open design exists for. The owner still
        # gets the move; it simply carries no bill.
        embedder, observation_id = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(
            evidence_observation_ids=[observation_id])])
        coster = FakeReasoner([ReasoningError("upstream is down")])

        result = run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                             business_id=business, today=STORE_TODAY,
                             coster=coster)

        assert result.find_id is not None
        assert repo.get_find_context(business, result.find_id).cost_estimate is None

    def test_the_coster_never_sees_the_revenue_the_analyst_predicted(
            self, repo, business):
        # The independence claim, asserted on the real wiring rather than on
        # build_costing_prompt alone. 2300 is what this find claims to be worth.
        embedder, observation_id = self._corpus(repo, business)
        reasoner = FakeReasoner([find_payload(
            predicted_daily_cents=2300,
            evidence_observation_ids=[observation_id])])
        coster = FakeReasoner([reply(estimate(
            0, line("Menu reprint", 90000, "setup")))])

        run_analyst(repo=repo, embedder=embedder, reasoner=reasoner,
                    business_id=business, today=STORE_TODAY, coster=coster)

        [call] = coster.calls
        assert "2300" not in call["user"]
        assert call["system"] == COSTER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# The display contract — what the owner's card is allowed to say
# ---------------------------------------------------------------------------

from brasstacks.agents.coster import cost_for_display  # noqa: E402


class TestCostForDisplay:
    def _stored(self, setup=90000, daily=1200):
        return {
            "setup_cost_cents": setup,
            "recurring_daily_cost_cents": daily,
            "basis": "Wages at the local rate.",
            "measured": False,
            "lines": [{"label": "Menu reprint", "cents": setup,
                       "kind": "setup", "sourced": False}],
        }

    def test_it_reports_no_estimate_for_a_find_nobody_priced(self):
        shown = cost_for_display(None, predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["hasEstimate"] is False
        assert shown["setupCents"] is None
        assert shown["dailyCents"] is None
        assert shown["paybackDays"] is None

    def test_an_unpriced_find_never_renders_a_zero(self):
        # The failure mode this whole design exists to prevent, asserted at the
        # last layer before the markup. A 0 here prints "costs nothing".
        shown = cost_for_display(None, predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert 0 not in (shown["setupCents"], shown["dailyCents"])

    def test_it_carries_both_totals_and_the_basis(self):
        shown = cost_for_display(self._stored(), predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["hasEstimate"] is True
        assert shown["setupCents"] == 90000
        assert shown["dailyCents"] == 1200
        assert shown["basis"] == "Wages at the local rate."

    def test_it_computes_payback_from_both_agents(self):
        # 90000 setup, 2300 revenue less 1200 cost = 1100/day net → 82 days.
        shown = cost_for_display(self._stored(), predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["paybackDays"] == 82

    def test_it_says_when_a_move_verifies_before_it_breaks_even(self):
        shown = cost_for_display(self._stored(), predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["paysBackWithinWindow"] is False

    def test_a_demoted_find_carrying_no_revenue_has_no_payback(self):
        # An unpriced find stores 0 cents. Payback against 0 revenue is not
        # "immediately", and it is not an error either.
        shown = cost_for_display(self._stored(), predicted_daily_cents=0,
                                 verify_after_days=14)

        assert shown["paybackDays"] is None
        assert shown["paysBackWithinWindow"] is None

    def test_it_is_never_marked_measured(self):
        shown = cost_for_display(self._stored(), predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["measured"] is False

    def test_a_stored_measured_flag_of_true_is_not_believed(self):
        # Nothing measures a cost yet. If a row somehow claims otherwise — a
        # hand-edited fixture, a future writer landing early — the display layer
        # is the last place that can refuse to repeat it.
        stored = self._stored()
        stored["measured"] = True

        shown = cost_for_display(stored, predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["measured"] is False

    def test_it_is_json_safe(self):
        import json

        shown = cost_for_display(self._stored(), predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert json.loads(json.dumps(shown)) == shown

    def test_a_malformed_stored_row_degrades_to_no_estimate(self):
        # A JSONB column is not a schema. Anything the reader cannot stand
        # behind shows nothing rather than a partial bill.
        for junk in ("not a dict", [], {"setup_cost_cents": "lots"},
                     {"setup_cost_cents": None,
                      "recurring_daily_cost_cents": None}):
            shown = cost_for_display(junk, predicted_daily_cents=2300,
                                     verify_after_days=14)
            assert shown["hasEstimate"] is False, junk


# ---------------------------------------------------------------------------
# The honesty rules, at the layer that renders them
# ---------------------------------------------------------------------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from brasstacks.workflow_snapshot import build_workspace  # noqa: E402

SAMPLE = Path(__file__).resolve().parent / "data" / "workspace_sample.json"


def _sample_with_cost(cost):
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for raw_find in data.get("finds", []):
        raw_find["cost_estimate"] = cost
    return data


class TestACostNeverTouchesTheVerifiedRecord:
    def test_the_verified_headline_is_identical_with_and_without_costs(self):
        # The rule from CLAUDE.md: only `verdict = 'verified'` money reaches the
        # headline daily figure. A cost estimate is measured by nothing, so it
        # must not be able to move that number in either direction — netting a
        # verified revenue against a modelled cost would launder the second into
        # the first and quietly break the claim the whole product rests on.
        priced = build_workspace(_sample_with_cost({
            "setup_cost_cents": 90000,
            "recurring_daily_cost_cents": 5000,
            "basis": "Wages at the local rate.",
            "measured": False,
            "lines": [],
        }))
        unpriced = build_workspace(_sample_with_cost(None))

        for key in ("verifiedDaily", "verifiedDailyTxt", "dailyCents",
                    "verified", "headline"):
            if key in priced or key in unpriced:
                assert priced.get(key) == unpriced.get(key), key

    def test_every_find_carries_the_cost_contract(self):
        workspace = build_workspace(_sample_with_cost(None))

        for shown in workspace["finds"]:
            assert shown["costEstimate"]["hasEstimate"] is False
            assert shown["costEstimate"]["setupCents"] is None

    def test_a_priced_find_reaches_the_owner_payload(self):
        workspace = build_workspace(_sample_with_cost({
            "setup_cost_cents": 90000,
            "recurring_daily_cost_cents": 5000,
            "basis": "Wages at the local rate.",
            "measured": False,
            "lines": [{"label": "Menu reprint", "cents": 90000,
                       "kind": "setup", "sourced": False}],
        }))

        shown = workspace["finds"][0]["costEstimate"]
        assert shown["hasEstimate"] is True
        assert shown["setupCents"] == 90000
        assert shown["dailyCents"] == 5000
        assert shown["measured"] is False

    def test_the_predicted_revenue_is_unchanged_by_a_costing(self):
        # Cost sits BESIDE revenue, never netted into it. Two figures, two
        # verdicts, because one is measurable today and the other is not.
        priced = build_workspace(_sample_with_cost({
            "setup_cost_cents": 90000,
            "recurring_daily_cost_cents": 5000,
            "basis": "b", "measured": False, "lines": [],
        }))
        unpriced = build_workspace(_sample_with_cost(None))

        assert ([shown["predictedDaily"] for shown in priced["finds"]]
                == [shown["predictedDaily"] for shown in unpriced["finds"]])


class TestTheCardReadsFormattedText:
    """CLAUDE.md: every money value crossing into the page is integer cents,
    formatted once in Python. The card must never divide by 100 in JavaScript.
    """

    def _stored(self, setup=90000, daily=1200):
        return {"setup_cost_cents": setup, "recurring_daily_cost_cents": daily,
                "basis": "Wages at the local rate.", "measured": False,
                "lines": []}

    def test_whole_dollars_lose_the_decimals(self):
        shown = cost_for_display(self._stored(setup=90000, daily=1200),
                                 predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["setupTxt"] == "$900"
        assert shown["dailyTxt"] == "$12"

    def test_part_dollars_keep_both_decimals(self):
        shown = cost_for_display(self._stored(setup=89950, daily=1250),
                                 predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["setupTxt"] == "$899.50"
        assert shown["dailyTxt"] == "$12.50"

    def test_a_move_that_never_pays_back_says_so_in_words(self):
        # Not "0 days", not an empty string. The card has to be able to say the
        # daily cost eats the daily revenue.
        shown = cost_for_display(self._stored(daily=2300),
                                 predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["paybackDays"] is None
        assert shown["paybackTxt"] == "Not at this rate"

    def test_no_setup_cost_pays_back_from_day_one(self):
        shown = cost_for_display(self._stored(setup=0, daily=1200),
                                 predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["paybackTxt"] == "From day one"

    def test_a_payback_period_is_written_in_days(self):
        shown = cost_for_display(self._stored(),
                                 predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["paybackTxt"] == "82 days"

    def test_one_day_is_not_pluralised(self):
        shown = cost_for_display(self._stored(setup=100, daily=0),
                                 predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["paybackTxt"] == "1 day"

    def test_an_unpriced_find_carries_empty_text_not_a_zero(self):
        shown = cost_for_display(None, predicted_daily_cents=2300,
                                 verify_after_days=14)

        assert shown["setupTxt"] == ""
        assert shown["dailyTxt"] == ""
        assert shown["paybackTxt"] == ""
