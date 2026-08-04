"""The refutation pass — one model call whose only job is to prove a find wrong.

The deterministic gates catch structure: a similarity bar, a per-source cap, a
capture taken before opening. Measured against the nine finds live on
2026-08-03, they do not catch what actually went wrong in this dataset:

* **f26662ea** priced a takeout push off a competitor set nobody observed. The
  Postmates "most ordered" ranking behind it is real; the rivals it compares
  against are not in any cited row.
* **9bc53e94** told Asaka to photograph its lunch bento. Its OWN cited row
  describes a bento photo on the listing.
* **818fdb2d** told Yellow Cow to launch a fixed-price weekday lunch set. Three
  rows of that tenant's memory name the Dosirak set meal as its best-selling
  delivery item.
* **8b4009e5** priced a group package off a 2018 newspaper article.

"chicken fingers, burgers", "a bento photo", "Dosirak" — ordinary nouns. No
extractor flags them, no threshold removes them, and every one of them is
visible to anything that reads the find beside the rows it cites. So a model
reads them, and it is asked to REFUTE rather than to review. The framing is the
mechanism: "review this find" returns balanced prose that publishes everything,
"prove this find wrong, and quote the row that does it" returns a citation or
nothing.

And it fails OPEN. This is the component with the highest failure rate in the
system — one network call, one model, one JSON parse — and the failure this
whole line of work exists to avoid is an owner opening the board to nothing. An
earlier design of the deterministic gates withheld 9 of 9 and would have been a
dead product. A checker outage that blanked the board would be the same dead
product arriving by a different route, so an outage publishes the deterministic
survivors and takes the dollar figure off them.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from brasstacks.agents.refuter import (
    NOT_CHECKED_REASON,
    REFUTATION_SCHEMA,
    REFUTER_ACTIONS,
    REFUTER_SYSTEM_PROMPT,
    Refutation,
    build_refutation_prompt,
    refute_finds,
)
from brasstacks.finds import EvidenceFact, ParsedFind
from brasstacks.providers import FakeReasoner, ReasoningError
from brasstacks.repository import Retrieved

TODAY = date(2026, 8, 4)
#: 08:40 local in California — when Radar actually fetched Yellow Cow's Grubhub
#: storefront on the night find 7c4a9124 was written.
CAPTURE = datetime(2026, 8, 2, 15, 40, tzinfo=timezone.utc)

BENTO_ROW = (
    "Asaka Japanese Restaurant — Lunch Bento Box $18.95. Photo of the bento "
    "box with salmon teriyaki, rice and salad. 4.5 stars, 212 reviews."
)
POSTMATES_ROW = (
    "Palsaik Korean BBQ. Most ordered: #1 Samgyupsal, #2 Galbi, "
    "#3 Soondubu Jjigae. Delivery in 25-40 min."
)


def row(observation_id, content, *, url=None, kind="trend", subject=None,
        similarity=0.3, rank=0, at=CAPTURE):
    return Retrieved(
        observation_id=observation_id, content=content, kind=kind,
        similarity=similarity, rank=rank, observed_at=at,
        source_name="web", source_url=url, subject=subject,
    )


def find(*, title="Photograph the weekday lunch bento",
         summary="No listing carries a picture of the bento.",
         move="Shoot the bento box. Upload it to the listing.",
         rationale="Reviewers name it and no listing carries a picture.",
         cents=2300, cites=("obs-1",), claim_type="opportunity"):
    return ParsedFind(
        emoji="🍱", title=title, summary=summary, rationale=rationale,
        move=move, predicted_daily_cents=cents, confidence=0.5,
        verify_after=date(2026, 8, 20),
        evidence_observation_ids=tuple(cites), claim_type=claim_type,
    )


def verdict(index, action, reason="because", contradicted_by=()):
    return {"index": index, "action": action, "reason": reason,
            "contradicted_by": list(contradicted_by)}


def reply(*verdicts):
    return {"verdicts": list(verdicts)}


# ---------------------------------------------------------------------------
# What the refuter is shown
# ---------------------------------------------------------------------------

class TestThePromptCarriesTheFindAndItsOwnEvidence:
    def test_it_carries_every_word_the_owner_would_read(self):
        prompt = build_refutation_prompt(
            finds=[find()], retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert "Photograph the weekday lunch bento" in prompt
        assert "No listing carries a picture of the bento." in prompt
        assert "Shoot the bento box." in prompt
        assert "Reviewers name it and no listing carries a picture." in prompt

    def test_it_carries_the_cited_row_in_full(self):
        # Untruncated on purpose. The whole job is catching a claim that
        # contradicts its own evidence, and find 9bc53e94's bento photo sits in
        # the middle of a long listing row — a cheaper prompt that cut the row
        # short would be blind to the exact thing it exists to see.
        prompt = build_refutation_prompt(
            finds=[find()], retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert BENTO_ROW in prompt

    def test_it_carries_provenance_and_the_capture_time(self):
        prompt = build_refutation_prompt(
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW,
                                    url="https://www.yelp.com/biz/asaka")})

        assert "yelp.com" in prompt
        assert "2026-08-02T15:40:00Z" in prompt

    def test_it_says_whether_the_shop_was_trading_when_the_page_was_read(self):
        prompt = build_refutation_prompt(
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
            evidence_facts={"obs-1": EvidenceFact(
                observation_id="obs-1", statement_type="page_state",
                captured_while_open=False)},
        )

        assert "shut" in prompt
        assert "page_state" in prompt

    def test_a_durable_row_is_not_labelled_with_an_openness_answer(self):
        # Measured on the live cluster on 2026-08-04: all 51 rows cited across
        # the nine finds answer "captured while the business was shut", because
        # Radar sweeps at 06:00 and 18:00 and restaurants open at 11:00. Printed
        # on every row that is a lever that refutes 9 of 9 — the exact trap the
        # blunt version of the deterministic gate fell into. A review, a price
        # and a menu are as true at 08:00 as at 20:00, so only a row describing
        # a page's momentary state carries the answer.
        prompt = build_refutation_prompt(
            finds=[find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
            evidence_facts={"obs-1": EvidenceFact(
                observation_id="obs-1", statement_type="review",
                captured_while_open=False)},
        )

        assert "shut" not in prompt
        assert "2026-08-02T15:40:00Z" in prompt

    def test_the_system_prompt_says_capture_time_only_bites_on_page_state(self):
        text = REFUTER_SYSTEM_PROMPT.lower()

        assert "page_state" in text
        assert "capture time" in text

    def test_a_row_the_find_did_not_cite_is_not_shown(self):
        # "ONLY the find text and its cited evidence." An uncited row in the
        # prompt is a row the refuter can use to argue *for* the find, which is
        # the one thing this call must not help with.
        prompt = build_refutation_prompt(
            finds=[find(cites=("obs-1",))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW),
                       "obs-2": row("obs-2", POSTMATES_ROW)},
        )

        assert BENTO_ROW in prompt
        assert POSTMATES_ROW not in prompt

    def test_each_find_is_numbered_so_a_verdict_can_name_it(self):
        prompt = build_refutation_prompt(
            finds=[find(title="First move"), find(title="Second move")],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert "index 0" in prompt
        assert "index 1" in prompt

    def test_it_carries_the_price_the_find_put_on_itself(self):
        # 8b4009e5 priced a group package off a 2018 newspaper article. The
        # number is a claim like any other and has to be refutable.
        prompt = build_refutation_prompt(
            finds=[find(cents=4000)],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert "4000" in prompt

    def test_a_cited_row_that_never_reached_retrieval_is_not_invented(self):
        # Defensive: a find citing an id the caller did not hand over must not
        # crash the night, and must not silently look like it had evidence.
        prompt = build_refutation_prompt(
            finds=[find(cites=("obs-1", "ghost"))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert "ghost" not in prompt


class TestThePromptCarriesWhatTheBusinessAlreadyIs:
    def test_the_offer_inventory_is_in_the_prompt(self):
        # Find 818fdb2d's failure is invisible from the find and its citations
        # alone: nothing it cited mentioned the Dosirak set. The correction was
        # in three uncited rows of the tenant's own memory, which is what the
        # business_state block gathers.
        from brasstacks.business_state import build_business_state
        from brasstacks.repository import StoredObservation

        state = build_business_state(
            business={"name": "Yellow Cow Korean BBQ"},
            facts=(),
            observations=[StoredObservation(
                observation_id="obs-9",
                content=("Featured items. Dosirak (Set Meal) · Set Meal "
                         "· Grilled Pork."),
                kind="trend", observed_at=CAPTURE, source_name="web",
                source_url="https://www.grubhub.com/restaurant/yellow-cow")],
        )

        prompt = build_refutation_prompt(
            finds=[find(title="Launch a fixed-price weekday lunch set")],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
            business_state=state,
        )

        assert "Dosirak" in prompt

    def test_without_a_state_block_the_prompt_still_builds(self):
        prompt = build_refutation_prompt(
            finds=[find()], retrieved={"obs-1": row("obs-1", BENTO_ROW)},
            business_state=None)

        assert "Photograph the weekday lunch bento" in prompt


class TestTheFramingIsRefutationNotReview:
    def test_the_system_prompt_asks_the_model_to_prove_the_find_wrong(self):
        text = REFUTER_SYSTEM_PROMPT.lower()

        assert "wrong" in text
        assert "refut" in text

    def test_it_tells_the_model_that_failing_to_refute_is_a_publish(self):
        # Without this the model reaches for "withhold" whenever it feels
        # uneasy, and an uneasy model every night is the dead product the
        # deterministic gates already proved out at 9 of 9.
        assert "publish" in REFUTER_SYSTEM_PROMPT.lower()
        for action in REFUTER_ACTIONS:
            assert action in REFUTER_SYSTEM_PROMPT

    def test_the_schema_asks_for_a_verdict_per_find(self):
        item = REFUTATION_SCHEMA["properties"]["verdicts"]["items"]

        assert set(item["required"]) >= {"index", "action", "reason"}
        # No `enum`, matching FIND_SCHEMA. The structured-output endpoint has
        # already rejected keywords on that schema and a 400 costs the check.
        assert "enum" not in item["properties"]["action"]


# ---------------------------------------------------------------------------
# Reading the verdicts back
# ---------------------------------------------------------------------------

class TestOneCallCoversTheNight:
    def test_three_finds_cost_one_model_call(self):
        # 40-70 seconds per tenant, not per find. Three calls would put a
        # three-tenant night past the point where the 900-second Lambda ceiling
        # stops being comfortable.
        reasoner = FakeReasoner([reply(verdict(0, "publish"),
                                       verdict(1, "publish"),
                                       verdict(2, "publish"))])

        result = refute_finds(
            reasoner=reasoner,
            finds=[find(), find(), find()],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert len(reasoner.calls) == 1
        assert result.checked is True
        assert [v.action for v in result.verdicts] == ["publish"] * 3

    def test_no_finds_means_no_call_at_all(self):
        reasoner = FakeReasoner([])

        result = refute_finds(reasoner=reasoner, finds=[], retrieved={})

        assert reasoner.calls == []
        assert result.verdicts == ()
        assert result.checked is True


class TestAWithholdHasToQuoteTheRowThatDoesIt:
    def test_a_withhold_naming_a_cited_row_stands(self):
        reasoner = FakeReasoner([reply(verdict(
            0, "withhold",
            reason="the cited listing already shows a photo of the bento",
            contradicted_by=["obs-1"]))])

        result = refute_finds(
            reasoner=reasoner, finds=[find(cites=("obs-1",))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(0).action == "withhold"
        assert result.for_index(0).contradicted_by == ("obs-1",)

    def test_a_withhold_that_names_nothing_becomes_a_demotion(self):
        # "Prove it or price it." A refuter that can withhold on a feeling is
        # one bad night away from an empty board, and the deterministic gates
        # already showed what a gate that cannot say which row it read costs.
        reasoner = FakeReasoner([reply(verdict(
            0, "withhold", reason="this feels thin"))])

        result = refute_finds(
            reasoner=reasoner, finds=[find(cites=("obs-1",))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(0).action == "demote"
        assert "this feels thin" in result.for_index(0).reason

    def test_a_withhold_naming_a_row_this_find_never_cited_becomes_a_demotion(self):
        reasoner = FakeReasoner([reply(verdict(
            0, "withhold", reason="wrong row", contradicted_by=["obs-2"]))])

        result = refute_finds(
            reasoner=reasoner, finds=[find(cites=("obs-1",))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW),
                       "obs-2": row("obs-2", POSTMATES_ROW)})

        assert result.for_index(0).action == "demote"

    def test_a_withhold_may_name_a_row_from_the_offer_inventory(self):
        # The 818fdb2d shape: what refutes the find is a row the find never
        # cited, because it never looked. Those ids are in the state block the
        # refuter was given, so naming one is naming something it can see.
        from brasstacks.business_state import build_business_state
        from brasstacks.repository import StoredObservation

        state = build_business_state(
            business={"name": "Yellow Cow Korean BBQ"}, facts=(),
            observations=[StoredObservation(
                observation_id="obs-9",
                content=("Featured items. Dosirak (Set Meal) · Set Meal "
                         "· Grilled Pork."),
                kind="trend", observed_at=CAPTURE, source_name="web",
                source_url="https://www.grubhub.com/restaurant/yellow-cow")])
        reasoner = FakeReasoner([reply(verdict(
            0, "withhold", reason="they already sell the Dosirak set meal",
            contradicted_by=["obs-9"]))])

        result = refute_finds(
            reasoner=reasoner, finds=[find(cites=("obs-1",))],
            retrieved={"obs-1": row("obs-1", BENTO_ROW)},
            business_state=state)

        assert result.for_index(0).action == "withhold"


class TestWhatEachActionCostsTheFind:
    def test_a_publish_keeps_its_money(self):
        reasoner = FakeReasoner([reply(verdict(0, "publish"))])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(0).money_allowed is True

    def test_a_demotion_takes_the_dollar_figure_off_it(self):
        reasoner = FakeReasoner([reply(verdict(
            0, "demote", reason="the competitor set is not in any cited row"))])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(0).money_allowed is False
        assert result.for_index(0).withheld is False

    def test_a_withhold_is_withheld(self):
        reasoner = FakeReasoner([reply(verdict(
            0, "withhold", reason="its own row shows the photo",
            contradicted_by=["obs-1"]))])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(0).withheld is True
        assert result.for_index(0).money_allowed is False


class TestOutputWeCannotRead:
    @pytest.mark.parametrize("action", ["", "reject", "APPROVE", None, 7])
    def test_an_action_we_do_not_recognise_publishes_unchecked(self, action):
        reasoner = FakeReasoner([reply({"index": 0, "action": action,
                                        "reason": "?"})])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(0).action == "publish"
        assert result.for_index(0).checked is False
        assert result.for_index(0).money_allowed is False

    def test_a_find_with_no_verdict_publishes_unchecked(self):
        reasoner = FakeReasoner([reply(verdict(0, "publish"))])

        result = refute_finds(reasoner=reasoner, finds=[find(), find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(1).action == "publish"
        assert result.for_index(1).checked is False

    def test_a_verdict_for_a_find_that_does_not_exist_is_ignored(self):
        reasoner = FakeReasoner([reply(verdict(0, "publish"),
                                       verdict(9, "withhold"))])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert [v.index for v in result.verdicts] == [0]

    def test_a_body_where_no_verdict_was_readable_is_an_outage(self):
        # Three garbage actions is not a check that ran and faulted nothing. If
        # the receipt said "ok" here, an operator reading it would believe the
        # deck had been challenged when nothing had looked at it.
        reasoner = FakeReasoner([reply(
            {"index": 0, "action": "yes", "reason": "?"},
            {"index": 1, "action": "no", "reason": "?"})])

        result = refute_finds(reasoner=reasoner, finds=[find(), find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.checked is False

    def test_one_readable_verdict_still_counts_as_a_check(self):
        reasoner = FakeReasoner([reply(
            verdict(0, "publish"),
            {"index": 1, "action": "no", "reason": "?"})])

        result = refute_finds(reasoner=reasoner, finds=[find(), find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.checked is True
        assert result.for_index(1).checked is False

    @pytest.mark.parametrize("payload", [
        {"verdicts": "nope"},
        {"verdicts": [["not", "an", "object"]]},
        {},
    ])
    def test_a_body_we_cannot_read_fails_open(self, payload):
        reasoner = FakeReasoner([payload])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.checked is False
        assert result.for_index(0).action == "publish"


class TestItFailsOpen:
    def test_an_exception_publishes_rather_than_withholds(self):
        # The test this whole component turns on. The refuter is one network
        # call away from failing on any given night, and a checker outage that
        # blanked the owner's board would be a worse product than no checker.
        reasoner = FakeReasoner([ReasoningError("upstream 529")])

        result = refute_finds(reasoner=reasoner, finds=[find(), find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.checked is False
        assert [v.withheld for v in result.verdicts] == [False, False]
        assert [v.action for v in result.verdicts] == ["publish", "publish"]

    def test_the_outage_takes_the_dollar_figure_off_what_it_publishes(self):
        reasoner = FakeReasoner([ReasoningError("upstream 529")])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.for_index(0).money_allowed is False
        assert result.for_index(0).reason == NOT_CHECKED_REASON

    def test_it_records_what_went_wrong(self):
        reasoner = FakeReasoner([ReasoningError("upstream 529")])

        result = refute_finds(reasoner=reasoner, finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert "upstream 529" in result.error

    def test_an_error_that_is_not_a_provider_error_also_fails_open(self):
        # A timeout from the HTTP layer, a JSON bug, an attribute error in a
        # provider we did not write. Anything at all: this call is advisory and
        # nothing it can do is worth costing the owner her morning.
        class Exploding:
            def complete_json(self, **kwargs):
                raise TimeoutError("read timed out")

        result = refute_finds(reasoner=Exploding(), finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert result.checked is False
        assert result.for_index(0).action == "publish"

    def test_a_missing_index_beyond_the_verdict_list_still_answers(self):
        result = refute_finds(reasoner=FakeReasoner([ReasoningError("x")]),
                              finds=[find()],
                              retrieved={"obs-1": row("obs-1", BENTO_ROW)})

        assert isinstance(result.for_index(4), Refutation)
        assert result.for_index(4).checked is False


# ---------------------------------------------------------------------------
# The night
# ---------------------------------------------------------------------------

GRUBHUB = "https://www.grubhub.com/restaurant/yellow-cow/2033337"
FACEBOOK = "https://www.facebook.com/yellowcowkbbq"
# What Grubhub renders on a shut storefront — `signals.classify_statement`
# reads it as `page_state`, which is what lets the claim standards withhold a
# find resting on it. Two of the night tests below need a move the mechanical
# gate refuses before the adversary is ever asked.
STOREFRONT = ("Yellow Cow Korean BBQ 4.9 (257 ratings) $0 delivery fee. "
              "This menu isn't available right now. Preorder for 6:30pm.")
HOURS = ("Mon, Wed, Sun - 11AM~9PM  Tue - Closed  "
         "Thur, Sat - 11AM~9:30PM  Fri - 11AM~10PM "
         "(310) 329-7343 1835 W Redondo Beach Blvd")


def _night_repo():
    from brasstacks.providers import FakeEmbedder
    from brasstacks.repository import InMemoryRepository

    repo = InMemoryRepository()
    business = repo.create_business(
        name="Yellow Cow Korean BBQ", category="restaurant",
        city="1835 W Redondo Beach Blvd, Gardena, CA 90247, United States")
    embedder = FakeEmbedder()
    ids = []
    for content, url in ((STOREFRONT, GRUBHUB), (HOURS, FACEBOOK)):
        [vector] = embedder.embed([content])
        ids.append(repo.insert_observation(
            business, content=content, kind="trend", embedding=vector,
            observed_at=CAPTURE, source_name="web", source_url=url))
    return repo, business, ids


def _proposal(ids, **overrides):
    body = {
        "emoji": "🍱",
        "title": "Launch a fixed-price weekday lunch set",
        "claim_type": "opportunity",
        "summary": "Weekday lunch traffic would carry a fixed-price set.",
        "rationale": "Nearby offices want a quick midday option.",
        "move": "Price a weekday lunch set at $16.",
        "alternative_explanation": "They may already sell one.",
        "predicted_daily_cents": 3200,
        "confidence": 0.5,
        "verify_after_days": 21,
        "evidence_observation_ids": list(ids),
    }
    body.update(overrides)
    return body


def _run(repo, business, reasoner, refuter):
    from brasstacks.agents.analyst import run_analyst
    from brasstacks.providers import FakeEmbedder

    return run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                       business_id=business, today=TODAY, refuter=refuter)


class TestTheNightRunsTheRefutationPass:
    def test_it_runs_after_the_analyst_and_before_anything_is_stored(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([reply(verdict(
            0, "withhold", reason="they already sell the Dosirak set meal",
            contradicted_by=[ids[0]]))])

        result = _run(repo, business, FR([_proposal(ids)]), refuter)

        assert result.find_id is None
        assert repo.recent_finds(business, limit=10) == []
        assert len(refuter.calls) == 1

    def test_the_refuted_find_is_kept_with_the_reason_it_was_refuted_for(self):
        from brasstacks.providers import FakeReasoner as FR
        from brasstacks.repository import WITHHELD_STATUS

        repo, business, ids = _night_repo()
        refuter = FR([reply(verdict(
            0, "withhold", reason="they already sell the Dosirak set meal",
            contradicted_by=[ids[0]]))])

        _run(repo, business, FR([_proposal(ids)]), refuter)

        [held] = repo.recent_finds(business, limit=10, include_unseen=True)
        context = repo.get_find_context(business, held.find_id)
        assert context.status == WITHHELD_STATUS
        assert "Dosirak" in context.withheld_reason

    def test_a_demoted_find_reaches_the_board_without_a_dollar_figure(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([reply(verdict(
            0, "demote", reason="the competitor prices are in no cited row"))])

        result = _run(repo, business, FR([_proposal(ids)]), refuter)

        assert result.find_id is not None
        [found] = repo.recent_finds(business, limit=10)
        assert found.predicted_daily_cents == 0

    def test_a_published_find_keeps_its_prediction(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([reply(verdict(0, "publish", reason="could not fault it"))])

        _run(repo, business, FR([_proposal(ids)]), refuter)

        [found] = repo.recent_finds(business, limit=10)
        assert found.predicted_daily_cents == 3200

    def test_one_refuted_card_does_not_cost_the_deck(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([reply(
            verdict(0, "withhold", reason="already sold", contradicted_by=[ids[0]]),
            verdict(1, "publish"))])
        survivor = _proposal(ids, title="Put the set meal price on the storefront")

        _run(repo, business, FR([{"finds": [_proposal(ids), survivor]}]), refuter)

        assert [f.title for f in repo.recent_finds(business, limit=10)] == [
            "Put the set meal price on the storefront"]

    def test_a_find_the_claim_standards_already_withheld_is_not_sent_to_it(self):
        # It costs tokens and seconds to ask an adversary to refute something
        # already off the board, and a `publish` verdict on it must never be
        # able to put it back.
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        outage = _proposal(
            ids, title="Fix the delivery menu so orders can be placed",
            claim_type="current_state",
            summary='Your delivery listing is not available right now.',
            move="Open the delivery dashboard.")
        refuter = FR([reply(verdict(0, "publish"))])

        result = _run(repo, business, FR([{"finds": [outage, _proposal(ids)]}]),
                      refuter)

        prompt = refuter.calls[0]["user"]
        assert "Fix the delivery menu" not in prompt
        assert [o.verdict.withheld for o in result.outcomes] == [True, False]

    def test_every_find_withheld_by_the_standards_means_no_call_at_all(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        outage = _proposal(
            ids, claim_type="current_state",
            summary="Your delivery listing is not available right now.")
        refuter = FR([])

        _run(repo, business, FR([outage]), refuter)

        assert refuter.calls == []


class TestAnOutageDoesNotBlankTheBoard:
    def test_a_refuter_that_raises_still_publishes_the_night(self):
        # The end-to-end version of the fail-open test. This is the difference
        # between a checker and an outage that costs the owner her morning.
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([ReasoningError("upstream 529")])

        result = _run(repo, business, FR([_proposal(ids)]), refuter)

        assert result.find_id is not None
        assert len(repo.recent_finds(business, limit=10)) == 1

    def test_it_publishes_without_the_dollar_figure(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([ReasoningError("upstream 529")])

        _run(repo, business, FR([_proposal(ids)]), refuter)

        [found] = repo.recent_finds(business, limit=10)
        assert found.predicted_daily_cents == 0

    def test_the_run_row_still_says_the_night_was_ok(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([ReasoningError("upstream 529")])

        _run(repo, business, FR([_proposal(ids)]), refuter)

        [run] = repo.recent_runs(business, limit=1)
        assert run.status == "ok"


class TestTheReceipt:
    def test_it_records_that_the_check_ran_and_what_it_did(self):
        from brasstacks.analyst_trace import parse_analyst_trace
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([reply(
            verdict(0, "withhold", reason="already sold", contradicted_by=[ids[0]]),
            verdict(1, "demote", reason="the prices are in no cited row"))])
        second = _proposal(ids, title="Put the set meal price on the storefront")

        _run(repo, business, FR([{"finds": [_proposal(ids), second]}]), refuter)

        [run] = repo.recent_runs(business, limit=1)
        trace = parse_analyst_trace(run.note)
        assert trace["refutation"] == "ok"
        assert trace["refuted_withheld"] == 1
        assert trace["refuted_demoted"] == 1

    def test_a_find_that_lost_its_price_without_a_demotion_is_still_counted(self):
        # The partial case: the call succeeded, but came back with no verdict
        # for the second move. That move publishes with no dollar figure, and
        # `refuted_demoted` alone would say zero — leaving an operator with a
        # priceless card and nothing in the receipt that explains it.
        from brasstacks.analyst_trace import parse_analyst_trace
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([reply(verdict(0, "publish"))])
        second = _proposal(ids, title="Put the set meal price on the storefront")

        _run(repo, business, FR([{"finds": [_proposal(ids), second]}]), refuter)

        [run] = repo.recent_runs(business, limit=1)
        trace = parse_analyst_trace(run.note)
        assert trace["refuted_demoted"] == 0
        assert trace["refuted_unpriced"] == 1
        priced = {f.title: f.predicted_daily_cents
                  for f in repo.recent_finds(business, limit=10)}
        assert priced["Put the set meal price on the storefront"] == 0

    def test_an_outage_is_named_in_the_receipt(self):
        from brasstacks.analyst_trace import parse_analyst_trace
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()

        _run(repo, business, FR([_proposal(ids)]),
             FR([ReasoningError("upstream 529")]))

        [run] = repo.recent_runs(business, limit=1)
        assert parse_analyst_trace(run.note)["refutation"] == "unavailable"

    def test_a_night_where_nothing_survived_to_be_challenged_claims_no_check(self):
        # The claim standards took every move, so no adversarial call was made.
        # Recording "ok" would say a check ran and faulted nothing.
        from brasstacks.analyst_trace import parse_analyst_trace
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        outage = _proposal(
            ids, claim_type="current_state",
            summary="Your delivery listing is not available right now.")

        _run(repo, business, FR([outage]), FR([]))

        [run] = repo.recent_runs(business, limit=1)
        assert "refutation" not in parse_analyst_trace(run.note)

    def test_a_night_with_no_refuter_says_so_rather_than_claiming_a_check(self):
        # Absence has to be distinguishable from a clean pass. A deployment
        # wired without a refuter looks exactly like one where the adversary
        # could fault nothing, unless the receipt says which it was.
        from brasstacks.analyst_trace import parse_analyst_trace
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()

        _run(repo, business, FR([_proposal(ids)]), None)

        [run] = repo.recent_runs(business, limit=1)
        assert "refutation" not in parse_analyst_trace(run.note)


class TestWithoutARefuterNothingChanges:
    def test_the_prediction_survives_a_night_with_no_adversary(self):
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()

        _run(repo, business, FR([_proposal(ids)]), None)

        [found] = repo.recent_finds(business, limit=10)
        assert found.predicted_daily_cents == 3200


class TestTheNightWiresItThrough:
    def test_run_night_hands_the_refuter_to_the_analyst(self):
        from brasstacks.night import run_night
        from brasstacks.outcomes import NoOutcomeSource
        from brasstacks.providers import FakeEmbedder
        from brasstacks.providers import FakeReasoner as FR

        repo, business, ids = _night_repo()
        refuter = FR([reply(verdict(0, "demote", reason="unsupported price"))])

        run_night(repo=repo, embedder=FakeEmbedder(),
                  reasoner=FR([_proposal(ids)]), outcomes=NoOutcomeSource(),
                  business_id=business, today=TODAY, sources=[],
                  refuter=refuter)

        assert len(refuter.calls) == 1
        [found] = repo.recent_finds(business, limit=10)
        assert found.predicted_daily_cents == 0

    def test_the_deployed_night_builds_one(self):
        # A refuter that is never constructed is a check that never runs, and
        # the night would look identical in the logs either way.
        import inspect

        from brasstacks.handlers import night as night_handler

        assert "refuter" in inspect.getsource(night_handler.handler)


class TestPromptConstructionCannotTakeTheNight:
    """The tranche's central promise, stated absolutely in three docstrings and
    false until this test.

    ``build_refutation_prompt`` was called OUTSIDE the try, so a failure while
    assembling the prompt — a business_state shape that moved under us, a URL
    that will not parse — propagated out of ``run_analyst``. The run went to
    'failed', nothing was stored, and the board went empty. That is precisely
    the outcome the refuter exists to prevent, arriving through the component
    built to prevent it. A checker is advisory or it is not worth having.
    """

    def test_a_failure_building_the_prompt_publishes_rather_than_raises(self, monkeypatch):
        import brasstacks.agents.refuter as refuter_module

        def explode(**_kwargs):
            raise TypeError("business_state shape changed under us")

        monkeypatch.setattr(refuter_module, "build_refutation_prompt", explode)
        reasoner = FakeReasoner([])

        result = refuter_module.refute_finds(
            reasoner=reasoner, finds=[find()], retrieved={})

        assert result.checked is False
        assert "TypeError" in (result.error or "")
        assert len(result.verdicts) == 1
        assert result.verdicts[0].action == "publish"
        # The error names the prompt failure, not a provider one — proof it
        # never reached the model rather than failing once it got there.
        assert "business_state shape changed" in (result.error or "")
