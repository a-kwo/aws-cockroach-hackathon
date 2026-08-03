"""The Analyst is told what the business already has before it recommends.

Three of the nine finds live on 2026-08-03 failed for one reason: the prompt
described the market and never described the business. So the night now reads
the whole stored corpus, folds it into a `BusinessState`, and puts that above
the retrieved rows.

The rules this adds are rules about *knowledge*, never about permission. Nothing
here may reduce the number of moves a night proposes — the gates that tried that
withheld 9 of 9 finds in review, and an owner with an empty board is not better
served than an owner with a weak one. A find that would have said "launch a
lunch set" must still be proposed; it must now say "your Dosirak set already
sells, price it for weekday lunch" instead.

The other half of this file is the prompt wording the never-shown-finds tranche
asked for. `recent_finds` learned to distinguish a find the owner saw from one a
gate withheld, and left the two sentences to be written here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from brasstacks.agents.analyst import (
    RECENT_FINDS_SHOWN,
    SYSTEM_PROMPT,
    build_prompt,
    run_analyst,
)
from brasstacks.business_state import build_business_state
from brasstacks.providers import FakeEmbedder, FakeReasoner
from brasstacks.repository import EvidenceRef, InMemoryRepository

TODAY = date(2026, 8, 3)
CAPTURED = datetime(2026, 8, 2, 15, 7, 11, tzinfo=timezone.utc)

YELLOW_COW = "Yellow Cow Korean BBQ"
YELLOW_COW_ADDRESS = "1835 W Redondo Beach Blvd, Gardena, CA 90247, United States"

#: Verbatim from observation 83838c9f, the Postmates storefront. The row find
#: 818fdb2d needed and never cited.
POSTMATES = ("Featured items. Dosirak (Set Meal) - 도시락세트 · Set Meal · "
             "Appetizers · Specialties · Fried Rice & Bibimbap · Soups & "
             "Noodles · Grilled Beef · Grilled Pork.")
POSTMATES_URL = "https://postmates.com/store/yellow-cow-korean-bbq/Bq_kf"

_EMBEDDING = [1.0] + [0.0] * 1023


def _find(title="Price the Dosirak set for weekday lunch", ids=()):
    return {
        "emoji": "🍱", "title": title,
        "summary": "The set meal already sells; a weekday lunch price would "
                   "put a number on the window for passing trade.",
        "rationale": "r", "move": "m",
        "alternative_explanation": "a",
        "predicted_daily_cents": 2300, "confidence": 0.4,
        "verify_after_days": 21,
        "evidence_observation_ids": list(ids),
    }


@pytest.fixture()
def yellow_cow():
    repo = InMemoryRepository()
    business = repo.create_business(name=YELLOW_COW, category="restaurant",
                                    city=YELLOW_COW_ADDRESS)
    observation = repo.insert_observation(
        business, content=POSTMATES, kind="trend", embedding=_EMBEDDING,
        source_name="web", source_url=POSTMATES_URL, observed_at=CAPTURED)
    return repo, business, observation


def _state(repo, business):
    return build_business_state(
        business=repo.get_business(business),
        facts=repo.get_business_facts(business),
        observations=repo.all_observations(business, limit=500),
    )


class TestTheBlockReachesThePrompt:
    def test_what_the_business_already_sells_is_in_the_prompt(self, yellow_cow):
        repo, business, _ = yellow_cow

        prompt = build_prompt(
            business=repo.get_business(business), facts=[], rules=[],
            retrieved=[], today=TODAY,
            business_state=_state(repo, business))

        assert "Dosirak (Set Meal)" in prompt
        assert "Already sells" in prompt

    def test_a_business_with_no_memory_adds_no_block(self, yellow_cow):
        repo, _, _ = yellow_cow
        empty = repo.create_business(name="Nothing Yet", category="restaurant",
                                     city="Torrance")

        prompt = build_prompt(
            business=repo.get_business(empty), facts=[], rules=[], retrieved=[],
            today=TODAY, business_state=_state(repo, empty))

        assert "Already sells" not in prompt

    def test_the_block_is_optional(self, yellow_cow):
        # Every existing caller passes no state, and must keep working.
        repo, business, _ = yellow_cow

        prompt = build_prompt(business=repo.get_business(business), facts=[],
                              rules=[], retrieved=[], today=TODAY)

        assert "Yellow Cow Korean BBQ" in prompt


class TestTheRulesThatFollowFromIt:
    def test_a_move_may_not_launch_what_the_business_already_sells(self):
        low = SYSTEM_PROMPT.lower()
        assert "already has" in low or "already sells" in low
        assert "improving, pricing or promoting" in low

    def test_an_undeclared_domain_must_be_attributed_before_it_is_criticised(self):
        low = SYSTEM_PROMPT.lower()
        assert "did not declare" in low
        assert "whose" in low

    def test_day_disjoint_sources_may_not_be_called_contradictory(self):
        low = SYSTEM_PROMPT.lower()
        assert "same weekday" in low


class TestTheNightBuildsItFromStoredMemory:
    def test_the_night_tells_the_model_what_is_already_on_sale(self, yellow_cow):
        # The inventory line, not the retrieved row. Only the block writes
        # "- Dosirak (Set Meal) (1 source; cite …)".
        repo, business, observation = yellow_cow
        reasoner = FakeReasoner([{"finds": [_find(ids=[observation])]}])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        assert "- Dosirak (Set Meal) (1 source; cite " in reasoner.calls[0]["user"]

    def test_it_still_proposes_every_move_the_model_returned(self, yellow_cow):
        # This tranche changes what the Analyst knows, not what it may say.
        repo, business, observation = yellow_cow
        others = [
            repo.insert_observation(
                business, content=f"Reviewers keep mentioning the banchan {n}.",
                kind="review", embedding=_EMBEDDING, source_name="web",
                source_url=f"https://www.tripadvisor.com/Review-{n}",
                observed_at=CAPTURED)
            for n in range(2)
        ]
        reasoner = FakeReasoner([{"finds": [_find("one", [observation]),
                                            _find("two", others[:1]),
                                            _find("three", others[1:])]}])

        result = run_analyst(repo=repo, embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY)

        assert len(result.find_ids) == 3

    def test_a_repository_without_the_reader_still_runs_the_night(self, yellow_cow):
        # The deployed Lambda and the local harness upgrade separately, and a
        # night must not fail because the two halves landed in either order.
        # That mistake cost us the ledger's actual_daily_cents column once.
        repo, business, observation = yellow_cow

        class Older:
            def __getattr__(self, name):
                if name == "all_observations":
                    raise AttributeError(name)
                return getattr(repo, name)

        reasoner = FakeReasoner([{"finds": [_find(ids=[observation])]}])
        result = run_analyst(repo=Older(), embedder=FakeEmbedder(),
                             reasoner=reasoner, business_id=business,
                             today=TODAY)

        assert result.find_id is not None
        assert "Already sells" not in reasoner.calls[0]["user"]


class TestTheTwoListsOfPriorFinds:
    """Wording the never-shown-finds tranche left for this one to write."""

    def _seed(self, statuses):
        repo = InMemoryRepository()
        business = repo.create_business(name=YELLOW_COW, category="restaurant",
                                        city=YELLOW_COW_ADDRESS)
        observation = repo.insert_observation(
            business, content=POSTMATES, kind="trend", embedding=_EMBEDDING,
            source_name="web", source_url=POSTMATES_URL, observed_at=CAPTURED)
        for title, status in statuses:
            repo.insert_find_with_evidence(
                business, title=title, rationale="r", move="m", emoji="x",
                predicted_daily_cents=4200, confidence=0.5,
                verify_after=TODAY + timedelta(days=14), status=status,
                evidence=[EvidenceRef(observation, 0.6)])
        return repo, business, observation

    def _prompt(self, repo, business):
        return build_prompt(
            business=repo.get_business(business), facts=[], rules=[],
            retrieved=[], today=TODAY,
            recent_finds=repo.recent_finds(business, limit=RECENT_FINDS_SHOWN,
                                           include_unseen=True))

    def test_a_withheld_find_is_offered_back_as_raisable(self):
        repo, business, _ = self._seed([("Open for lunch on weekdays", "withheld")])

        prompt = self._prompt(repo, business)

        assert "Open for lunch on weekdays" in prompt
        assert "NOT off limits" in prompt

    def test_a_withheld_find_is_not_on_the_forbidden_list(self):
        repo, business, _ = self._seed([("Open for lunch on weekdays", "withheld"),
                                        ("Saturday waitlist", "rejected")])

        prompt = self._prompt(repo, business)
        forbidden = prompt.split("Considered on an earlier night")[0]

        assert "Saturday waitlist" in forbidden
        assert "Open for lunch on weekdays" not in forbidden

    def test_a_withheld_find_carries_no_price(self):
        # A prediction nobody was shown was never a promise. Reprinting the
        # number invites the model to anchor on it.
        repo, business, _ = self._seed([("Open for lunch on weekdays", "withheld")])

        held_back = self._prompt(repo, business).split(
            "Considered on an earlier night")[1]

        assert "4200" not in held_back

    def test_the_system_prompt_names_the_list_it_means(self):
        # "the recent-finds list" no longer names one list.
        assert "recent-finds list" not in SYSTEM_PROMPT
        assert "Already proposed" in SYSTEM_PROMPT

    def test_the_night_asks_for_both_lists(self):
        repo, business, observation = self._seed(
            [("Open for lunch on weekdays", "withheld")])
        reasoner = FakeReasoner([{"finds": [_find(ids=[observation])]}])

        run_analyst(repo=repo, embedder=FakeEmbedder(), reasoner=reasoner,
                    business_id=business, today=TODAY)

        assert "Open for lunch on weekdays" in reasoner.calls[0]["user"]
