"""The boundary between Brass Tacks and whoever actually moves the money.

Same shape as the ordering seam, for the same reason: everything above this
line is built and tested against ``FakePaymentTool``, and the real Stripe
adapter implements the identical protocol underneath. A suite that needed a
Stripe account would be a suite nobody could run.

The protocol is deliberately two verbs. ``charge`` spends the owner's money;
``refund`` gives it back. Nothing else exists, so the dangerous calls have
exactly one implementation each to audit.

Money is integer cents throughout. No floats.
"""

import pytest

from brasstacks.payments import (
    Charge,
    FakePaymentTool,
    PaymentDeclined,
    PaymentError,
    PaymentTool,
    Refund,
    StripePaymentTool,
)


def fake(**kwargs) -> FakePaymentTool:
    return FakePaymentTool(**kwargs)


class TestItIsATool:
    def test_the_fake_satisfies_the_protocol(self):
        # runtime_checkable, so a swap to the Stripe adapter is caught by the
        # type system rather than at checkout.
        assert isinstance(fake(), PaymentTool)

    def test_the_stripe_adapter_satisfies_the_protocol(self):
        adapter = StripePaymentTool(api_key="sk_test_x", payment_method="pm_x",
                                    transport=lambda *a, **k: (200, {}))
        assert isinstance(adapter, PaymentTool)

    def test_tools_name_themselves(self):
        # Named in receipts and task events, so a failure says which tool failed.
        assert fake().name
        adapter = StripePaymentTool(api_key="sk_test_x", payment_method="pm_x",
                                    transport=lambda *a, **k: (200, {}))
        assert adapter.name == "stripe"


class TestCharging:
    def test_a_charge_returns_its_amount(self):
        charge = fake().charge(amount_cents=13_50, description="Test order",
                               idempotency_key="k1")
        assert charge.amount_cents == 13_50

    def test_a_charge_carries_an_external_reference(self):
        # What a human would quote to Stripe support.
        charge = fake().charge(amount_cents=1_00, description="x",
                               idempotency_key="k1")
        assert charge.external_reference

    def test_the_tool_records_what_it_charged(self):
        tool = fake()
        tool.charge(amount_cents=1_00, description="x", idempotency_key="k1")
        assert len(tool.charged) == 1

    def test_a_float_amount_is_refused(self):
        # 13.50 dollars looks harmless and is 13 cents after int(). A float
        # here means somebody parsed a price wrong upstream.
        with pytest.raises(TypeError):
            fake().charge(amount_cents=13.50, description="x",
                          idempotency_key="k1")

    def test_a_bool_amount_is_refused(self):
        with pytest.raises(TypeError):
            fake().charge(amount_cents=True, description="x",
                          idempotency_key="k1")

    def test_a_zero_amount_is_refused(self):
        # A zero charge is always a caller bug, never an intent.
        with pytest.raises(ValueError):
            fake().charge(amount_cents=0, description="x", idempotency_key="k1")

    def test_a_negative_amount_is_refused(self):
        with pytest.raises(ValueError):
            fake().charge(amount_cents=-5_00, description="x",
                          idempotency_key="k1")


class TestIdempotency:
    """The single most dangerous path. A retried Lambda, a double-clicked
    approve — each must charge the owner exactly once."""

    def test_the_same_key_twice_charges_once(self):
        tool = fake()
        tool.charge(amount_cents=1_00, description="x", idempotency_key="k1")
        tool.charge(amount_cents=1_00, description="x", idempotency_key="k1")
        assert len(tool.charged) == 1

    def test_a_replay_returns_the_original_charge(self):
        tool = fake()
        first = tool.charge(amount_cents=1_00, description="x",
                            idempotency_key="k1")
        second = tool.charge(amount_cents=1_00, description="x",
                             idempotency_key="k1")
        assert second.external_reference == first.external_reference

    def test_a_replay_with_a_different_amount_is_refused(self):
        # Same key, different money means the caller has a bug. Returning the
        # old charge would hide it; charging again would double-spend.
        tool = fake()
        tool.charge(amount_cents=1_00, description="x", idempotency_key="k1")
        with pytest.raises(PaymentError):
            tool.charge(amount_cents=2_00, description="x",
                        idempotency_key="k1")

    def test_an_empty_key_is_refused(self):
        with pytest.raises(ValueError):
            fake().charge(amount_cents=1_00, description="x",
                          idempotency_key="")


class TestFailure:
    def test_an_injected_failure_raises(self):
        tool = fake()
        tool.fail_next("card declined")
        with pytest.raises(PaymentError, match="card declined"):
            tool.charge(amount_cents=1_00, description="x",
                        idempotency_key="k1")

    def test_a_decline_is_its_own_type(self):
        # A decline has an owner-facing remedy — a different card — where a
        # network failure has a retry. Callers must be able to tell them apart.
        tool = fake()
        tool.fail_next("card declined", declined=True)
        with pytest.raises(PaymentDeclined):
            tool.charge(amount_cents=1_00, description="x",
                        idempotency_key="k1")

    def test_a_failed_charge_is_not_recorded(self):
        tool = fake()
        tool.fail_next("network")
        with pytest.raises(PaymentError):
            tool.charge(amount_cents=1_00, description="x",
                        idempotency_key="k1")
        assert tool.charged == []

    def test_a_failure_does_not_burn_the_idempotency_key(self):
        # A charge that never happened must be retryable under the same key.
        tool = fake()
        tool.fail_next("network")
        with pytest.raises(PaymentError):
            tool.charge(amount_cents=1_00, description="x",
                        idempotency_key="k1")
        charge = tool.charge(amount_cents=1_00, description="x",
                             idempotency_key="k1")
        assert charge.amount_cents == 1_00


class TestRefunds:
    def test_a_charge_can_be_refunded_in_full(self):
        tool = fake()
        charge = tool.charge(amount_cents=13_50, description="x",
                             idempotency_key="k1")
        refund = tool.refund(charge_reference=charge.external_reference,
                             idempotency_key="r1")
        assert refund.amount_cents == 13_50

    def test_refunding_an_unknown_charge_is_refused(self):
        with pytest.raises(PaymentError):
            fake().refund(charge_reference="pi_nope", idempotency_key="r1")

    def test_a_refund_replay_refunds_once(self):
        tool = fake()
        charge = tool.charge(amount_cents=1_00, description="x",
                             idempotency_key="k1")
        tool.refund(charge_reference=charge.external_reference,
                    idempotency_key="r1")
        tool.refund(charge_reference=charge.external_reference,
                    idempotency_key="r1")
        assert len(tool.refunded) == 1

    def test_a_refund_requires_a_key(self):
        tool = fake()
        charge = tool.charge(amount_cents=1_00, description="x",
                             idempotency_key="k1")
        with pytest.raises(ValueError):
            tool.refund(charge_reference=charge.external_reference,
                        idempotency_key="")


class RecordingTransport:
    """Stands in for HTTPS. Records every request and plays back canned Stripe
    responses, so the adapter's wire format is tested without a network."""

    def __init__(self, *responses):
        self.requests = []
        self._responses = list(responses)

    def __call__(self, *, method, url, data, headers):
        self.requests.append(
            {"method": method, "url": url, "data": data, "headers": headers})
        return self._responses.pop(0)


def intent_succeeded(ref="pi_123", amount=13_50):
    return (200, {"id": ref, "object": "payment_intent", "amount": amount,
                  "currency": "usd", "status": "succeeded"})


class TestStripeAdapter:
    """The wire format, checked offline. Stripe's API is form-encoded HTTPS;
    the adapter builds those requests and distrusts what comes back."""

    def adapter(self, transport, **kwargs):
        return StripePaymentTool(api_key="sk_test_abc",
                                 payment_method="pm_card", transport=transport,
                                 **kwargs)

    def test_a_charge_posts_a_payment_intent(self):
        transport = RecordingTransport(intent_succeeded())
        self.adapter(transport).charge(amount_cents=13_50, description="Order",
                                       idempotency_key="k1")
        request = transport.requests[0]
        assert request["method"] == "POST"
        assert request["url"] == "https://api.stripe.com/v1/payment_intents"

    def test_the_amount_goes_over_the_wire_as_integer_cents(self):
        transport = RecordingTransport(intent_succeeded())
        self.adapter(transport).charge(amount_cents=13_50, description="Order",
                                       idempotency_key="k1")
        assert transport.requests[0]["data"]["amount"] == "1350"

    def test_the_charge_confirms_immediately_off_session(self):
        # The Quartermaster charges a saved card with nobody at a keyboard.
        transport = RecordingTransport(intent_succeeded())
        self.adapter(transport).charge(amount_cents=13_50, description="Order",
                                       idempotency_key="k1")
        data = transport.requests[0]["data"]
        assert data["confirm"] == "true"
        assert data["off_session"] == "true"
        assert data["payment_method"] == "pm_card"

    def test_the_idempotency_key_travels_as_the_stripe_header(self):
        transport = RecordingTransport(intent_succeeded())
        self.adapter(transport).charge(amount_cents=13_50, description="Order",
                                       idempotency_key="k1")
        assert transport.requests[0]["headers"]["Idempotency-Key"] == "k1"

    def test_the_api_key_travels_as_a_bearer_token(self):
        transport = RecordingTransport(intent_succeeded())
        self.adapter(transport).charge(amount_cents=13_50, description="Order",
                                       idempotency_key="k1")
        auth = transport.requests[0]["headers"]["Authorization"]
        assert auth == "Bearer sk_test_abc"

    def test_a_succeeded_intent_becomes_a_charge(self):
        transport = RecordingTransport(intent_succeeded(ref="pi_777",
                                                        amount=13_50))
        charge = self.adapter(transport).charge(amount_cents=13_50,
                                                description="Order",
                                                idempotency_key="k1")
        assert charge.external_reference == "pi_777"
        assert charge.amount_cents == 13_50

    def test_a_card_decline_raises_the_decline_type(self):
        transport = RecordingTransport(
            (402, {"error": {"type": "card_error", "code": "card_declined",
                             "message": "Your card was declined."}}))
        with pytest.raises(PaymentDeclined, match="declined"):
            self.adapter(transport).charge(amount_cents=13_50,
                                           description="Order",
                                           idempotency_key="k1")

    def test_any_other_error_raises_payment_error(self):
        transport = RecordingTransport(
            (500, {"error": {"message": "Something went wrong."}}))
        with pytest.raises(PaymentError):
            self.adapter(transport).charge(amount_cents=13_50,
                                           description="Order",
                                           idempotency_key="k1")

    def test_an_intent_that_is_not_succeeded_is_not_a_charge(self):
        # `requires_action` means a human has to authenticate. Off-session
        # there is no human, so treating it as paid would ship goods nobody
        # paid for.
        transport = RecordingTransport(
            (200, {"id": "pi_1", "amount": 13_50, "currency": "usd",
                   "status": "requires_action"}))
        with pytest.raises(PaymentError, match="requires_action"):
            self.adapter(transport).charge(amount_cents=13_50,
                                           description="Order",
                                           idempotency_key="k1")

    def test_a_refund_posts_against_the_payment_intent(self):
        transport = RecordingTransport(
            (200, {"id": "re_1", "amount": 13_50, "status": "succeeded"}))
        refund = self.adapter(transport).refund(charge_reference="pi_777",
                                                idempotency_key="r1")
        request = transport.requests[0]
        assert request["url"] == "https://api.stripe.com/v1/refunds"
        assert request["data"]["payment_intent"] == "pi_777"
        assert refund.amount_cents == 13_50

    def test_the_adapter_refuses_floats_before_the_wire(self):
        # Stripe would accept "1350.0"; this codebase must not get that far.
        transport = RecordingTransport(intent_succeeded())
        with pytest.raises(TypeError):
            self.adapter(transport).charge(amount_cents=13.5,
                                           description="Order",
                                           idempotency_key="k1")
        assert transport.requests == []

    def test_a_missing_api_key_is_refused_at_construction(self):
        # Failing at checkout time would strand an approved order.
        with pytest.raises(ValueError):
            StripePaymentTool(api_key="", payment_method="pm_card",
                              transport=lambda **k: (200, {}))


class TestChargeAndRefundAreValues:
    def test_amounts_stay_integers(self):
        charge = fake().charge(amount_cents=999, description="x",
                               idempotency_key="k1")
        assert isinstance(charge.amount_cents, int)
        assert isinstance(charge, Charge)

    def test_a_refund_is_a_value_too(self):
        tool = fake()
        charge = tool.charge(amount_cents=999, description="x",
                             idempotency_key="k1")
        refund = tool.refund(charge_reference=charge.external_reference,
                             idempotency_key="r1")
        assert isinstance(refund, Refund)
        assert isinstance(refund.amount_cents, int)
