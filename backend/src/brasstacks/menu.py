"""Menu photos in, priced menu rows out.

An owner signing up has a menu on the wall or in their hand long before they
have it in a spreadsheet. Photographing it is the cheapest way to tell the
agents what the business actually sells and what it charges — and what it
charges is the number every later Analyst hypothesis about pricing depends on.

Two design notes worth keeping:

**Prices live as integer cents in one place.** The canonical copy is the
`profile_data.menu` JSONB on the business row. The observation and fact rows
built here are *derived prose* for retrieval — Titan embeds sentences, not
key-value pairs, so "Margherita|1400|Pizzas" would embed to noise. If the two
ever disagree, the JSONB is right and the prose is stale.

**Nothing here trusts its input.** The model is reading digits off a
photograph and the owner then corrects the result in a browser, so the same
validation runs on the model's output and again on whatever the browser posts
back. `finds._require_cents` sets the house standard and this mirrors it.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .providers import ImageInput

#: Four photos covers a folded trifold or a two-page card with a drinks insert.
#: The cap exists because every image is input tokens on a Claude call, and a
#: signup flow is exactly where someone would idly attach their camera roll.
MAX_IMAGES = 4

#: Decoded bytes per image. A phone camera JPEG is 3–6MB and API Gateway
#: rejects the whole request body past 10MB, so the browser downscales before
#: upload. This is the server declining to depend on that having happened.
MAX_IMAGE_BYTES = 4_500_000

ALLOWED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")

#: $1,000 for a single line. A tasting menu can be expensive; a phone number
#: read off the footer cannot be a price. This is the ceiling that separates
#: the two, and it is the same reasoning as MAX_PREDICTED_DAILY_CENTS in finds.
MAX_ITEM_PRICE_CENTS = 100_000

#: A long menu is ~120 lines. Past this the model is repeating itself.
MAX_ITEMS = 200

#: Menus are short documents but a dense one runs long once every description
#: is transcribed. Truncation here loses the back half of the menu silently.
DEFAULT_MENU_MAX_TOKENS = 8192

#: Written on the observation rows so a re-scan can find and supersede them,
#: and so the Analyst's evidence trail names where the item came from.
MENU_SOURCE_NAME = "menu_scan"
MENU_STATEMENT_TYPE = "menu_item"

#: `observation_kind` already had this value for owner-supplied material.
MENU_OBSERVATION_KIND = "owner_upload"

_DATA_URL = re.compile(r"^data:([-\w.+/]+);base64,", re.IGNORECASE)


class MenuError(ValueError):
    """The scan cannot be trusted. Always safe to show to the owner."""


@dataclass(frozen=True)
class MenuItem:
    name: str
    price_cents: int | None = None
    price_note: str | None = None
    description: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class Menu:
    items: tuple[MenuItem, ...]
    currency: str = "USD"
    #: How many photographs the scan was given, and how many of them produced no
    #: rows — because the model failed on them, or because they were pictures of
    #: something that is not a menu. Both default to zero so a menu rebuilt from
    #: stored JSON, which has no scan behind it, is not describing one.
    pages_scanned: int = 0
    pages_failed: int = 0

    @property
    def priced(self) -> tuple[MenuItem, ...]:
        return tuple(i for i in self.items if i.price_cents is not None)

    @property
    def sections(self) -> tuple[str, ...]:
        seen: list[str] = []
        for item in self.items:
            if item.section and item.section not in seen:
                seen.append(item.section)
        return tuple(seen)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def format_cents(cents: int) -> str:
    """Render integer cents for display.

    Integer arithmetic on purpose. `cents / 100` reads correctly for 1400 and
    produces 16.499999999999998 for 1650 — which then reaches an owner-facing
    sentence and a Titan embedding.
    """
    return f"${cents // 100}.{cents % 100:02d}"


def _price_cents(value: Any, *, where: str) -> int | None:
    """Validate one price. Mirrors `finds._require_cents`."""
    if value is None:
        return None

    # bool is an int subclass; True would otherwise price an item at one cent.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MenuError(
            f"{where}: price must be a number of integer cents, got {value!r}. "
            "A string here usually means the model ignored the output schema."
        )

    if isinstance(value, float):
        # 1400.0 survives a JSON round trip and is unambiguous.
        # 1400.5 is not a real amount of money.
        if not value.is_integer():
            raise MenuError(f"{where}: price must be whole cents, got {value!r}")
        value = int(value)

    if value < 0:
        raise MenuError(f"{where}: price must be non-negative, got {value}")

    if value > MAX_ITEM_PRICE_CENTS:
        raise MenuError(
            f"{where}: price {value} is implausible (ceiling "
            f"{MAX_ITEM_PRICE_CENTS}). Usually dollars written into the cents "
            "field, or a phone number read off the menu footer."
        )

    return int(value)


# ---------------------------------------------------------------------------
# Image intake
# ---------------------------------------------------------------------------

def _clean(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    return text[:limit] if text else None


def normalise_images(payload: Any) -> list[ImageInput]:
    """Turn whatever the browser posted into images we are willing to send."""
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise MenuError("images must be a list of {media_type, data} objects")

    entries = list(payload)
    if not entries:
        raise MenuError("send at least one photo of the menu")
    if len(entries) > MAX_IMAGES:
        raise MenuError(
            f"send at most {MAX_IMAGES} photos, got {len(entries)}"
        )

    images: list[ImageInput] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise MenuError(f"photo {index} must be an object, got {type(entry).__name__}")

        data = entry.get("data")
        if not isinstance(data, str) or not data.strip():
            raise MenuError(f"photo {index} has no data")
        data = data.strip()

        media_type = _clean(entry.get("media_type"), limit=64)

        # `canvas.toDataURL()` hands back `data:image/jpeg;base64,...`. Passing
        # that through verbatim puts the prefix inside the base64 field and the
        # API rejects the whole message with nothing useful to show the owner.
        match = _DATA_URL.match(data)
        if match:
            media_type = media_type or match.group(1).lower()
            data = data[match.end():]

        data = "".join(data.split())

        if not media_type:
            raise MenuError(f"photo {index} has no media_type")
        media_type = media_type.lower()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise MenuError(
                f"photo {index} is {media_type}, which is not a supported image "
                f"type (expected one of {', '.join(ALLOWED_MEDIA_TYPES)})"
            )

        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MenuError(f"photo {index} is not valid base64: {exc}") from exc

        if not decoded:
            raise MenuError(f"photo {index} decoded to zero bytes")
        if len(decoded) > MAX_IMAGE_BYTES:
            raise MenuError(
                f"photo {index} is too large ({len(decoded)} bytes, "
                f"ceiling {MAX_IMAGE_BYTES}). Downscale before uploading."
            )

        images.append(ImageInput(media_type=media_type, data=data))

    return images


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------

MENU_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "currency": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}]
                                },
                                "price_cents": {
                                    "anyOf": [{"type": "integer"}, {"type": "null"}]
                                },
                                "price_note": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}]
                                },
                            },
                            "required": ["name", "description", "price_cents", "price_note"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["currency", "sections"],
    "additionalProperties": False,
}

MENU_SYSTEM = (
    "You transcribe restaurant menus from photographs. You are a transcriber, "
    "not an author: every item you return must be legible in the image. If a "
    "price is unreadable, obscured, or written as words rather than digits, "
    "leave price_cents null and put what the menu actually says in price_note "
    "(for example 'Market price' or 'Ask your server'). Never estimate a price "
    "from a similar item, from the section, or from what the dish usually "
    "costs — a guessed price becomes a revenue forecast the owner is told to "
    "trust.\n\n"
    "Prices are INTEGER CENTS. $14 is 1400. $16.50 is 1650. 95c is 95. Never "
    "return dollars in this field.\n\n"
    "Keep the menu's own section headings. If the menu has no sections, use a "
    "single section named after the page, such as 'Menu'. Copy descriptions "
    "roughly as written; do not invent one for an item that has none."
)

MENU_USER = (
    "Transcribe every item on this menu, in the order it appears. Return the "
    "sections and their items."
)


def _parse_page(
    image: ImageInput,
    *,
    reasoner: Any,
    max_tokens: int,
) -> tuple[str, list[MenuItem]]:
    """Read one photograph. Returns its currency and its items."""
    payload = reasoner.complete_json(
        system=MENU_SYSTEM,
        user=MENU_USER,
        schema=MENU_SCHEMA,
        max_tokens=max_tokens,
        images=[image],
    )

    currency = _clean(payload.get("currency"), limit=8) or "USD"
    sections = payload.get("sections")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        # No rows rather than an exception: a photo that yielded nothing is a
        # lost page, and `parse_menu` counts those. Raising here would make one
        # blank shot indistinguishable from a price this module cannot trust,
        # and only one of those two may be swallowed.
        return currency, []

    items: list[MenuItem] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        section_name = _clean(section.get("name"), limit=80)
        raw_items = section.get("items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            continue
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            name = _clean(raw.get("name"), limit=120)
            if not name:
                continue
            where = f"{section_name or 'menu'} / {name}"
            items.append(MenuItem(
                name=name,
                price_cents=_price_cents(raw.get("price_cents"), where=where),
                price_note=_clean(raw.get("price_note"), limit=80),
                description=_clean(raw.get("description"), limit=240),
                section=section_name,
            ))

    return currency, items


def parse_menu(
    images: Sequence[ImageInput],
    *,
    reasoner: Any,
    max_tokens: int = DEFAULT_MENU_MAX_TOKENS,
) -> Menu:
    """Read a menu out of photographs — one model call per photo, in parallel.

    Measured against the real API on 2026-08-06 with rendered menus, opus-5 at
    low effort: 46 items over three pages took 25.5s, 92 items over four pages
    took 38.7s. Transcription was exact both times, so accuracy was never what
    threatened this feature — the clock was. API Gateway cuts the integration at
    30s, and one call carrying every photo pays the sum of all of them, which
    those two points put over the ceiling at around sixty items. A cafe menu
    fits. The dinner menu this exists to read does not.

    Per photo, concurrently, the wall clock becomes the slowest single page
    instead of the total. Threads rather than asyncio because the client is
    synchronous and this is entirely network wait; the pool is bounded by
    MAX_IMAGES, which is four.

    The second gain is the failure mode. One unreadable photo used to cost the
    whole menu; now it costs its own page, the rest come back, and `pages_failed`
    says so out loud — an owner about to approve this list as their menu must be
    able to see the gap and re-shoot it, not discover it weeks later when the
    Analyst prices a dish that is not there.
    """
    if not images:
        raise MenuError("send at least one photo of the menu")

    images = list(images)
    # Indexed so the merge below is in photo order however the threads finish.
    # Photo order is menu order, and a scan that returned the dessert page first
    # would be a menu the owner has to re-sort by hand.
    results: dict[int, tuple[str, list[MenuItem]]] = {}
    failures: dict[int, Exception] = {}

    def scan(index: int) -> None:
        try:
            currency, items = _parse_page(
                images[index], reasoner=reasoner, max_tokens=max_tokens)
        except MenuError:
            # Deliberately NOT tolerated. The only MenuError `_parse_page` can
            # raise is a price it refused — a string, a fraction, a phone number
            # off the footer — and that is the one failure this whole module
            # exists to make loud. Demoting it to a lost page would let a menu
            # come back looking complete while a price the owner never saw was
            # dropped, and a menu price is what every later revenue forecast is
            # built on. A blank photo returns no rows instead of raising.
            raise
        except Exception as e:  # noqa: BLE001 — one page must not end the scan
            failures[index] = e
            return
        if not items:
            # A photo of the wall, or the back of the menu. Not an error for the
            # scan, but not a page either: counted as lost so the total the owner
            # is shown matches the number of photos that actually produced rows.
            failures[index] = MenuError("no menu items in that photo")
            return
        results[index] = (currency, items)

    if len(images) == 1:
        scan(0)
    else:
        with ThreadPoolExecutor(max_workers=len(images)) as pool:
            list(pool.map(scan, range(len(images))))

    if not results:
        # Every photo failed. Re-raise the first real provider error rather than
        # inventing a summary, because "the model refused" and "that is a picture
        # of a wall" need different things from the owner.
        first = failures[min(failures)]
        if isinstance(first, MenuError):
            raise MenuError(
                "found no menu items in those photos. A clearer, straight-on "
                "shot of the menu text usually fixes it."
            )
        raise first

    currency = results[min(results)][0]

    # Owners photograph with overlap so nothing falls between shots, so the same
    # row genuinely arrives twice. Deduped on the whole triple, not the name: a
    # half portion and a full one share a name at different prices, and one drink
    # can appear under two sections.
    merged: list[MenuItem] = []
    seen: set[tuple[str, str, int | None]] = set()
    for index in sorted(results):
        for item in results[index][1]:
            key = (item.name.casefold(),
                   (item.section or "").casefold(),
                   item.price_cents)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

    return Menu(
        items=tuple(merged[:MAX_ITEMS]),
        currency=currency,
        pages_scanned=len(images),
        pages_failed=len(failures),
    )


# ---------------------------------------------------------------------------
# The round trip through the browser
# ---------------------------------------------------------------------------

def menu_from_payload(value: Any) -> Menu | None:
    """Re-validate a menu that came back from the review step.

    The owner corrects the scan before submitting, so this data is
    client-controlled by the time we see it again. Running it through the same
    price rules as the model output is the only thing between a hand-edited
    request and arbitrary cents on the business profile.

    Returns None for "the owner skipped the scan", which is not an error —
    scanning is optional and must never block signup.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MenuError("menu must be an object")

    raw_items = value.get("items")
    if raw_items is None:
        return None
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise MenuError("menu.items must be a list")

    items: list[MenuItem] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        name = _clean(raw.get("name"), limit=120)
        # A blank row left behind in the review UI is a slip, not an attack.
        # Dropping it silently beats failing a signup over an empty text box.
        if not name:
            continue
        items.append(MenuItem(
            name=name,
            price_cents=_price_cents(raw.get("price_cents"), where=name),
            price_note=_clean(raw.get("price_note"), limit=80),
            description=_clean(raw.get("description"), limit=240),
            section=_clean(raw.get("section"), limit=80),
        ))

    if not items:
        return None

    currency = _clean(value.get("currency"), limit=8) or "USD"
    return Menu(items=tuple(items[:MAX_ITEMS]), currency=currency)


def menu_to_payload(menu: Menu) -> dict[str, Any]:
    """The canonical stored shape: integer cents, no prose."""
    return {
        "currency": menu.currency,
        "items": [
            {
                "name": item.name,
                "description": item.description,
                "price_cents": item.price_cents,
                "price_note": item.price_note,
                "section": item.section,
            }
            for item in menu.items
        ],
    }


# ---------------------------------------------------------------------------
# What the agents read
# ---------------------------------------------------------------------------

def _price_phrase(item: MenuItem) -> str:
    if item.price_cents is not None:
        return format_cents(item.price_cents)
    return item.price_note or "an unlisted price"


def menu_observations(menu: Menu) -> list[str]:
    """One sentence per item, for the Analyst's vector search.

    Sentences rather than rows because that is what Titan responds to. Measured
    against this corpus, concrete hypothesis-shaped text retrieves around 0.58
    similarity where abstract phrasing gets 0.24 and pulls the wrong cluster
    entirely. A menu row serialised as fields would embed to noise and the
    Analyst would never see the menu at all.
    """
    statements: list[str] = []
    for item in menu.items:
        if item.section:
            sentence = (
                f"Our menu lists {item.name} at {_price_phrase(item)} "
                f"in the {item.section} section."
            )
        else:
            sentence = f"Our menu lists {item.name} at {_price_phrase(item)}."
        if item.description:
            description = item.description.rstrip(". ")
            sentence = f"{sentence} {description}."
        statements.append(sentence)
    return statements


def menu_facts(menu: Menu) -> list[str]:
    """Standing context, read wholesale rather than retrieved.

    The Analyst loads facts on every run, so these hold even on a night when
    no vector query happens to land on a menu item.
    """
    facts: list[str] = []

    sections = menu.sections
    if sections:
        facts.append(
            f"The menu has {len(menu.items)} items across {len(sections)} "
            f"sections: {', '.join(sections)}."
        )
    else:
        facts.append(f"The menu has {len(menu.items)} items.")

    priced = menu.priced
    if priced:
        amounts = [item.price_cents for item in priced]
        # Integer division: an average price is reported to the cent, and a
        # float here would put 15.249999999999998 in front of an owner.
        average = sum(amounts) // len(amounts)
        facts.append(
            f"Menu prices run from {format_cents(min(amounts))} to "
            f"{format_cents(max(amounts))}, averaging {format_cents(average)}."
        )

    unpriced = [item for item in menu.items if item.price_cents is None]
    if unpriced:
        facts.append(
            f"{len(unpriced)} menu items have no printed price: "
            f"{', '.join(item.name for item in unpriced[:5])}."
        )

    return facts
