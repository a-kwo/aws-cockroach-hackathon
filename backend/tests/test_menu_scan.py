"""The menu scan: photos in, priced menu rows in CockroachDB out.

Three things here are worth guarding, and each one is a claim about the
owner's money or the agent's memory:

1. **Prices are integer cents, end to end.** A menu photo is the first place
   real money enters the system, and the model reads it as text. "$14.00" must
   land as 1400, never as 14.0, and a price the model invents out of a blurry
   photo must be rejected rather than averaged into a forecast.

2. **The client is not trusted.** The scan endpoint returns parsed items for
   the owner to correct, and the corrected list comes back on the signup POST.
   Anything arriving that way is re-validated from scratch — otherwise a
   hand-edited payload writes arbitrary cents into the business profile.

3. **Menu items reach the retrieval corpus.** The Analyst vector-searches
   `observation`, not `business_fact`, so a menu that only became a fact would
   be invisible to every hypothesis query the Analyst asks at night.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)

# A one-pixel PNG. Small enough to inline, real enough to base64-decode.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


def an_image(media_type: str = "image/png", data: str | None = None) -> dict:
    return {"media_type": media_type, "data": PNG_B64 if data is None else data}


# A model response in the shape MENU_SCHEMA demands.
MODEL_MENU = {
    "currency": "USD",
    "sections": [
        {
            "name": "Pizzas",
            "items": [
                {
                    "name": "Margherita",
                    "description": "San Marzano tomatoes, buffalo mozzarella",
                    "price_cents": 1400,
                    "price_note": None,
                },
                {
                    "name": "Diavola",
                    "description": None,
                    "price_cents": 1650,
                    "price_note": None,
                },
            ],
        },
        {
            "name": "Mains",
            "items": [
                {
                    "name": "Whole Branzino",
                    "description": "Wood-grilled, salsa verde",
                    "price_cents": None,
                    "price_note": "Market price",
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Image intake
# ---------------------------------------------------------------------------

class TestImageIntake:
    """What the browser uploads is untrusted bytes until proven otherwise.

    The camera on a modern phone produces 3-6MB JPEGs and API Gateway rejects
    the request body at 10MB, so the browser downscales before upload. These
    checks are the server refusing to rely on that having happened.
    """

    def test_a_plain_image_survives_intake(self):
        from brasstacks.menu import normalise_images

        images = normalise_images([an_image()])

        assert len(images) == 1
        assert images[0].media_type == "image/png"
        assert images[0].data == PNG_B64

    def test_a_data_url_prefix_is_stripped(self):
        """`canvas.toDataURL()` returns `data:image/jpeg;base64,...`.

        Sending that straight through puts the prefix inside the base64 field
        and Anthropic rejects the whole message. Cheaper to strip it here than
        to make every caller remember.
        """
        from brasstacks.menu import normalise_images

        payload = [{"media_type": "image/jpeg",
                    "data": f"data:image/jpeg;base64,{PNG_B64}"}]

        assert normalise_images(payload)[0].data == PNG_B64

    def test_the_media_type_comes_from_the_data_url_when_absent(self):
        from brasstacks.menu import normalise_images

        payload = [{"data": f"data:image/webp;base64,{PNG_B64}"}]

        assert normalise_images(payload)[0].media_type == "image/webp"

    def test_a_pdf_is_refused(self):
        """Menu PDFs are a real thing owners have, but they are not images.

        Refusing loudly beats sending a document block the vision path was not
        built for and getting an opaque provider error.
        """
        from brasstacks.menu import MenuError, normalise_images

        with pytest.raises(MenuError, match="image/pdf|not a supported"):
            normalise_images([an_image(media_type="application/pdf")])

    def test_undecodable_base64_is_refused(self):
        from brasstacks.menu import MenuError, normalise_images

        with pytest.raises(MenuError, match="base64"):
            normalise_images([an_image(data="not base64 at all!!")])

    def test_no_images_is_refused(self):
        from brasstacks.menu import MenuError, normalise_images

        with pytest.raises(MenuError, match="at least one"):
            normalise_images([])

    def test_too_many_images_is_refused(self):
        """Every image is input tokens on a Claude call the owner did not pay for."""
        from brasstacks.menu import MAX_IMAGES, MenuError, normalise_images

        with pytest.raises(MenuError, match="at most"):
            normalise_images([an_image()] * (MAX_IMAGES + 1))

    def test_an_oversized_image_is_refused(self):
        from brasstacks.menu import MAX_IMAGE_BYTES, MenuError, normalise_images

        huge = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")

        with pytest.raises(MenuError, match="too large"):
            normalise_images([an_image(data=huge)])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParseMenu:
    def test_the_images_reach_the_model(self):
        """The whole feature is the image getting to Claude.

        A regression where `images` is dropped would still return a plausible
        menu — the model would invent one from the prompt alone — so this
        asserts on the call, not the result.
        """
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import FakeReasoner

        # One call per photo since the scan was split to fit the 30s ceiling,
        # so two photos are two calls carrying one image each — not one call
        # carrying two. The point of the test is unchanged: a regression that
        # dropped `images` would still return a plausible menu invented from the
        # prompt, so this asserts on the call rather than the result.
        reasoner = FakeReasoner([MODEL_MENU, MODEL_MENU])
        parse_menu(normalise_images([an_image(), an_image()]), reasoner=reasoner)

        assert len(reasoner.calls) == 2
        assert all(len(call["images"]) == 1 for call in reasoner.calls)
        assert reasoner.calls[0]["images"][0].data == PNG_B64

    def test_items_carry_their_section_and_price(self):
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import FakeReasoner

        menu = parse_menu(normalise_images([an_image()]),
                          reasoner=FakeReasoner([MODEL_MENU]))

        assert [i.name for i in menu.items] == ["Margherita", "Diavola", "Whole Branzino"]
        assert menu.items[0].price_cents == 1400
        assert menu.items[0].section == "Pizzas"
        assert menu.items[2].section == "Mains"

    def test_a_priceless_item_keeps_its_note(self):
        """"Market price" is information, not a missing value."""
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import FakeReasoner

        menu = parse_menu(normalise_images([an_image()]),
                          reasoner=FakeReasoner([MODEL_MENU]))

        assert menu.items[2].price_cents is None
        assert menu.items[2].price_note == "Market price"

    def test_an_empty_menu_is_refused(self):
        """A photo of a wall parses to nothing. Say so rather than storing nothing."""
        from brasstacks.menu import MenuError, normalise_images, parse_menu
        from brasstacks.providers import FakeReasoner

        reasoner = FakeReasoner([{"currency": "USD", "sections": []}])

        with pytest.raises(MenuError, match="no menu items"):
            parse_menu(normalise_images([an_image()]), reasoner=reasoner)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

class TestPricesAreIntegerCents:
    """The highest-risk code in the feature.

    Mirrors the discipline in `finds._require_cents`: the model is reading
    digits off a photograph, and a unit slip here becomes a forecast the owner
    is told to trust.
    """

    def _menu_with_price(self, value):
        return {
            "currency": "USD",
            "sections": [{"name": "Mains", "items": [
                {"name": "Steak", "description": None,
                 "price_cents": value, "price_note": None},
            ]}],
        }

    def _parse(self, value):
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import FakeReasoner

        return parse_menu(normalise_images([an_image()]),
                          reasoner=FakeReasoner([self._menu_with_price(value)]))

    def test_a_whole_float_is_accepted_as_cents(self):
        """1400.0 is unambiguous; JSON round-trips make it common."""
        assert self._parse(1400.0).items[0].price_cents == 1400

    def test_a_fractional_cent_is_refused(self):
        from brasstacks.menu import MenuError

        with pytest.raises(MenuError, match="whole cents"):
            self._parse(1400.5)

    def test_a_string_price_is_refused(self):
        """A string means the model ignored the schema; guessing the unit is worse."""
        from brasstacks.menu import MenuError

        with pytest.raises(MenuError, match="number of integer cents"):
            self._parse("$14.00")

    def test_a_negative_price_is_refused(self):
        from brasstacks.menu import MenuError

        with pytest.raises(MenuError, match="non-negative"):
            self._parse(-100)

    def test_a_boolean_is_refused(self):
        """`bool` is an `int` subclass — True would otherwise price at 1 cent."""
        from brasstacks.menu import MenuError

        with pytest.raises(MenuError, match="number of integer cents"):
            self._parse(True)

    def test_an_implausible_price_is_refused(self):
        """14 dollars reported as 14 in the cents field is the failure to catch.

        The ceiling is what separates "expensive tasting menu" from "the model
        read the phone number".
        """
        from brasstacks.menu import MAX_ITEM_PRICE_CENTS, MenuError

        with pytest.raises(MenuError, match="implausible"):
            self._parse(MAX_ITEM_PRICE_CENTS + 1)

    def test_a_free_item_is_allowed(self):
        """Bread service at zero is a real menu line."""
        assert self._parse(0).items[0].price_cents == 0


# ---------------------------------------------------------------------------
# The round trip through the browser
# ---------------------------------------------------------------------------

class TestMenuFromPayload:
    """The owner corrects the scan before signing up, so the menu arrives twice.

    The second arrival is client-controlled JSON. Re-validating it through the
    same rules as the model output is the only thing standing between a hand
    edited request and arbitrary cents on the business profile.
    """

    def test_a_corrected_menu_is_accepted(self):
        from brasstacks.menu import menu_from_payload

        menu = menu_from_payload({
            "currency": "USD",
            "items": [{"name": "Margherita", "description": "Fixed by the owner",
                       "price_cents": 1500, "price_note": None, "section": "Pizzas"}],
        })

        assert menu.items[0].price_cents == 1500
        assert menu.items[0].description == "Fixed by the owner"

    def test_a_tampered_price_is_refused(self):
        from brasstacks.menu import MenuError, menu_from_payload

        with pytest.raises(MenuError, match="whole cents"):
            menu_from_payload({"currency": "USD", "items": [
                {"name": "Steak", "price_cents": 12.34, "section": "Mains"},
            ]})

    def test_an_absent_menu_is_not_an_error(self):
        """Scanning is optional. Skipping it must not fail signup."""
        from brasstacks.menu import menu_from_payload

        assert menu_from_payload(None) is None
        assert menu_from_payload({"currency": "USD", "items": []}) is None

    def test_an_unnamed_item_is_dropped_not_fatal(self):
        """A blank row left behind in the review UI should not block signup."""
        from brasstacks.menu import menu_from_payload

        menu = menu_from_payload({"currency": "USD", "items": [
            {"name": "  ", "price_cents": 100},
            {"name": "Margherita", "price_cents": 1400},
        ]})

        assert [i.name for i in menu.items] == ["Margherita"]


# ---------------------------------------------------------------------------
# What the agents actually read
# ---------------------------------------------------------------------------

class TestMenuReachesMemory:
    def _menu(self):
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import FakeReasoner

        return parse_menu(normalise_images([an_image()]),
                          reasoner=FakeReasoner([MODEL_MENU]))

    def test_each_item_becomes_a_retrievable_sentence(self):
        """Titan responds to sentences, not to key-value rows.

        Measured on this corpus: concrete, hypothesis-shaped text retrieves at
        ~0.58 similarity where abstract phrasing gets ~0.24. A menu row dumped
        as "Margherita|1400|Pizzas" would embed to noise.
        """
        from brasstacks.menu import menu_observations

        statements = menu_observations(self._menu())

        assert len(statements) == 3
        assert "Margherita" in statements[0]
        assert "$14.00" in statements[0]
        assert "Pizzas" in statements[0]
        assert statements[0].endswith(".")

    def test_a_priceless_item_says_so_rather_than_showing_zero(self):
        from brasstacks.menu import menu_observations

        branzino = [s for s in menu_observations(self._menu()) if "Branzino" in s][0]

        assert "Market price" in branzino
        assert "$0.00" not in branzino

    def test_the_price_range_becomes_a_standing_fact(self):
        """The Analyst reads facts wholesale, so the shape of the menu is
        available even when no vector query happens to hit a menu item."""
        from brasstacks.menu import menu_facts

        facts = " ".join(menu_facts(self._menu()))

        assert "$14.00" in facts and "$16.50" in facts
        assert "Pizzas" in facts and "Mains" in facts

    def test_money_never_becomes_a_float(self):
        """Guards the formatting path specifically.

        `cents / 100` reads correctly for $14.00 and silently produces
        $16.499999999999998 for other values.
        """
        from brasstacks.menu import format_cents

        assert format_cents(1650) == "$16.50"
        assert format_cents(5) == "$0.05"
        assert format_cents(0) == "$0.00"
        assert format_cents(123456) == "$1234.56"


# ---------------------------------------------------------------------------
# The scan endpoint
# ---------------------------------------------------------------------------

def a_session(repo):
    """Mint an account with a live session, as /register would have."""
    from brasstacks.auth import hash_password, issue_session_token

    account = repo.create_account(None, username="sam",
                                  password_hash=hash_password("x" * 20))
    token, fingerprint, expires = issue_session_token(now=NOW)
    repo.create_session(fingerprint, business_id=None,
                        account_id=account, expires_at=expires)
    return account, token


def a_scan_event(images, *, token=None):
    import json

    return {
        "rawPath": "/v1/onboarding/menu-scan",
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"Authorization": f"Bearer {token}"} if token else {},
        "body": json.dumps({"images": images}),
    }


class TestScanEndpoint:
    def _call(self, images, *, reasoner, repo=None, authed=True):
        from brasstacks.handlers.menu_scan import scan_menu
        from brasstacks.repository import InMemoryRepository

        repo = repo if repo is not None else InMemoryRepository()
        token = a_session(repo)[1] if authed else None
        return scan_menu(a_scan_event(images, token=token),
                         repo=repo, reasoner=reasoner, now=NOW)

    def test_a_scan_returns_items_for_review(self):
        """The endpoint parses and returns; it writes nothing.

        The business row does not exist yet at this point in signup, so there
        is nothing to attach a menu to. The owner corrects the list and it
        arrives again on the signup POST.
        """
        import json

        from brasstacks.providers import FakeReasoner

        response = self._call([an_image()], reasoner=FakeReasoner([MODEL_MENU]))

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert [i["name"] for i in body["menu"]["items"]] == [
            "Margherita", "Diavola", "Whole Branzino",
        ]
        assert body["menu"]["items"][0]["price_cents"] == 1400

    def test_scanning_writes_nothing_to_the_database(self):
        from brasstacks.providers import FakeReasoner
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        self._call([an_image()], reasoner=FakeReasoner([MODEL_MENU]), repo=repo)

        assert repo._observations == []
        assert repo._facts == {}

    def test_a_stranger_cannot_spend_the_owners_model_budget(self):
        """Vision calls cost real money and this URL is in a public repo."""
        from brasstacks.providers import FakeReasoner

        reasoner = FakeReasoner([MODEL_MENU])
        response = self._call([an_image()], reasoner=reasoner, authed=False)

        assert response["statusCode"] == 401
        assert reasoner.calls == []

    def test_a_bad_photo_is_a_400_not_a_500(self):
        from brasstacks.providers import FakeReasoner

        response = self._call([an_image(media_type="application/pdf")],
                              reasoner=FakeReasoner([]))

        assert response["statusCode"] == 400

    def test_an_unreadable_menu_says_so(self):
        """The owner photographed the wall. Tell them, do not 500."""
        import json

        from brasstacks.providers import FakeReasoner

        response = self._call([an_image()],
                              reasoner=FakeReasoner([{"currency": "USD", "sections": []}]))

        assert response["statusCode"] == 400
        assert "no menu items" in json.loads(response["body"])["error"]

    def test_a_model_refusal_is_not_a_crash(self):
        from brasstacks.providers import FakeReasoner, ModelRefusedError

        response = self._call([an_image()],
                              reasoner=FakeReasoner([ModelRefusedError("declined")]))

        assert response["statusCode"] == 502


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

PROFILE = {
    "owner": {"name": "Sam Reyes", "email": "sam@example.com"},
    "business": {
        "name": "Fig & Ash",
        "category": "restaurant_cafe",
        "categoryLabel": "Restaurant / cafe",
        "location": "18 Mill Lane, Bristol",
    },
    "buyers": {"segments": ["Local families"], "offers": ["Wood-fired pizza"],
               "channels": ["Walk-in"]},
    "objective": "Fill weekday lunches",
}

REVIEWED_MENU = {
    "currency": "USD",
    "items": [
        {"name": "Margherita", "description": "San Marzano tomatoes",
         "price_cents": 1400, "price_note": None, "section": "Pizzas"},
        {"name": "Whole Branzino", "description": None,
         "price_cents": None, "price_note": "Market price", "section": "Mains"},
    ],
}


class TestSignupStoresTheMenu:
    def _onboard(self, profile, repo=None):
        from brasstacks.handlers import onboarding
        from brasstacks.providers import FakeEmbedder
        from brasstacks.repository import InMemoryRepository

        repo = repo if repo is not None else InMemoryRepository()
        token = a_session(repo)[1]
        event = {
            "headers": {"Authorization": f"Bearer {token}"},
            "body": __import__("json").dumps({**profile, "inviteCode": "let-me-in"}),
        }
        response = onboarding.onboard(event, repo=repo, embedder=FakeEmbedder(),
                                      geocoder=None, now=NOW)
        return repo, response

    def test_menu_items_land_in_the_retrieval_corpus(self):
        """The point of the whole feature.

        `business_fact` is read wholesale but only `observation` is vector
        searched, so a menu that stopped at facts would be invisible to every
        hypothesis query the Analyst runs at night.
        """
        import json

        repo, response = self._onboard({**PROFILE, "menu": REVIEWED_MENU})
        business_id = json.loads(response["body"])["business_id"]

        rows = repo.all_observations(business_id, limit=50)
        menu_rows = [r for r in rows if r.statement_type == "menu_item"]

        assert len(menu_rows) == 2
        assert all(r.kind == "owner_upload" for r in menu_rows)
        assert any("Margherita" in r.content and "$14.00" in r.content
                   for r in menu_rows)

    def test_menu_observations_are_embedded(self):
        """An observation with no vector is invisible to the Analyst.

        Storing the row without embedding it would look like success at signup
        and fail silently every night afterwards.
        """
        import json

        from brasstacks.handlers import onboarding
        from brasstacks.providers import FakeEmbedder
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        embedder = FakeEmbedder()
        token = a_session(repo)[1]
        event = {"headers": {"Authorization": f"Bearer {token}"},
                 "body": json.dumps({**PROFILE, "menu": REVIEWED_MENU,
                                     "inviteCode": "let-me-in"})}
        onboarding.onboard(event, repo=repo, embedder=embedder,
                           geocoder=None, now=NOW)

        assert any("Margherita" in text for text in embedder.embedded)
        assert all(o.embedding is not None for o in repo._observations)

    def test_the_menu_shape_becomes_a_standing_fact(self):
        import json

        repo, response = self._onboard({**PROFILE, "menu": REVIEWED_MENU})
        business_id = json.loads(response["body"])["business_id"]

        facts = " ".join(repo.get_business_facts(business_id))

        assert "menu has 2 items" in facts
        assert "Pizzas" in facts

    def test_prices_are_stored_as_integer_cents(self):
        """The JSONB copy is canonical. Prose is derived from it, never the reverse."""
        import json

        repo, response = self._onboard({**PROFILE, "menu": REVIEWED_MENU})
        business_id = json.loads(response["body"])["business_id"]

        stored = repo._businesses[business_id]["profile_data"]["menu"]

        assert stored["items"][0]["price_cents"] == 1400
        assert isinstance(stored["items"][0]["price_cents"], int)
        assert stored["items"][1]["price_cents"] is None
        assert stored["items"][1]["price_note"] == "Market price"

    def test_skipping_the_scan_still_signs_you_up(self):
        """Scanning is optional and a service business has no menu at all."""
        import json

        repo, response = self._onboard(PROFILE)

        assert response["statusCode"] == 201
        business_id = json.loads(response["body"])["business_id"]
        assert repo.all_observations(business_id, limit=50) == []

    def test_a_tampered_price_is_refused_before_anything_is_created(self):
        """Signup is all-or-nothing: a bad menu must not leave an orphan business."""
        import json

        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        tampered = {"currency": "USD", "items": [
            {"name": "Steak", "price_cents": 99.99, "section": "Mains"},
        ]}
        _, response = self._onboard({**PROFILE, "menu": tampered}, repo=repo)

        assert response["statusCode"] == 400
        assert "cents" in json.loads(response["body"])["error"]
        assert repo._businesses == {}


class TestProfileEditKeepsTheMenu:
    """A profile save must not quietly cost the owner their menu.

    `update_profile` rebuilds `profile_data` from the PUT body and supersedes
    every profile-managed fact. The profile editor does not know about menus,
    so without this the owner changing their opening hours would wipe the menu
    and its facts — while the menu *observations* stayed in the corpus, leaving
    the Analyst retrieving items the profile no longer admits to having.
    """

    def _setup(self):
        import json

        from brasstacks.handlers import onboarding
        from brasstacks.providers import FakeEmbedder
        from brasstacks.repository import InMemoryRepository

        repo = InMemoryRepository()
        token = a_session(repo)[1]
        onboarding.onboard(
            {"headers": {"Authorization": f"Bearer {token}"},
             "body": json.dumps({**PROFILE, "menu": REVIEWED_MENU,
                                 "inviteCode": "let-me-in"})},
            repo=repo, embedder=FakeEmbedder(), geocoder=None, now=NOW,
        )
        return repo, token

    def _put(self, repo, token, body):
        import json

        from brasstacks.handlers.profile import update_profile
        from brasstacks.providers import FakeEmbedder

        return update_profile(
            {"headers": {"Authorization": f"Bearer {token}"},
             "requestContext": {"http": {"method": "PUT"}},
             "body": json.dumps(body)},
            repo=repo, embedder=FakeEmbedder(), geocoder=None, now=NOW,
        )

    def test_an_edit_that_never_mentions_the_menu_preserves_it(self):
        repo, token = self._setup()
        business_id = list(repo._businesses)[0]

        # Exactly what the profile editor sends: no `menu` key at all.
        response = self._put(repo, token, {**PROFILE, "objective": "Fill Mondays"})

        assert response["statusCode"] == 200
        stored = repo._businesses[business_id]["profile_data"]["menu"]
        assert stored["items"][0]["price_cents"] == 1400
        assert "menu has 2 items" in " ".join(repo.get_business_facts(business_id))

    def test_sending_an_explicit_null_clears_it(self):
        """Absent means "I wasn't editing that". Null means "remove it"."""
        repo, token = self._setup()
        business_id = list(repo._businesses)[0]

        response = self._put(repo, token, {**PROFILE, "menu": None})

        assert response["statusCode"] == 200
        assert "menu" not in repo._businesses[business_id]["profile_data"]
        assert "menu has" not in " ".join(repo.get_business_facts(business_id))


# ---------------------------------------------------------------------------
# The timeout chain — a function must not outlive the request that called it
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

#: HTTP API integrations cap at 30s and cannot be raised; the live API's
#: integrations are all configured at 30000ms. Verified against api n71m8mb1y8
#: on 2026-08-06.
API_GATEWAY_CEILING_SECONDS = 30

_TEMPLATE = _Path(__file__).resolve().parents[2] / "deploy" / "template.yaml"


def _function_timeout(name: str) -> int:
    """The `Timeout:` declared for one Serverless::Function in the template."""
    template = _TEMPLATE.read_text(encoding="utf-8")
    block = template.split(f"  {name}:", 1)[1].split("\n  Function", 1)[0]
    return int(_re.search(r"\n      Timeout: (\d+)", block).group(1))


def test_the_scan_function_dies_with_the_request_not_after_it():
    """A Lambda outliving its API Gateway integration bills for discarded work.

    The menu scan runs behind an HTTP API whose integration timeout is 30s and
    cannot be raised. A 60s function timeout does not buy a slow scan more time
    — the browser has already been given a 504 — it just lets the Claude call
    run to completion, be billed, and have its result thrown away. Worse, the
    owner sees a failure and retries, so a menu that is too dense to scan in
    time costs double every attempt.

    Nothing is lost by dying at the ceiling: the scan stores nothing, and the
    signup write it shares a function with is a single transaction that rolls
    back.
    """
    assert _function_timeout("OnboardingFunction") <= API_GATEWAY_CEILING_SECONDS


# ---------------------------------------------------------------------------
# One call per photograph, run concurrently
# ---------------------------------------------------------------------------
#
# Measured 2026-08-06 against the real API with rendered menus, claude-opus-5 at
# low effort: 46 items over 3 pages took 25.5s, and 92 items over 4 pages took
# 38.7s. Both transcribed perfectly — every name, every price — so quality was
# never the problem. The problem is that API Gateway cuts the integration at
# 30s, and one call carrying every photo spends the sum of all of them. Fitting
# those two points gives roughly 12s of fixed overhead plus 0.29s per item,
# crossing the ceiling at about 60 items: a cafe menu passes, a real dinner menu
# does not.
#
# Splitting the work per photo makes the wall clock the slowest single page
# rather than the total, and it changes the failure mode from "lose the menu"
# to "lose a page" — which the owner can see and re-shoot.

import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


def _page(name: str, cents: int, section: str = "Pizzas") -> dict:
    """A one-item model response, in MENU_SCHEMA's shape."""
    return {"currency": "USD", "sections": [
        {"name": section, "items": [
            {"name": name, "price_cents": cents,
             "description": None, "price_note": None}]}]}


class PagedReasoner:
    """A fake that answers per image, and records concurrency honestly.

    Keyed on the image rather than on call order, because concurrent calls
    arrive in no fixed order and a queue-based fake would make these tests
    depend on thread scheduling.
    """

    def __init__(self, by_data: dict, *, delay: float = 0.0):
        self._by_data = by_data
        self._delay = delay
        self._lock = _threading.Lock()
        self.calls: list = []
        self.peak_in_flight = 0
        self._in_flight = 0

    def complete_json(self, *, system, user, schema, max_tokens, images=()):
        with self._lock:
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            self.calls.append({"system": system, "user": user,
                               "schema": schema, "max_tokens": max_tokens,
                               "images": list(images)})
        try:
            if self._delay:
                _time.sleep(self._delay)
            assert len(images) == 1, "each call should carry exactly one photo"
            answer = self._by_data[images[0].data]
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            with self._lock:
                self._in_flight -= 1


class TestEachPhotoIsScannedSeparately:
    def test_every_photo_gets_its_own_call(self):
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({
            "AAAA": _page("Margherita", 1400),
            "BBBB": _page("Tiramisu", 900, "Dolci"),
        })

        parse_menu(normalise_images([an_image(data="AAAA"),
                                     an_image(data="BBBB")]),
                   reasoner=reasoner)

        assert len(reasoner.calls) == 2
        assert all(len(c["images"]) == 1 for c in reasoner.calls)

    def test_the_pages_are_scanned_at_the_same_time(self):
        """The whole point: wall clock is the slowest page, not the sum.

        Sequentially, four 0.25s pages take a second. The generous ceiling here
        is deliberate — this asserts the calls overlap, not how fast the box is.
        """
        from brasstacks.menu import normalise_images, parse_menu

        pages = {d: _page(f"Item {d}", 1000) for d in ("AAAA", "BBBB", "CCCC", "DDDD")}
        reasoner = PagedReasoner(pages, delay=0.25)

        started = _time.monotonic()
        parse_menu(normalise_images([an_image(data=d) for d in pages]),
                   reasoner=reasoner)
        elapsed = _time.monotonic() - started

        assert reasoner.peak_in_flight >= 2, "calls did not overlap"
        assert elapsed < 0.75, f"looks sequential: {elapsed:.2f}s for 4x0.25s"

    def test_items_come_back_in_page_order(self):
        """Photo order is menu order. A merge that reorders scrambles it."""
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({
            "AAAA": _page("Antipasto", 900, "Starters"),
            "BBBB": _page("Primo", 1800, "Pasta"),
            "CCCC": _page("Dolce", 700, "Desserts"),
        }, delay=0.05)

        menu = parse_menu(
            normalise_images([an_image(data=d) for d in ("AAAA", "BBBB", "CCCC")]),
            reasoner=reasoner)

        assert [i.name for i in menu.items] == ["Antipasto", "Primo", "Dolce"]

    def test_a_single_photo_still_makes_a_single_call(self):
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({"AAAA": _page("Margherita", 1400)})

        menu = parse_menu(normalise_images([an_image(data="AAAA")]),
                          reasoner=reasoner)

        assert len(reasoner.calls) == 1
        assert [i.name for i in menu.items] == ["Margherita"]


class TestOneBadPhotoDoesNotCostTheWholeMenu:
    def test_the_readable_pages_still_come_back(self):
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import ReasoningError

        reasoner = PagedReasoner({
            "AAAA": _page("Margherita", 1400),
            "BBBB": ReasoningError("the model refused"),
            "CCCC": _page("Tiramisu", 900, "Dolci"),
        })

        menu = parse_menu(
            normalise_images([an_image(data=d) for d in ("AAAA", "BBBB", "CCCC")]),
            reasoner=reasoner)

        assert [i.name for i in menu.items] == ["Margherita", "Tiramisu"]

    def test_a_lost_page_is_counted_rather_than_hidden(self):
        """Silent truncation is the failure this module already refuses.

        The owner is about to approve this list as their menu. A page that was
        dropped has to be something they can see and re-shoot, not a gap they
        discover weeks later when the Analyst prices a dish that is not there.
        """
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import ReasoningError

        reasoner = PagedReasoner({
            "AAAA": _page("Margherita", 1400),
            "BBBB": ReasoningError("the model refused"),
        })

        menu = parse_menu(normalise_images([an_image(data="AAAA"),
                                            an_image(data="BBBB")]),
                          reasoner=reasoner)

        assert menu.pages_failed == 1
        assert menu.pages_scanned == 2

    def test_a_clean_scan_reports_no_failures(self):
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({"AAAA": _page("Margherita", 1400)})

        menu = parse_menu(normalise_images([an_image(data="AAAA")]),
                          reasoner=reasoner)

        assert menu.pages_failed == 0

    def test_every_page_failing_on_the_provider_reraises_the_provider_error(self):
        """A model outage is a 502, not a 400 blaming the owner's photograph.

        The handler maps MenuError to 400 ("your photo") and ProviderError to
        502 ("not your fault"), so which exception escapes decides what the
        owner is told to do about it. Every page failing upstream must not
        arrive as advice to take a straighter picture.
        """
        from brasstacks.menu import normalise_images, parse_menu
        from brasstacks.providers import ReasoningError

        reasoner = PagedReasoner({
            "AAAA": ReasoningError("the model refused"),
            "BBBB": ReasoningError("the model refused"),
        })

        with pytest.raises(ReasoningError):
            parse_menu(normalise_images([an_image(data="AAAA"),
                                         an_image(data="BBBB")]),
                       reasoner=reasoner)

    def test_every_page_parsing_to_nothing_is_a_menu_error(self):
        """Photos of a wall. That one *is* the owner's to fix, and says so."""
        from brasstacks.menu import MenuError, normalise_images, parse_menu

        reasoner = PagedReasoner({
            "AAAA": {"currency": "USD", "sections": []},
            "BBBB": {"currency": "USD", "sections": []},
        })

        with pytest.raises(MenuError, match="straight-on"):
            parse_menu(normalise_images([an_image(data="AAAA"),
                                         an_image(data="BBBB")]),
                       reasoner=reasoner)

    def test_a_price_the_module_refuses_fails_the_whole_scan(self):
        """A bad price is never demoted to a lost page.

        This is the regression the per-page split invited: a blanket `except`
        around each page would have turned "the model wrote a phone number into
        price_cents" into "page 2 did not read", and handed back a menu that
        looked complete with a price silently missing. Menu prices are what
        every later revenue forecast is built on.
        """
        from brasstacks.menu import MenuError, normalise_images, parse_menu

        bad = {"currency": "USD", "sections": [
            {"name": "Pizzas", "items": [
                {"name": "Margherita", "price_cents": "fourteen dollars",
                 "description": None, "price_note": None}]}]}
        reasoner = PagedReasoner({
            "AAAA": _page("Tiramisu", 900, "Dolci"),
            "BBBB": bad,
        })

        with pytest.raises(MenuError, match="integer cents"):
            parse_menu(normalise_images([an_image(data="AAAA"),
                                         an_image(data="BBBB")]),
                       reasoner=reasoner)

    def test_a_page_that_parses_to_nothing_is_a_failed_page_not_a_dead_scan(self):
        """One photo of the wall between two of the menu keeps the menu."""
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({
            "AAAA": _page("Margherita", 1400),
            "BBBB": {"currency": "USD", "sections": []},
        })

        menu = parse_menu(normalise_images([an_image(data="AAAA"),
                                            an_image(data="BBBB")]),
                          reasoner=reasoner)

        assert [i.name for i in menu.items] == ["Margherita"]
        assert menu.pages_failed == 1


class TestOverlappingPhotosDoNotDuplicateTheMenu:
    def test_the_same_row_photographed_twice_appears_once(self):
        """Owners photograph with overlap so nothing falls between shots."""
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({
            "AAAA": _page("Margherita", 1400),
            "BBBB": _page("Margherita", 1400),
        })

        menu = parse_menu(normalise_images([an_image(data="AAAA"),
                                            an_image(data="BBBB")]),
                          reasoner=reasoner)

        assert [i.name for i in menu.items] == ["Margherita"]

    def test_the_same_name_at_a_different_price_is_kept(self):
        """A half portion and a full one share a name and are not one row."""
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({
            "AAAA": _page("Margherita", 1400),
            "BBBB": _page("Margherita", 900),
        })

        menu = parse_menu(normalise_images([an_image(data="AAAA"),
                                            an_image(data="BBBB")]),
                          reasoner=reasoner)

        assert len(menu.items) == 2

    def test_the_same_name_in_a_different_section_is_kept(self):
        from brasstacks.menu import normalise_images, parse_menu

        reasoner = PagedReasoner({
            "AAAA": _page("Espresso", 350, "Bevande"),
            "BBBB": _page("Espresso", 350, "Dolci"),
        })

        menu = parse_menu(normalise_images([an_image(data="AAAA"),
                                            an_image(data="BBBB")]),
                          reasoner=reasoner)

        assert len(menu.items) == 2
