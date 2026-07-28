"""Validating the Analyst's output before it reaches the database.

The model proposes a find; this layer decides whether that proposal is
trustworthy enough to store. Everything downstream — the Meter's verdict, the
published hit rate, the ledger — inherits any error that slips through here.

A rejected find should fail the run loudly. A silently coerced one is worse than
a crash, because it looks like a real prediction forever.
"""

from datetime import date

import pytest

from brasstacks.finds import InvalidFindError, parse_find

TODAY = date(2026, 7, 28)
KNOWN_IDS = ("obs-1", "obs-2", "obs-3")


def payload(**overrides):
    """A valid Analyst payload. Tests override one field at a time."""
    base = {
        "emoji": "🍰",
        "title": "Tiramisu → $9",
        "rationale": "212 reviews single it out as the best in the city; it is priced below every rival.",
        "move": "Raise tiramisu to $9 and reprint the dessert card.",
        "predicted_daily_cents": 2300,
        "confidence": 0.82,
        "verify_after_days": 14,
        "evidence_observation_ids": ["obs-1", "obs-2"],
    }
    base.update(overrides)
    return base


class TestHappyPath:
    def test_parses_a_valid_payload(self):
        find = parse_find(payload(), today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.title == "Tiramisu → $9"
        assert find.predicted_daily_cents == 2300
        assert find.confidence == 0.82
        assert find.evidence_observation_ids == ("obs-1", "obs-2")

    def test_verify_after_is_offset_from_the_injected_date(self):
        # No clock reads — the caller supplies today, so this is deterministic.
        find = parse_find(payload(verify_after_days=14), today=TODAY,
                          known_observation_ids=KNOWN_IDS)
        assert find.verify_after == date(2026, 8, 11)

    def test_evidence_order_is_preserved(self):
        # Retrieval rank matters; find_evidence stores it.
        find = parse_find(payload(evidence_observation_ids=["obs-3", "obs-1"]),
                          today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.evidence_observation_ids == ("obs-3", "obs-1")


class TestMoneyIsIntegerCents:
    """The single most dangerous field. A model that thinks in dollars, or emits
    a float, must not quietly produce a 100x-wrong ledger entry."""

    def test_fractional_cents_are_rejected(self):
        with pytest.raises(InvalidFindError, match="whole cents"):
            parse_find(payload(predicted_daily_cents=23.5), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_integral_float_is_accepted_as_cents(self):
        # Models legitimately emit 2300.0 for an integer field. That is
        # unambiguous, so accept it rather than failing a whole nightly run.
        find = parse_find(payload(predicted_daily_cents=2300.0), today=TODAY,
                          known_observation_ids=KNOWN_IDS)
        assert find.predicted_daily_cents == 2300
        assert isinstance(find.predicted_daily_cents, int)

    def test_numeric_string_is_rejected(self):
        # "2300" is ambiguous about units and signals the model ignored the
        # schema. Fail rather than guess.
        with pytest.raises(InvalidFindError, match="predicted_daily_cents"):
            parse_find(payload(predicted_daily_cents="2300"), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_negative_prediction_is_rejected(self):
        with pytest.raises(InvalidFindError, match="non-negative"):
            parse_find(payload(predicted_daily_cents=-100), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_absurdly_large_prediction_is_rejected(self):
        # $50k/day for a neighbourhood restaurant means the model confused
        # units or hallucinated. Bound it rather than publish it.
        with pytest.raises(InvalidFindError, match="implausible"):
            parse_find(payload(predicted_daily_cents=5_000_000), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_boolean_is_not_a_number(self):
        with pytest.raises(InvalidFindError, match="predicted_daily_cents"):
            parse_find(payload(predicted_daily_cents=True), today=TODAY,
                       known_observation_ids=KNOWN_IDS)


class TestConfidence:
    def test_confidence_above_one_is_rejected(self):
        # A model emitting 82 for "82%" must not become confidence=82.
        with pytest.raises(InvalidFindError, match="confidence"):
            parse_find(payload(confidence=82), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_negative_confidence_is_rejected(self):
        with pytest.raises(InvalidFindError, match="confidence"):
            parse_find(payload(confidence=-0.1), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_boundaries_are_accepted(self):
        for c in (0.0, 1.0):
            find = parse_find(payload(confidence=c), today=TODAY,
                              known_observation_ids=KNOWN_IDS)
            assert find.confidence == c


class TestVerifyWindow:
    def test_zero_days_is_rejected(self):
        # The Meter must never judge a find the same day it was proposed —
        # there is no outcome data yet, so it would always read as a miss.
        with pytest.raises(InvalidFindError, match="verify_after_days"):
            parse_find(payload(verify_after_days=0), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_negative_days_is_rejected(self):
        with pytest.raises(InvalidFindError, match="verify_after_days"):
            parse_find(payload(verify_after_days=-3), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_absurdly_distant_window_is_rejected(self):
        # A find we cannot check for two years is not a prediction.
        with pytest.raises(InvalidFindError, match="verify_after_days"):
            parse_find(payload(verify_after_days=900), today=TODAY,
                       known_observation_ids=KNOWN_IDS)


class TestEvidence:
    """find_evidence is the receipt proving retrieval drove the reasoning. A
    find citing observations that were never retrieved is a hallucination, and
    it is exactly what a judge would probe."""

    def test_unknown_observation_id_is_rejected(self):
        with pytest.raises(InvalidFindError, match="not retrieved"):
            parse_find(payload(evidence_observation_ids=["obs-1", "obs-999"]),
                       today=TODAY, known_observation_ids=KNOWN_IDS)

    def test_empty_evidence_is_rejected(self):
        with pytest.raises(InvalidFindError, match="at least one"):
            parse_find(payload(evidence_observation_ids=[]), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_duplicate_evidence_is_deduplicated_preserving_order(self):
        find = parse_find(payload(evidence_observation_ids=["obs-2", "obs-1", "obs-2"]),
                          today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.evidence_observation_ids == ("obs-2", "obs-1")


class TestEscapeSequenceRepair:
    """Models sometimes double-escape their own JSON.

    Observed in production: a find whose move field contained the literal seven
    characters ``\\u2014`` rather than an em dash. json.loads had already run —
    the model wrote a doubled backslash, so the escape survived decoding and went
    straight into the database, then rendered as garbage on screen.

    This is the trust boundary, so the repair happens here rather than in the UI:
    every consumer of a find would otherwise need the same workaround.
    """

    def test_literal_em_dash_escape_is_decoded(self):
        find = parse_find(
            payload(move=r"Draft the card \u2014 nothing sends without your OK"),
            today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.move == "Draft the card — nothing sends without your OK"

    def test_literal_en_dash_escape_is_decoded(self):
        find = parse_find(
            payload(rationale=r"Recovering 4\u20136 covers per Saturday"),
            today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.rationale == "Recovering 4–6 covers per Saturday"

    def test_several_escapes_in_one_field(self):
        find = parse_find(
            payload(move=r"$150\u2013$230 extra \u2014 about $25\u2013$38 a day"),
            today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.move == "$150–$230 extra — about $25–$38 a day"

    def test_ordinary_text_is_untouched(self):
        find = parse_find(payload(title="Tiramisu → $9"), today=TODAY,
                          known_observation_ids=KNOWN_IDS)
        assert find.title == "Tiramisu → $9"

    def test_a_backslash_that_is_not_an_escape_survives(self):
        # Repairing must not corrupt legitimate text that happens to contain a
        # backslash.
        find = parse_find(payload(move=r"Put the file in C:\users\rosa"),
                          today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.move == r"Put the file in C:\users\rosa"

    def test_a_malformed_escape_is_left_alone(self):
        find = parse_find(payload(move=r"Discount \u20 percent"), today=TODAY,
                          known_observation_ids=KNOWN_IDS)
        assert find.move == r"Discount \u20 percent"


class TestRequiredText:
    @pytest.mark.parametrize("field", ["title", "rationale", "move"])
    def test_missing_field_is_rejected(self, field):
        p = payload()
        del p[field]
        with pytest.raises(InvalidFindError, match=field):
            parse_find(p, today=TODAY, known_observation_ids=KNOWN_IDS)

    @pytest.mark.parametrize("field", ["title", "rationale", "move"])
    def test_blank_field_is_rejected(self, field):
        with pytest.raises(InvalidFindError, match=field):
            parse_find(payload(**{field: "   "}), today=TODAY,
                       known_observation_ids=KNOWN_IDS)

    def test_text_is_stripped(self):
        find = parse_find(payload(title="  Tiramisu → $9  "), today=TODAY,
                          known_observation_ids=KNOWN_IDS)
        assert find.title == "Tiramisu → $9"

    def test_missing_emoji_defaults_rather_than_failing(self):
        # Cosmetic field. Losing a whole nightly find over a missing emoji
        # would be the wrong trade.
        p = payload()
        del p["emoji"]
        find = parse_find(p, today=TODAY, known_observation_ids=KNOWN_IDS)
        assert find.emoji == "💡"
