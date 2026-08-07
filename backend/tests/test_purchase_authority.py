"""Whether the agent may spend the owner's money without asking first.

This is the only code standing between a bug in the Quartermaster and the
owner's bank account, so it is deterministic, pure, and tested to death. No
model reads these rules and decides whether they apply — see docs/ORDERS_AGENT.md
section 6 for why a spend ceiling written in prose is not a ceiling.

Two outcomes only: place it now, or draft it and ask a human. There is
deliberately no "refuse outright" — failing closed here means falling back to
asking the owner, never silently dropping work they requested.

Money is integer cents throughout. No floats.
"""

import pytest

from brasstacks.purchase_authority import (
    Decision,
    Level,
    PurchaseAuthority,
    authorize,
)


def auto(scope: str, *, per_order=50_00, period_cap=None, period_days=7,
         enabled=True) -> PurchaseAuthority:
    return PurchaseAuthority(
        scope=scope, level=Level.AUTO, per_order_cap_cents=per_order,
        period_cap_cents=period_cap, period_days=period_days, enabled=enabled,
    )


class TestNothingConfigured:
    """An item nobody granted standing authority over is not forbidden — it is
    simply not pre-approved. The owner asking for tomatoes out of the blue must
    still work; it just goes through them."""

    def test_no_authority_at_all_asks_the_owner(self):
        result = authorize(item="tomatoes", order_total_cents=2_000,
                           authorities=[])
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_an_authority_for_something_else_does_not_apply(self):
        result = authorize(item="tomatoes", order_total_cents=2_000,
                           authorities=[auto("olive oil")])
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_the_reason_names_what_was_missing(self):
        # The owner sees this string when asked to approve. "no standing
        # authority" is actionable; a bare False is not.
        result = authorize(item="tomatoes", order_total_cents=2_000,
                           authorities=[])
        assert "authority" in result.reason.lower()


class TestAskAlways:
    """The default level, and the one every inferred-stock order uses no matter
    what else is configured."""

    def test_ask_always_asks_even_for_a_trivial_amount(self):
        rule = PurchaseAuthority(scope="tomatoes", level=Level.ASK_ALWAYS,
                                 per_order_cap_cents=100_00)
        result = authorize(item="tomatoes", order_total_cents=1,
                           authorities=[rule])
        assert result.decision is Decision.NEEDS_APPROVAL


class TestAuto:
    """Standing authority the owner granted ahead of time."""

    def test_within_the_cap_places_the_order(self):
        result = authorize(item="tomatoes", order_total_cents=20_00,
                           authorities=[auto("tomatoes", per_order=50_00)])
        assert result.decision is Decision.ALLOW

    def test_exactly_at_the_cap_is_still_allowed(self):
        # The cap is what the owner authorised, so spending precisely it is
        # inside the permission, not outside it.
        result = authorize(item="tomatoes", order_total_cents=50_00,
                           authorities=[auto("tomatoes", per_order=50_00)])
        assert result.decision is Decision.ALLOW

    def test_over_the_cap_escalates_rather_than_placing(self):
        result = authorize(item="tomatoes", order_total_cents=50_01,
                           authorities=[auto("tomatoes", per_order=50_00)])
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_the_escalation_reason_names_the_cap(self):
        result = authorize(item="tomatoes", order_total_cents=50_01,
                           authorities=[auto("tomatoes", per_order=50_00)])
        assert "50.00" in result.reason


class TestAskIfOver:
    """Handle the small stuff, ask about the big stuff."""

    def test_under_the_threshold_places_the_order(self):
        rule = PurchaseAuthority(
            scope="produce", level=Level.ASK_IF_OVER,
            per_order_cap_cents=200_00, auto_threshold_cents=30_00,
        )
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=25_00, authorities=[rule])
        assert result.decision is Decision.ALLOW

    def test_over_the_threshold_asks(self):
        rule = PurchaseAuthority(
            scope="produce", level=Level.ASK_IF_OVER,
            per_order_cap_cents=200_00, auto_threshold_cents=30_00,
        )
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=30_01, authorities=[rule])
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_ask_if_over_without_a_threshold_is_rejected(self):
        # A threshold-less ask_if_over rule is meaningless, and guessing a
        # default would be guessing with the owner's money.
        rule = PurchaseAuthority(scope="produce", level=Level.ASK_IF_OVER,
                                 per_order_cap_cents=200_00)
        with pytest.raises(ValueError):
            authorize(item="tomatoes", category="produce",
                      order_total_cents=10_00, authorities=[rule])


class TestRevocation:
    """A permission the owner withdrew must stop working immediately — not at
    the end of a period, and not after the next order."""

    def test_a_disabled_authority_does_not_authorise(self):
        result = authorize(item="tomatoes", order_total_cents=10_00,
                           authorities=[auto("tomatoes", enabled=False)])
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_revoking_does_not_ban_the_item(self):
        # Withdrawing standing permission stops autonomy. It does not mean the
        # owner may never buy tomatoes again.
        result = authorize(item="tomatoes", order_total_cents=10_00,
                           authorities=[auto("tomatoes", enabled=False)])
        assert result.decision is Decision.NEEDS_APPROVAL
        assert "revoked" in result.reason.lower()


class TestRollingPeriodCap:
    """The backstop against a loop that places many individually-small orders.

    Per-order caps cannot catch this: a hundred £5 orders each pass. The period
    cap is what makes a runaway agent expensive-but-bounded instead of ruinous.
    """

    def test_spend_within_the_period_cap_is_allowed(self):
        rule = auto("produce", per_order=50_00, period_cap=200_00)
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=20_00, authorities=[rule],
                           spent_in_period_cents=100_00)
        assert result.decision is Decision.ALLOW

    def test_an_order_that_would_cross_the_period_cap_escalates(self):
        rule = auto("produce", per_order=50_00, period_cap=200_00)
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=20_00, authorities=[rule],
                           spent_in_period_cents=190_00)
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_landing_exactly_on_the_period_cap_is_allowed(self):
        rule = auto("produce", per_order=50_00, period_cap=200_00)
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=10_00, authorities=[rule],
                           spent_in_period_cents=190_00)
        assert result.decision is Decision.ALLOW

    def test_already_over_the_cap_escalates_even_for_a_tiny_order(self):
        rule = auto("produce", per_order=50_00, period_cap=200_00)
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=1, authorities=[rule],
                           spent_in_period_cents=200_00)
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_no_period_cap_means_only_the_per_order_cap_binds(self):
        rule = auto("produce", per_order=50_00, period_cap=None)
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=20_00, authorities=[rule],
                           spent_in_period_cents=10_000_00)
        assert result.decision is Decision.ALLOW


class TestScopeMatching:
    """An owner says "produce", the cart says "tomatoes". The link between them
    has to be explicit, because inferring it is how an agent talks itself into
    a permission nobody granted."""

    def test_a_category_authority_covers_an_item_in_it(self):
        result = authorize(item="tomatoes", category="produce",
                           order_total_cents=10_00,
                           authorities=[auto("produce")])
        assert result.decision is Decision.ALLOW

    def test_an_item_authority_beats_a_broader_category_one(self):
        # The specific rule is the one the owner wrote about this thing.
        item_rule = PurchaseAuthority(
            scope="saffron", level=Level.ASK_ALWAYS, per_order_cap_cents=500_00)
        result = authorize(item="saffron", category="produce",
                           order_total_cents=10_00,
                           authorities=[auto("produce"), item_rule])
        assert result.decision is Decision.NEEDS_APPROVAL

    def test_matching_ignores_case_and_surrounding_space(self):
        result = authorize(item="  Tomatoes ", order_total_cents=10_00,
                           authorities=[auto("tomatoes")])
        assert result.decision is Decision.ALLOW

    def test_a_category_authority_does_not_cover_an_uncategorised_item(self):
        result = authorize(item="tomatoes", order_total_cents=10_00,
                           authorities=[auto("produce")])
        assert result.decision is Decision.NEEDS_APPROVAL


class TestCorruptInput:
    """Fail loudly. A malformed amount must never be quietly treated as zero and
    waved through."""

    def test_a_negative_total_is_rejected(self):
        with pytest.raises(ValueError):
            authorize(item="tomatoes", order_total_cents=-1,
                      authorities=[auto("tomatoes")])

    def test_a_float_total_is_rejected(self):
        # Money is integer cents everywhere in this codebase. A float arriving
        # here means someone parsed a receipt wrong upstream.
        with pytest.raises(TypeError):
            authorize(item="tomatoes", order_total_cents=20.00,
                      authorities=[auto("tomatoes")])

    def test_a_negative_prior_spend_is_rejected(self):
        with pytest.raises(ValueError):
            authorize(item="tomatoes", order_total_cents=10_00,
                      authorities=[auto("tomatoes")],
                      spent_in_period_cents=-1)

    def test_a_negative_cap_is_rejected(self):
        with pytest.raises(ValueError):
            authorize(item="tomatoes", order_total_cents=10_00,
                      authorities=[auto("tomatoes", per_order=-1)])

    def test_an_empty_item_is_rejected(self):
        with pytest.raises(ValueError):
            authorize(item="   ", order_total_cents=10_00, authorities=[])


class TestZeroTotals:
    """A zero-cent order is not a purchase, and treating it as pre-approved
    would let a cart that failed to price itself sail through."""

    def test_a_zero_total_asks_rather_than_placing(self):
        result = authorize(item="tomatoes", order_total_cents=0,
                           authorities=[auto("tomatoes")])
        assert result.decision is Decision.NEEDS_APPROVAL
