"""The Zinc adapter: real retail orders through api.zinc.com.

Same discipline as the Stripe tests — the transport is a recording fake, so
every assertion here is about the exact bytes Zinc would receive and the
exact honesty of what we tell the owner afterwards. No network, ever.

Zinc's ordering call is asynchronous: POST returns an order that moves
pending → in_progress → order_placed (or order_failed) on its own clock.
Amazon adds tax and shipping the search price cannot know, so a draft
carries an explicit allowance in ``fees_cents`` and the order is capped at
the cart total via ``max_price`` — the owner approves a ceiling, and the
receipt reports what is actually known.

(This adapter originally spoke the classic api.zinc.io v1 dialect — Basic
auth, product_ids, request_id polling. The owner's account turned out to
live on api.zinc.com, a different wire format entirely: Bearer tokens,
product URLs, native idempotency keys. The seam held; only this file and
the adapter changed.)
"""

import uuid

import pytest

from brasstacks.ordering import (
    ItemUnavailable,
    OrderingError,
    ZincOrderingTool,
    zinc_shipping_address,
)

ADDRESS = {
    "first_name": "Maya",
    "last_name": "Kwon",
    "address_line1": "1 Harbor Way",
    "city": "San Francisco",
    "state": "CA",
    "postal_code": "94107",
    "phone_number": "4155550134",
    "country": "US",
}


class ZincTransport:
    """Scripted responses keyed by ``METHOD path-fragment``, FIFO per key.

    The last response under a key sticks, so a poll loop can be scripted as
    two ``in_progress`` bodies followed by the final ``order_placed``.
    """

    def __init__(self):
        self.requests = []
        self._routes = {}

    def queue(self, method, fragment, status, body):
        self._routes.setdefault((method, fragment), []).append((status, body))

    def __call__(self, *, method, url, json_body, headers):
        self.requests.append({"method": method, "url": url,
                              "json": json_body, "headers": headers})
        for (verb, fragment), responses in self._routes.items():
            if verb == method and fragment in url:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"no scripted response for {method} {url}")


def search_hit(title="Tork Xpressnap Dispenser Napkins",
               product_id="B001BQVCDQ", price=69_95):
    return {"status": "completed",
            "results": [{"product_id": product_id, "title": title,
                         "price": price, "url": None}]}


def order_body(status="order_placed", order_id="ord-1", **extra):
    return {"id": order_id, "status": status, "max_price": 83_94,
            "items": [], **extra}


def tool(transport, **kwargs):
    kwargs.setdefault("client_token", "zn_test_token")
    kwargs.setdefault("shipping_address", dict(ADDRESS))
    kwargs.setdefault("sleeper", lambda seconds: None)
    return ZincOrderingTool(transport=transport, **kwargs)


class TestShippingAddressAutofill:
    """The ship-to comes from the owner's own signup, not a config file.

    The business row already holds the typed address (``city``) and the
    business name; ``zinc_shipping_address`` turns them into the structured
    address Zinc demands, or refuses with a sentence the owner can act on.
    Deterministic parsing only — a guessed delivery address spends real
    money at a wrong door.
    """

    def test_a_typed_address_becomes_a_structured_one(self):
        address = zinc_shipping_address(
            business_name="Harborview Japanese",
            address_text="1 Harbor Way, San Francisco, CA 94107")
        assert address == {
            "first_name": "Harborview",
            "last_name": "Japanese",
            "address_line1": "1 Harbor Way",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94107",
            "country": "US",
        }

    def test_a_suite_line_stays_with_the_street(self):
        address = zinc_shipping_address(
            business_name="Harborview Japanese",
            address_text="1 Harbor Way, Suite 2, San Francisco, CA 94107")
        assert address["address_line1"] == "1 Harbor Way, Suite 2"
        assert address["city"] == "San Francisco"

    def test_a_single_word_business_still_fills_both_name_fields(self):
        address = zinc_shipping_address(
            business_name="Rosa's",
            address_text="12 Vine St, Portland, OR 97209")
        assert address["first_name"] == "Rosa's"
        assert address["last_name"] == "Receiving"

    def test_zip_plus_four_and_lowercase_state_normalise(self):
        address = zinc_shipping_address(
            business_name="Rosa's Kitchen",
            address_text="12 Vine St, Portland, or 97209-1402")
        assert address["state"] == "OR"
        assert address["postal_code"] == "97209"

    def test_a_trailing_country_is_tolerated(self):
        address = zinc_shipping_address(
            business_name="Rosa's Kitchen",
            address_text="12 Vine St, Portland, OR 97209, USA")
        assert address["city"] == "Portland"

    def test_a_vague_address_is_refused_with_the_remedy(self):
        with pytest.raises(ValueError) as caught:
            zinc_shipping_address(business_name="Rosa's Kitchen",
                                  address_text="Portland, somewhere nice")
        message = str(caught.value)
        assert "Profile" in message
        assert "ZIP" in message

    def test_a_missing_address_is_refused_the_same_way(self):
        with pytest.raises(ValueError):
            zinc_shipping_address(business_name="Rosa's Kitchen",
                                  address_text=None)

    def test_a_phone_rides_along_when_the_business_has_one(self):
        """Zinc requires a delivery contact number. The tenant's own phone
        wins when the profile holds one; the punctuation people type is
        stripped to digits."""
        address = zinc_shipping_address(
            business_name="Rosa's Kitchen",
            address_text="12 Vine St, Portland, OR 97209",
            phone="(415) 555-0134")
        assert address["phone_number"] == "4155550134"

    def test_no_phone_means_no_phone_field(self):
        """Absent beats invented: the deployment-level fallback is added by
        the handler, and a missing number must fail loudly at the adapter
        rather than ship with a made-up contact."""
        address = zinc_shipping_address(
            business_name="Rosa's Kitchen",
            address_text="12 Vine St, Portland, OR 97209")
        assert "phone_number" not in address


class TestConstruction:
    def test_a_client_token_is_required(self):
        with pytest.raises(ValueError):
            ZincOrderingTool(client_token="  ", shipping_address=ADDRESS)

    def test_the_shipping_address_must_be_complete(self):
        """Zinc rejects a partial address at order time — hours after the
        owner approved. Failing at construction moves the error to deploy
        time, where a human is looking."""
        broken = dict(ADDRESS)
        del broken["postal_code"]
        with pytest.raises(ValueError) as caught:
            ZincOrderingTool(client_token="tok", shipping_address=broken)
        assert "postal_code" in str(caught.value)

    def test_a_missing_phone_fails_at_construction_not_checkout(self):
        """Zinc's Address schema requires phone_number. The live run that
        taught us this failed at approval time with a bare 'Field
        required' — the worst moment and the worst message."""
        broken = dict(ADDRESS)
        del broken["phone_number"]
        with pytest.raises(ValueError) as caught:
            ZincOrderingTool(client_token="tok", shipping_address=broken)
        assert "phone_number" in str(caught.value)


class TestDraft:
    def test_the_search_asks_the_retailer_with_a_bearer_token(self):
        transport = ZincTransport()
        transport.queue("GET", "/products/search", 200, search_hit())
        tool(transport).draft(items=[("napkins", 2)])
        request = transport.requests[0]
        assert "https://api.zinc.com/products/search" in request["url"]
        assert "query=napkins" in request["url"]
        assert "retailer=amazon" in request["url"]
        assert request["headers"]["Authorization"] == "Bearer zn_test_token"

    def test_the_line_names_the_actual_product_not_the_wish(self):
        """The owner approves what will really be bought. 'napkins' is a
        wish; the listing title is the product — and because the name is in
        the fingerprint, a different search result at approval time stops
        the order for a fresh look."""
        transport = ZincTransport()
        transport.queue("GET", "/products/search", 200, search_hit())
        cart = tool(transport).draft(items=[("napkins", 2)])
        assert cart.lines[0].name.startswith("Tork Xpressnap")
        assert cart.lines[0].quantity == 2
        assert cart.lines[0].unit_price_cents == 69_95

    def test_the_fees_line_is_the_tax_and_shipping_allowance(self):
        transport = ZincTransport()
        transport.queue("GET", "/products/search", 200, search_hit())
        cart = tool(transport).draft(items=[("napkins", 2)])
        # 15% of the 139.90 subtotal, rounded up: the ceiling the owner
        # approves, not a guess presented as a price.
        assert cart.subtotal_cents == 139_90
        assert cart.fees_cents == 20_99
        assert cart.total_cents == 160_89

    def test_no_results_is_an_item_unavailable(self):
        transport = ZincTransport()
        transport.queue("GET", "/products/search", 200,
                        {"status": "completed", "results": []})
        with pytest.raises(ItemUnavailable):
            tool(transport).draft(items=[("unobtainium", 1)])

    def test_an_unpriced_result_is_skipped_for_the_next_priced_one(self):
        transport = ZincTransport()
        transport.queue("GET", "/products/search", 200, {
            "status": "completed",
            "results": [
                {"title": "Sponsored thing", "product_id": "B0AD",
                 "price": None},
                {"title": "Real thing", "product_id": "B0REAL",
                 "price": 5_00},
            ]})
        cart = tool(transport).draft(items=[("thing", 1)])
        assert cart.lines[0].name == "Real thing"

    def test_a_search_failure_says_so(self):
        transport = ZincTransport()
        transport.queue("GET", "/products/search", 500,
                        {"detail": "internal search error"})
        with pytest.raises(OrderingError):
            tool(transport).draft(items=[("napkins", 1)])

    def test_an_empty_cart_is_refused(self):
        with pytest.raises(ValueError):
            tool(ZincTransport()).draft(items=[])

    def test_a_bad_quantity_is_refused(self):
        transport = ZincTransport()
        transport.queue("GET", "/products/search", 200, search_hit())
        with pytest.raises(ValueError):
            tool(transport).draft(items=[("napkins", 0)])


class TestPlace:
    def drafted(self, transport, **kwargs):
        transport.queue("GET", "/products/search", 200, search_hit())
        adapter = tool(transport, **kwargs)
        return adapter, adapter.draft(items=[("napkins", 2)])

    def test_the_order_carries_the_product_url_and_the_ceiling(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 200,
                        order_body(status="order_placed", order_id="ord-9"))
        adapter.place(cart=cart, idempotency_key="approve:o1")

        posted = next(r for r in transport.requests if r["method"] == "POST")
        body = posted["json"]
        assert body["products"] == [
            {"url": "https://www.amazon.com/dp/B001BQVCDQ", "quantity": 2}]
        assert body["max_price"] == cart.total_cents
        assert body["shipping_address"] == ADDRESS
        assert body["metadata"]["idempotency_key"] == "approve:o1"

    def test_the_wire_idempotency_key_is_a_stable_uuid(self):
        """Zinc caps idempotency_key at 36 characters; ours ('approve:' + a
        UUID) are longer. The wire key is a UUID5 of the logical key — the
        same order retried sends the same UUID, two different orders never
        collide, and the original key rides in metadata for the audit
        trail."""
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 200, order_body())
        adapter.place(cart=cart, idempotency_key="approve:" + "x" * 36)
        body = next(r for r in transport.requests
                    if r["method"] == "POST")["json"]
        wire_key = body["idempotency_key"]
        assert len(wire_key) == 36
        assert str(uuid.UUID(wire_key)) == wire_key
        assert wire_key == str(uuid.uuid5(uuid.NAMESPACE_URL,
                                          "brasstacks:approve:" + "x" * 36))

    def test_an_immediately_placed_order_is_a_placed_receipt(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 200,
                        order_body(status="order_placed", order_id="ord-2"))
        receipt = adapter.place(cart=cart, idempotency_key="k1")
        assert receipt.status == "placed"
        assert receipt.external_reference == "zinc:ord-2"

    def test_a_pending_order_is_polled_to_its_outcome(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 200,
                        order_body(status="pending", order_id="ord-3"))
        transport.queue("GET", "/orders/ord-3", 200,
                        order_body(status="in_progress", order_id="ord-3"))
        transport.queue("GET", "/orders/ord-3", 200,
                        order_body(status="order_placed", order_id="ord-3"))
        receipt = adapter.place(cart=cart, idempotency_key="k1")
        assert receipt.status == "placed"
        assert receipt.external_reference == "zinc:ord-3"

    def test_still_confirming_after_the_polls_is_said_not_hidden(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport, poll_attempts=3)
        transport.queue("POST", "/orders", 200,
                        order_body(status="pending", order_id="ord-4"))
        transport.queue("GET", "/orders/ord-4", 200,
                        order_body(status="in_progress", order_id="ord-4"))
        receipt = adapter.place(cart=cart, idempotency_key="k1")
        assert receipt.status == "processing"
        assert receipt.external_reference == "zinc:ord-4"
        # Nothing better is known yet than the approved ceiling.
        assert receipt.total_cents == cart.total_cents
        # poll_attempts counts status inspections; the creation response is
        # the first, so three attempts cost two follow-up GETs.
        polls = [r for r in transport.requests
                 if r["method"] == "GET" and "/orders/ord-4" in r["url"]]
        assert len(polls) == 2

    def test_an_order_failed_status_surfaces_as_an_ordering_error(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 200,
                        order_body(status="pending", order_id="ord-5"))
        transport.queue("GET", "/orders/ord-5", 200,
                        order_body(status="order_failed", order_id="ord-5",
                                   job_result={"code": "out_of_stock"}))
        with pytest.raises(OrderingError) as caught:
            adapter.place(cart=cart, idempotency_key="k1")
        assert "out_of_stock" in str(caught.value)
        assert "zinc:ord-5" in str(caught.value)

    def test_a_rejected_post_surfaces_the_reason(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 400,
                        {"detail": "shipping_address is invalid"})
        with pytest.raises(OrderingError) as caught:
            adapter.place(cart=cart, idempotency_key="k1")
        assert "shipping_address is invalid" in str(caught.value)

    def test_a_validation_error_names_the_field_not_just_the_rule(self):
        """FastAPI validation details carry the field path in 'loc'. The
        first live order failed as exactly 'Field required' — true, and
        useless. The message must say WHICH field."""
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 422, {"detail": [
            {"type": "missing",
             "loc": ["body", "shipping_address", "phone_number"],
             "msg": "Field required"}]})
        with pytest.raises(OrderingError) as caught:
            adapter.place(cart=cart, idempotency_key="k1")
        assert "shipping_address.phone_number" in str(caught.value)
        assert "Field required" in str(caught.value)

    def test_a_foreign_cart_is_refused_not_guessed_at(self):
        """A cart this adapter never priced has no resolved product URLs.
        Guessing URLs from names would order the wrong things with real
        money; refusing costs a re-draft."""
        from brasstacks.ordering import FakeOrderingTool
        transport = ZincTransport()
        foreign = FakeOrderingTool(
            catalogue={"napkins": 4_50}).draft(items=[("napkins", 1)])
        with pytest.raises(OrderingError):
            tool(transport).place(cart=foreign, idempotency_key="k1")

    def test_a_replayed_key_returns_the_original_receipt(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        transport.queue("POST", "/orders", 200,
                        order_body(status="order_placed", order_id="ord-6"))
        first = adapter.place(cart=cart, idempotency_key="k1")
        again = adapter.place(cart=cart, idempotency_key="k1")
        assert again is first
        posts = [r for r in transport.requests if r["method"] == "POST"]
        assert len(posts) == 1

    def test_a_missing_key_is_refused(self):
        transport = ZincTransport()
        adapter, cart = self.drafted(transport)
        with pytest.raises(ValueError):
            adapter.place(cart=cart, idempotency_key="  ")
