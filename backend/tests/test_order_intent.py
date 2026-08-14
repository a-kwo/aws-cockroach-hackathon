"""Turning "order 20lb of tomatoes and a case of olive oil" into a cart.

A model does the reading, which means everything it returns is untrusted input.
It can hallucinate an item, invent a quantity, mangle a number, or decide that a
question about last month's revenue was a request to buy something. So the
parsing is generous and the validation is not: anything malformed stops here
rather than reaching a cart.

Note what is deliberately *not* checked — an implausibly large quantity. There
is no arbitrary ceiling on "how many tomatoes is too many", because the real
protection is downstream: the cart gets priced and the spend limit refuses it.
Guessing a limit here would only add a second, worse one.
"""

import pytest

from brasstacks.order_intent import NotAnOrderRequest, parse_order_request
from brasstacks.agents.quartermaster import TRIGGER_OWNER_INSTRUCTION


class StubReasoner:
    """Returns whatever the test says the model said."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, *, system, user, schema, max_tokens=None, images=()):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def said(**payload):
    base = {"is_order_request": True, "items": [], "category": None}
    base.update(payload)
    return StubReasoner(base)


class TestPlainRequests:
    def test_one_item_with_a_quantity(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 3}])
        request = parse_order_request("order 3 boxes of tomatoes",
                                      reasoner=reasoner)
        assert request.items == (("tomatoes", 3),)

    def test_several_items(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 3},
                               {"name": "olive oil", "quantity": 1}])
        request = parse_order_request("tomatoes and oil", reasoner=reasoner)
        assert request.items == (("tomatoes", 3), ("olive oil", 1))

    def test_it_is_tagged_as_coming_from_the_owner(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 1}])
        request = parse_order_request("get tomatoes", reasoner=reasoner)
        assert request.trigger == TRIGGER_OWNER_INSTRUCTION

    def test_the_original_words_are_kept(self):
        # Provenance: when the owner is asked to approve, they should see what
        # they actually typed, not the model's paraphrase of it.
        reasoner = said(items=[{"name": "tomatoes", "quantity": 1}])
        request = parse_order_request("get me some toms", reasoner=reasoner)
        assert request.note == "get me some toms"

    def test_a_category_is_carried_through(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 1}],
                        category="produce")
        request = parse_order_request("tomatoes", reasoner=reasoner)
        assert request.category == "produce"


class TestNormalisation:
    def test_item_names_are_lowercased_and_trimmed(self):
        reasoner = said(items=[{"name": "  Tomatoes  ", "quantity": 1}])
        request = parse_order_request("tomatoes", reasoner=reasoner)
        assert request.items == (("tomatoes", 1),)

    def test_a_missing_quantity_defaults_to_one(self):
        # "Order some olive oil" has no number in it. One is the smallest
        # useful assumption and the spend limit still sees the price.
        reasoner = said(items=[{"name": "olive oil"}])
        request = parse_order_request("order olive oil", reasoner=reasoner)
        assert request.items == (("olive oil", 1),)


class TestNotAnOrder:
    def test_a_question_is_refused(self):
        reasoner = said(is_order_request=False)
        with pytest.raises(NotAnOrderRequest):
            parse_order_request("how did we do last month?", reasoner=reasoner)

    def test_an_order_request_with_no_items_is_refused(self):
        # The model said yes but named nothing. That is not something to act on.
        reasoner = said(is_order_request=True, items=[])
        with pytest.raises(NotAnOrderRequest):
            parse_order_request("order something", reasoner=reasoner)

    def test_empty_input_is_refused_without_calling_the_model(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 1}])
        with pytest.raises(ValueError):
            parse_order_request("   ", reasoner=reasoner)
        assert reasoner.calls == []


class TestTheModelIsUntrusted:
    """Every one of these is something a model does eventually."""

    def test_a_zero_quantity_is_refused(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 0}])
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_a_negative_quantity_is_refused(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": -3}])
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_a_fractional_quantity_is_refused(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 2.5}])
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_a_numeric_string_quantity_is_accepted(self):
        # Models return "3" instead of 3 constantly. This one is worth being
        # generous about, because the value is unambiguous.
        reasoner = said(items=[{"name": "tomatoes", "quantity": "3"}])
        request = parse_order_request("tomatoes", reasoner=reasoner)
        assert request.items == (("tomatoes", 3),)

    def test_a_non_numeric_quantity_is_refused(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": "a few"}])
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_a_nameless_item_is_refused(self):
        reasoner = said(items=[{"name": "  ", "quantity": 1}])
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_a_non_object_item_is_refused(self):
        reasoner = said(items=["tomatoes"])
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_items_not_being_a_list_is_refused(self):
        reasoner = said(items={"name": "tomatoes", "quantity": 1})
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_a_missing_verdict_field_is_refused(self):
        reasoner = StubReasoner({"items": [{"name": "x", "quantity": 1}]})
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=reasoner)

    def test_a_non_dict_response_is_refused(self):
        with pytest.raises(ValueError):
            parse_order_request("tomatoes", reasoner=StubReasoner("nope"))

    def test_duplicate_items_are_merged(self):
        # "tomatoes and more tomatoes" must not become two lines that each pass
        # a per-item check while together exceeding what the owner meant.
        reasoner = said(items=[{"name": "tomatoes", "quantity": 2},
                               {"name": "Tomatoes", "quantity": 3}])
        request = parse_order_request("tomatoes", reasoner=reasoner)
        assert request.items == (("tomatoes", 5),)


class TestModelFailure:
    def test_a_model_error_propagates(self):
        # Not swallowed into "no items". A failed parse must not look like a
        # successful parse of nothing.
        reasoner = StubReasoner(RuntimeError("bedrock down"))
        with pytest.raises(RuntimeError):
            parse_order_request("tomatoes", reasoner=reasoner)


class TestTheSchemaIsApiLegal:
    def test_every_object_forbids_additional_properties(self):
        """The Anthropic structured-output API rejects any 'object' whose
        schema does not explicitly set additionalProperties to false. This
        one omission 400'd every live parse — silently, because the ask
        handler deliberately falls back to the keyword parser on model
        failure — so owners could only order the twelve catalogue words
        while every test still passed."""
        from brasstacks.order_intent import ORDER_INTENT_SCHEMA

        def walk(node, path="$"):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, (
                        f"object at {path} must set additionalProperties "
                        "to False")
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(ORDER_INTENT_SCHEMA)


class TestThePrompt:
    def test_the_owners_words_reach_the_model(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 1}])
        parse_order_request("order 20lb of tomatoes", reasoner=reasoner)
        assert "20lb of tomatoes" in reasoner.calls[0]["user"]

    def test_a_schema_is_supplied(self):
        reasoner = said(items=[{"name": "tomatoes", "quantity": 1}])
        parse_order_request("tomatoes", reasoner=reasoner)
        assert reasoner.calls[0]["schema"]["type"] == "object"
