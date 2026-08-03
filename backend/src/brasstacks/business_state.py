"""What the business already is, as opposed to what the market says about it.

The Analyst has always been handed two things: the owner's facts and a ranked
slice of market observations. Nothing ever told it what the business *already
sells*, when it *already opens*, or which website is *actually the owner's*. An
audit of the nine finds live on 2026-08-03 found three wrong for exactly that:

* **818fdb2d** told Yellow Cow to launch a fixed-price weekday lunch set. Three
  rows of its own memory name the Dosirak set meal — Postmates lists it as a
  featured item, Uber Eats as the most popular one, Tripadvisor as something the
  restaurant offers. None was cited. It was not a launch.
* **c651141b** criticised asakacatogo.com. The owner declared asakakaiyo.com.
  The page carries Asaka's address and phone number so it may well be theirs,
  but nobody had said so, and the find read as a critique of the owner's site.
* **cbac2b29** called four sources "mutually contradictory hours" at the highest
  confidence of the nine on the weakest evidence of the nine. Two of the four
  are Grubhub pickup windows carrying no weekday at all. The rest cover three
  overlapping days. The contradiction was an artifact of nobody reconciling the
  sources per weekday before reading them.

So this module answers one question — *what is already true of this business?* —
and it answers it with pure functions over a profile and a set of stored rows.
No repository, no embedder, no model call. A claim about what a business already
sells is a claim about the world and has to be arguable in a test.

Two boundaries worth stating:

**It is not the market's opinion.** Reviews, competitor prices and demand
signals stay where they were. This is inventory, hours and identity — the things
a move must not contradict.

**It re-runs ingest hygiene rather than trusting it.** The 147 rows in the
cluster were stored before `clean_observation_text` existed, so half of every
corpus is still navigation and rival carousels. Re-running a pure function over
text we already hold is free, and the alternative is reading Pho So 1's rating
as Yellow Cow's menu.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from brasstacks.provenance import source_host, source_identity
from brasstacks.signals import (
    _business_key,
    _is_carousel_heading,
    _is_chrome,
    _heading,
    _squash,
    classify_statement,
    clean_observation_text,
)

# The hygiene helpers above are imported private rather than re-derived. What
# counts as page furniture or a rival carousel is one judgement, tuned against
# one corpus; two copies of it would drift and the second copy would be the one
# nobody tested.

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday")

#: How many offers reach the prompt. Asaka's Postmates storefront alone lists
#: eighteen menu sections, and this block sits in every nightly prompt: it has
#: to be bounded by design rather than by whichever menu Radar happened to
#: fetch. Ranked by how many distinct sources name the offer, so what survives
#: the cut is what memory is most sure of.
MAX_OFFERS_SHOWN = 12

#: Longest an offer name may be. Beyond this it is a sentence about a dish, not
#: the name of one — "Crunch Albacore Roll Shrimp tempura roll with green onion
#: tempura, topped with seared albacore, crispy onion and eel sauce" is a
#: description, and naming it in a find would put 20 uncitable words on a card.
MAX_OFFER_CHARS = 48
MAX_OFFER_WORDS = 6

#: Shortest offer name the mention scan will search the corpus for. Below this
#: a name is a fragment — "Bar" would match every row containing the word — and
#: an offer whose evidence is a coincidence is worse than one with a single
#: citation.
MIN_OFFER_KEY = 4


@dataclass(frozen=True)
class Offer:
    """Something this business already sells, and where we learned it.

    ``origin`` is the distinction the Analyst needs. "owner" is the owner
    telling us what they sell, which no observation can contradict. "market" is
    a storefront or a reviewer naming it, which is weaker but is citable — and
    citable is what a find needs, because a find may only name what it cites.
    """

    name: str
    origin: str
    observation_ids: tuple[str, ...] = ()
    sources: int = 0

    @property
    def key(self) -> str:
        """The words to look for when asking "does this move name this offer?"."""
        return _offer_key(self.name)


@dataclass(frozen=True)
class HoursClaim:
    """One source saying when this business trades on one day.

    ``weekday`` is None when the text named no day. That is not a gap to be
    filled in: "Hours Today Pickup: 11:45am–8:45pm" is a statement about the day
    the page was fetched, and treating it as a weekly rule is how five sources
    became "mutually contradictory".

    ``opens`` and ``closes`` are minutes past local midnight, and both are None
    when the day is closed. ``closes`` may exceed 1440 for a trading day that
    ends after midnight.
    """

    weekday: int | None
    opens: int | None
    closes: int | None
    service: str | None = None
    observation_id: str | None = None
    source: str | None = None

    @property
    def closed(self) -> bool:
        return self.opens is None


@dataclass(frozen=True)
class DayHours:
    """Every claim memory holds about one weekday, and whether they agree."""

    weekday: int
    claims: tuple[HoursClaim, ...]

    @property
    def name(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    @property
    def agreed(self) -> bool:
        """True unless two *different* sources state different windows.

        Two conditions, and both matter. Different sources, because one
        storefront read twice is one document — the mistake provenance.py
        exists to stop, arriving here by another route. Same service, because a
        takeout window closing before the dining room does is not a
        disagreement, it is a takeout window.
        """
        by_service: dict[str, dict[str, tuple[int | None, int | None]]] = {}
        for claim in self.claims:
            windows = by_service.setdefault(claim.service or "", {})
            windows[claim.source or claim.observation_id or ""] = (
                claim.opens, claim.closes)
        return all(len(set(windows.values())) <= 1 for windows in by_service.values())


@dataclass(frozen=True)
class DomainNote:
    """A host memory holds pages from, and whether the owner claimed it."""

    host: str
    declared: bool
    observation_ids: tuple[str, ...]
    #: The host name contains the business name. asakacatogo.com does;
    #: yelp.com does not. A lookalike is the dangerous case — it is the one a
    #: find will describe as "your website" without ever being told it is.
    resembles_name: bool = False


@dataclass(frozen=True)
class BusinessState:
    name: str
    declared_domain: str | None
    offers: tuple[Offer, ...]
    days: tuple[DayHours, ...]
    undated_hours: tuple[HoursClaim, ...]
    domains: tuple[DomainNote, ...]

    @property
    def conflicts(self) -> tuple[DayHours, ...]:
        return tuple(day for day in self.days if not day.agreed)

    @property
    def known(self) -> bool:
        return bool(self.offers or self.days or self.undated_hours or self.domains)

    def offer_named(self, text: str) -> Offer | None:
        """The offer a proposed move is talking about, or None.

        Deliberately literal: it matches the offer's own words appearing in the
        move. That is enough to catch "launch a fixed-price weekday lunch set"
        against "Dosirak (Set Meal)" only because the move names the dish, which
        find 818fdb2d's did not — so this is a check the *prompt* has to make
        the model perform, and this method is how a test can pin the inventory
        that makes it possible.
        """
        haystack = f" {_words(text)} "
        best: Offer | None = None
        for offer in self.offers:
            key = offer.key
            if len(key) < MIN_OFFER_KEY or f" {key} " not in haystack:
                continue
            if best is None or len(key) > len(best.key):
                best = offer
        return best


# ---------------------------------------------------------------------------
# The declared domain
# ---------------------------------------------------------------------------
#
# `profile_data.business.website` is where onboarding is supposed to keep this,
# and it is null for all three live tenants. What onboarding actually wrote is a
# `business_fact` sentence: "The business website is https://www.asakakaiyo.com/."
# Both are read, profile first, because the profile is the field an owner can
# edit and the fact is a sentence generated from an earlier version of it.

_WEBSITE_FACT = re.compile(
    r"\b(?:business\s+)?website\s+is\s+(?P<url>\S+)", re.IGNORECASE)


def _registrable(host: str | None) -> str | None:
    if not host:
        return None
    host = host.strip().strip(".").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def declared_domain(business: Mapping[str, Any] | None,
                    facts: Sequence[str]) -> str | None:
    """The website the owner claimed, without the www, or None."""
    profile = (business or {}).get("profile_data") or {}
    stated = ((profile.get("business") or {}).get("website")
              if isinstance(profile, Mapping) else None)
    host = _registrable(source_host(stated) or urlsplit(str(stated or "")).hostname)
    if host:
        return host
    for fact in facts:
        match = _WEBSITE_FACT.search(str(fact))
        if match is None:
            continue
        host = _registrable(source_host(match.group("url").rstrip(".")))
        if host:
            return host
    return None


def _is_declared(host: str, declared: str | None) -> bool:
    """Is this host the owner's declared site, or a subdomain of it?

    Subdomains count: order.asakakaiyo.com is the owner's ordering page, and
    demanding an exact host would flag the owner's own checkout as somebody
    else's website.
    """
    return bool(declared) and (host == declared or host.endswith("." + declared))


# ---------------------------------------------------------------------------
# Hours
# ---------------------------------------------------------------------------

_DAY_INDEX = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "weds": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Longest first, so "Monday" is never read as "Mon" with "day" left over. Two
# letter forms ("we", "sa") are deliberately absent: "we" is a pronoun, and a
# review beginning "We - 11 people - waited" would become a Wednesday.
_DAY = "(?:" + "|".join(sorted(_DAY_INDEX, key=len, reverse=True)) + ")"

#: What separates two days. A dash means a range ("Monday - Thursday"); a comma
#: means a list ("Mon,Tue,Thur,Sun"). Both shapes are in the live corpus, on
#: pages describing the same restaurant.
_DAY_SEP = r"\s*(?:,|&|/|\+|and|thru|through|to|[-–—])\s*"
_DAY_SEQ = rf"{_DAY}\b(?:{_DAY_SEP}{_DAY}\b)*"
_RANGE_SEP = re.compile(r"[-–—]|\bthru\b|\bthrough\b|\bto\b", re.IGNORECASE)

#: A clock reading. The lookarounds are what keep "(310) 791-0300" and "$15.99"
#: out: a digit or a currency symbol before, or a digit or a decimal point after,
#: disqualifies the match, so no part of a phone number or a price can be read
#: as an opening time. The trailing guard is `\.\d` rather than a bare `.`
#: because "Thursday : 11:30-21:30." ends a sentence, and rejecting that dot
#: cost the close time its minutes — 21:30 was read as 21:00.
_TIME = r"(?<![\d.$])\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?(?!\d|\.\d)"
_TIME_SEP = r"\s*(?:[-–—~]|\bto\b|\buntil\b|\btill\b)\s*"
_WINDOW = rf"(?:{_TIME}{_TIME_SEP}{_TIME}|closed)"

_DATED_HOURS = re.compile(
    rf"(?P<days>{_DAY_SEQ})\s*(?:[:\-–—(]\s*)?(?P<window>{_WINDOW})",
    re.IGNORECASE)
_ANY_WINDOW = re.compile(_WINDOW, re.IGNORECASE)
_CLOCK = re.compile(
    r"(?<![\d.$])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?(?!\d|\.\d)"
    r"|(?<![\d.$])(\d{1,2})(?::(\d{2}))?(?!\d|\.\d)",
    re.IGNORECASE)

#: What a window is a window *for*. Longest first so "lunch menu" wins over
#: "menu" and "lunch". Asaka runs a lunch menu until 3pm and a dinner menu after
#: it; reading those as one contradictory pair is the cbac2b29 mistake in
#: miniature.
_SERVICES = ("lunch menu", "dinner menu", "happy hour", "drive thru",
             "drive-thru", "dine-in", "dine in", "take-out", "takeout",
             "take out", "pick-up", "pickup", "pick up", "delivery",
             "breakfast", "brunch", "lunch", "dinner", "kitchen", "bar",
             # Last, and only reachable once "lunch menu" has failed to match.
             # Its whole job is to stop the main menu's window inheriting the
             # lunch label from the line above it.
             "menu")
_SERVICE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _SERVICES) + r")\b",
    re.IGNORECASE)

#: How far back a window looks for the service it belongs to. Eighty characters
#: reaches "Hours of Operation (Takeout)" from the "Monday - Thursday" two lines
#: below it, which is the layout asakacatogo.com actually ships.
_SERVICE_LOOKBEHIND = 80
_SERVICE_LOOKAHEAD = 40

#: A dayless window needs a reason to be read as opening hours at all. Without
#: this every "11:30 - 3:00" in a review becomes a trading window.
_HOURS_CUE = re.compile(
    r"\b(hours?|open|opens|opening|closed|closes|closing|menu|pick[- ]?up|"
    r"pickup|delivery|take[- ]?out|takeout|dine[- ]?in|serving|served)\b",
    re.IGNORECASE)

#: Words too common to identify a service unless they caption the window
#: directly. Everything else in `_SERVICES` names a way of buying food; "menu"
#: names a page.
_CAPTION_ONLY_SERVICES = frozenset({"menu"})

_SERVICE_ALIASES = {"pick up": "pickup", "pick-up": "pickup",
                    "take out": "takeout", "take-out": "takeout",
                    "dine in": "dine-in", "drive thru": "drive-thru"}


def _expand_days(text: str) -> list[int]:
    """Turn "Mon,Tue,Thur,Sun" or "Monday - Thursday" into weekday numbers."""
    parts = re.split(f"({_DAY})", text, flags=re.IGNORECASE)
    days: list[int] = []
    for index in range(1, len(parts), 2):
        current = _DAY_INDEX[parts[index].lower()]
        separator = parts[index - 1]
        if days and _RANGE_SEP.search(separator):
            previous = days[-1]
            span = (list(range(previous, current + 1)) if current >= previous
                    else list(range(previous, 7)) + list(range(0, current + 1)))
            days.extend(day for day in span[1:])
        elif current not in days:
            days.append(current)
    return days


def _minutes(hour: int, minute: int, meridiem: str | None) -> int | None:
    if minute > 59:
        return None
    if meridiem is None:
        return hour * 60 + minute if hour <= 24 else None
    if not 1 <= hour <= 12:
        return None
    if meridiem == "a":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return hour * 60 + minute


def _read_window(text: str) -> tuple[int | None, int | None] | None:
    """Both ends of "11:30 AM – 9:30 PM", or None if this is not a window.

    A pair of bare numbers is rejected outright. "rated 4 - 5" is a rating
    spread and "$10 - $22" is a price band; without a colon or a meridiem on at
    least one side there is nothing to distinguish either from opening hours.
    """
    if text.strip().lower() == "closed":
        return None, None
    if ":" not in text and not re.search(r"[ap]\.?m", text, re.IGNORECASE):
        return None
    readings = []
    for match in _CLOCK.finditer(text):
        if match.group(1) is not None:
            readings.append((int(match.group(1)), int(match.group(2) or 0),
                             match.group(3).lower()))
        else:
            readings.append((int(match.group(4)), int(match.group(5) or 0), None))
    if len(readings) != 2:
        return None

    (open_hour, open_min, open_mer), (close_hour, close_min, close_mer) = readings
    # One side carrying the meridiem is the common case: "11:30am-10pm" states
    # it twice, "Open 11 - 9pm" states it once. Assume the stated one applies to
    # both, and only flip if that would close the shop before it opened.
    if open_mer and not close_mer:
        for guess in (open_mer, "p" if open_mer == "a" else "a"):
            close_mer = guess
            if (_minutes(close_hour, close_min, guess) or 0) > (
                    _minutes(open_hour, open_min, open_mer) or 0):
                break
    elif close_mer and not open_mer:
        for guess in ("a" if close_mer == "p" else "p", close_mer):
            open_mer = guess
            if (_minutes(open_hour, open_min, guess) or 0) < (
                    _minutes(close_hour, close_min, close_mer) or 0):
                break

    opens = _minutes(open_hour, open_min, open_mer)
    closes = _minutes(close_hour, close_min, close_mer)
    if opens is None or closes is None:
        return None
    if closes <= opens:
        # A trading day that ends after midnight, not a source contradicting
        # itself. Kept as minutes past the opening midnight so two windows can
        # still be compared as numbers.
        closes += 24 * 60
    return opens, closes


def _service_near(text: str, start: int, end: int) -> str | None:
    """Which service this window belongs to, or None for the room itself.

    A label sitting immediately after the window wins, and "immediately" means
    nothing but punctuation between them. Postmates writes "11:30 AM - 3:00 PM •
    Lunch Menu" then "11:30 AM - 8:30 PM • Menu" two lines down, and taking the
    nearest label *above* instead made Asaka look like it served lunch until
    half past eight.

    A label further ahead is not used at all. palsaikkoreanbbqca.com ends its
    hours line "· Dining Dine-in · Takeout", which is a list of what the
    restaurant does, not a label on Tuesday's window — reading it as one split
    Tuesday into its own service group and hid a real disagreement with the
    Facebook page.
    """
    tail = text[end:end + _SERVICE_LOOKAHEAD]
    adjacent = _SERVICE.search(tail)
    # A bullet or a dash between the window and the label makes it a caption. A
    # bare space does not: "Pickup: 11:45am–8:45pm Delivery: 11:45am–8:45pm"
    # puts the *next* window's label one space after this one's.
    if adjacent is not None and re.fullmatch(r"\s*[•·|()\[\]\-–—][\s•·|()\[\]\-–—]*",
                                             tail[:adjacent.start()]):
        name = adjacent.group(1).lower()
        return _SERVICE_ALIASES.get(name, name)
    before = _SERVICE.findall(text[max(0, start - _SERVICE_LOOKBEHIND):start])
    if before:
        name = before[-1].lower()
        # "Menu" earns a window only as a caption on it. m.yelp.com puts "Full
        # menu" in its header above Palsaik's weekday hours, and labelling those
        # as the menu's hours moved them into their own service group where they
        # could no longer disagree with the Facebook page.
        if name in _CAPTION_ONLY_SERVICES:
            return None
        return _SERVICE_ALIASES.get(name, name)
    return None


def parse_hours(text: str) -> tuple[HoursClaim, ...]:
    """Every trading window this text states, keyed by weekday where it says one.

    Windows carrying no weekday come back with ``weekday=None`` rather than
    being dropped or guessed at. That is the whole correction: find cbac2b29
    counted two Grubhub "Hours Today Pickup" lines among four sources it called
    mutually contradictory, and neither line says anything about any day of the
    week.
    """
    text = str(text or "")
    claims: list[HoursClaim] = []
    covered: list[tuple[int, int]] = []

    for match in _DATED_HOURS.finditer(text):
        window = _read_window(match.group("window"))
        if window is None:
            continue
        covered.append((match.start("window"), match.end("window")))
        service = _service_near(text, match.start(), match.end())
        for weekday in _expand_days(match.group("days")):
            claims.append(HoursClaim(weekday=weekday, opens=window[0],
                                     closes=window[1], service=service))

    for match in _ANY_WINDOW.finditer(text):
        if any(start <= match.start() < end for start, end in covered):
            continue
        window = _read_window(match.group())
        if window is None:
            continue
        near = text[max(0, match.start() - _SERVICE_LOOKBEHIND):
                    match.end() + _SERVICE_LOOKAHEAD]
        if not _HOURS_CUE.search(near):
            continue
        claims.append(HoursClaim(
            weekday=None, opens=window[0], closes=window[1],
            service=_service_near(text, match.start(), match.end())))
    return tuple(claims)


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------

_SELLS_FACT = re.compile(r"\bwhat\s+we\s+sell\s+is\s+(?P<offers>.+?)\s*\.?\s*$",
                         re.IGNORECASE)
_OFFER_SPLIT = re.compile(r",\s*|\s+and\s+", re.IGNORECASE)

#: Menu-list labels. "Featured items. Dosirak (Set Meal)" is one storefront
#: line: a label, then the list it introduces. Without stripping the label the
#: first dish on every Postmates page is named "Featured items. <dish>".
_LIST_LABEL = re.compile(
    r"^(?:featured|popular|most\s+ordered|top|our|the)?\s*"
    r"(?:items?|dishes|menu)\s*[.:—-]\s*", re.IGNORECASE)
_LIST_SEPARATOR = re.compile(r"\s*[·•|]\s*")

#: How many items an interpunct list needs before it is read as a menu without a
#: label to say so. Four, because "Address … · Hours … · Dining Dine-in ·
#: Takeout." is a listing card with four parts and would otherwise sell the
#: owner "Dine-in".
_UNLABELLED_LIST_MIN = 4

#: Headings that name a part of a web page rather than a thing for sale.
_NOT_AN_OFFER = frozenset({
    "featured items", "featured item", "popular items", "most ordered",
    "menu", "main menu", "full menu", "our menu", "location", "locations",
    "photos", "photo", "hours", "opening hours", "openning hours",
    "hours of operation", "info", "general info", "more info", "store info",
    "store information", "reviews", "review", "ratings", "ratings & reviews",
    "good to know", "plan your visit", "website", "phone", "address", "map",
    "about", "about us", "contact", "contact us", "directions", "cuisine",
    "browse nearby", "nearby places", "similar restaurants", "share",
    "faq", "frequently asked questions", "from the kitchen", "what guests say",
    "overview", "details", "gallery", "amenities", "payment", "delivery",
    "pickup", "takeout", "dine-in", "order online", "start new order",
    "special offers", "offers", "deals", "prices", "price", "hours today",
})

_OFFER_PREFIXES = (
    "find ", "order ", "get ", "ready ", "craving ", "discover ", "savor ",
    "savour ", "experience ", "try ", "create ", "send ", "reserve ", "claim ",
    "enter ", "download ", "start ", "view ", "see ", "book ", "welcome ",
    "what ", "why ", "how ", "where ", "when ", "is ", "are ", "we ",
    # These open a list of somebody else's business. "Related Trip Guides" on
    # maps.roadtrippers.com passed every structural test and was sold to
    # Palsaik as a product.
    "related ", "other ", "similar ", "nearby ", "more ",
)

#: Labels that are true of a page for a few minutes, or of the platform rather
#: than of any business. Grubhub renders "This menu isn't available right now"
#: as a heading on the tenant's own storefront, so structure alone cannot tell
#: it from a menu section. `classify_statement` can, and it is the reason the
#: typing tranche exists.
_NOT_DURABLE = frozenset({"page_state", "boilerplate"})

#: A rating or a count inside an interpunct run means the run is an attribute
#: strip, not a menu. Uber Eats ships "4.9 x (200+) • Japanese • Sushi • Asian •
#: Group Friendly", which read as a menu sold Asaka "Japanese" and "Asian".
_CHIP = re.compile(r"^\d|\d\.\d")

_PRICE = re.compile(r"\$\s?\d")
#: How many non-empty lines after a heading may carry its price. Three, because
#: singleplatform renders "#### Miso Soup", then a blank, then " $1.95", and
#: the section heading above it sits two dishes away from the nearest number.
_PRICE_WITHIN_LINES = 3


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", str(text).lower()))


def _offer_key(name: str) -> str:
    """What to search the corpus for when looking for this offer by name.

    Parentheticals go: the Postmates listing is "Dosirak (Set Meal)" and the
    Uber Eats one is "the Dosirak", and an inventory that cannot connect those
    two is the inventory find 818fdb2d did not have.
    """
    bare = re.sub(r"\([^)]*\)", " ", name)
    return _words(bare) or _words(name)


def _plausible_offer(name: str, *, business_key: str) -> bool:
    low = name.lower().strip()
    if not (2 <= len(name) <= MAX_OFFER_CHARS):
        return False
    if not re.search(r"[a-z]", low):
        return False
    if low in _NOT_AN_OFFER or low.startswith(_OFFER_PREFIXES) or low.endswith("?"):
        return False
    if len(name.split()) > MAX_OFFER_WORDS:
        return False
    if classify_statement(name) in _NOT_DURABLE:
        return False
    # A business does not sell itself, and its own name on a menu page is the
    # page title. Letting it through would put "Asaka" in the inventory and any
    # move mentioning the restaurant would read as already-offered.
    return not (business_key and business_key in _squash(name))


def _tidy_offer(raw: str) -> str:
    name = " ".join(str(raw).split()).strip(" .,;:*_")
    # "Dosirak (Set Meal) - 도시락세트": the tail after the dash is the same name
    # in Hangul. Keeping it would make the offer unsearchable and unprintable
    # on a card.
    head, dash, tail = name.partition(" - ")
    if dash and not re.search(r"[A-Za-z]", tail):
        name = head
    return name.strip(" .,;:*_")


def _offer_names_from(text: str, *, business_key: str,
                      url_names_tenant: bool) -> list[str]:
    """Candidate offer names in one cleaned observation.

    Two shapes, both structural rather than semantic. A model call here would
    cost a night's budget to read a menu we can read for nothing, and would give
    a different answer on every run.
    """
    names: list[str] = []
    lines = text.split("\n")

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        parts = _LIST_SEPARATOR.split(stripped)
        if len(parts) > 1:
            labelled = bool(_LIST_LABEL.match(parts[0]))
            items = [_tidy_offer(_LIST_LABEL.sub("", part, count=1))
                     for part in parts]
            usable = [item for item in items
                      if _plausible_offer(item, business_key=business_key)]
            chipped = any(_CHIP.search(item) for item in items)
            if labelled or (len(items) >= _UNLABELLED_LIST_MIN and not chipped
                            and len(usable) == len(items)):
                names.extend(usable)
                continue

        level, bare = _heading(line)
        if level < 2 or _is_chrome(line) or _is_carousel_heading(line):
            continue
        name = _tidy_offer(bare)
        if not _plausible_offer(name, business_key=business_key):
            continue
        # A heading earns its place either by carrying a price nearby — that is
        # a menu, whoever published it — or by sitting on a page the URL says
        # belongs to this tenant. Neither test alone is enough: a rival's price
        # list would pass the first, and a storefront's footer the second.
        following = [row for row in lines[index:index + 1 + _PRICE_WITHIN_LINES]
                     if row.strip()]
        if url_names_tenant or any(_PRICE.search(row) for row in following):
            names.append(name)
    return names


def _owner_offers(business: Mapping[str, Any] | None,
                  facts: Sequence[str]) -> list[str]:
    profile = (business or {}).get("profile_data") or {}
    buyers = profile.get("buyers") if isinstance(profile, Mapping) else None
    stated = list((buyers or {}).get("offers") or []) if isinstance(buyers, Mapping) else []
    for fact in facts:
        match = _SELLS_FACT.search(" ".join(str(fact).split()))
        if match:
            stated.extend(_OFFER_SPLIT.split(match.group("offers")))
    seen: dict[str, str] = {}
    for raw in stated:
        name = _tidy_offer(raw)
        if name and name.casefold() not in seen:
            seen[name.casefold()] = name
    return list(seen.values())


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _cleaned_rows(business: Mapping[str, Any] | None,
                  observations: Sequence[Any]) -> list[tuple[Any, str, str]]:
    """Each observation with its furniture removed, dropping the empties."""
    name = (business or {}).get("name")
    address = (business or {}).get("city")
    rows = []
    for observation in observations:
        url = getattr(observation, "source_url", None)
        content = str(getattr(observation, "content", "") or "")
        text = (clean_observation_text(content, business_name=name,
                                       address=address, source_url=url)
                if url else content)
        if text.strip():
            rows.append((observation, text, source_host(url) or ""))
    return rows


def build_business_state(*, business: Mapping[str, Any] | None,
                         facts: Sequence[str],
                         observations: Sequence[Any]) -> BusinessState:
    """What is already true of this business, from its profile and its memory.

    ``observations`` are stored rows, not a retrieval slice. That is deliberate:
    the three Dosirak rows Yellow Cow's corpus holds were never retrieved on the
    night the Analyst told it to launch a set meal, and an inventory that only
    knows what tonight's vector search surfaced would have made the same mistake.
    """
    business = business or {}
    name = str(business.get("name") or "this business")
    business_key = _business_key(name)
    declared = declared_domain(business, facts)
    rows = _cleaned_rows(business, observations)

    # -- offers --------------------------------------------------------
    offers: list[Offer] = [
        Offer(name=stated, origin="owner")
        for stated in _owner_offers(business, facts)
    ]
    taken = {offer.key for offer in offers}
    candidates: dict[str, str] = {}
    for observation, text, _host in rows:
        url = getattr(observation, "source_url", None)
        url_names_tenant = bool(business_key
                                and business_key in _squash(str(url or "")))
        for candidate in _offer_names_from(
                text, business_key=business_key,
                url_names_tenant=url_names_tenant):
            key = _offer_key(candidate)
            if key and key not in taken and key not in candidates:
                candidates[key] = candidate

    # Every row that names an offer is evidence for it, whether or not that row
    # is where the name was found. This is what makes the block answerable:
    # "you already sell this, and here are the ids that say so".
    market: list[Offer] = []
    for key, candidate in candidates.items():
        cited: list[str] = []
        sources: set[str] = set()
        for observation, text, host in rows:
            # Whole words, always. A substring scan would cite every row
            # containing "bar" for an offer called "Bar", and an offer whose
            # evidence is a coincidence is worse than one with a single citation.
            if f" {key} " not in f" {_words(text)} ":
                continue
            cited.append(str(getattr(observation, "observation_id", "")))
            sources.add(source_identity(getattr(observation, "source_url", None))
                        or str(getattr(observation, "observation_id", "")))
        market.append(Offer(name=candidate, origin="market",
                            observation_ids=tuple(cited), sources=len(sources)))
    market.sort(key=lambda offer: (-offer.sources, -len(offer.observation_ids),
                                   offer.name.casefold()))
    offers.extend(market[:max(0, MAX_OFFERS_SHOWN - len(offers))])

    # -- hours ---------------------------------------------------------
    by_day: dict[int, list[HoursClaim]] = {}
    undated: list[HoursClaim] = []
    # One source stating one window once. asakacatogo.com is stored twice, whole,
    # and every day it covers arrived here twice over; a page read twice is one
    # claim, which is the same rule provenance.py applies to retrieval.
    seen: set[tuple] = set()
    for observation, text, _host in rows:
        observation_id = str(getattr(observation, "observation_id", ""))
        source = source_identity(getattr(observation, "source_url", None))
        for claim in parse_hours(text):
            placed = HoursClaim(
                weekday=claim.weekday, opens=claim.opens, closes=claim.closes,
                service=claim.service, observation_id=observation_id,
                source=source or observation_id)
            fingerprint = (placed.weekday, placed.opens, placed.closes,
                           placed.service, placed.source)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            if placed.weekday is None:
                # A bare "Closed" badge with no day and no time — mapquest and
                # Apple Maps both ship one — says the page was fetched outside
                # trading hours and nothing else. Kept, it reads as a claim that
                # the business is shut.
                if placed.closed:
                    continue
                undated.append(placed)
            else:
                by_day.setdefault(placed.weekday, []).append(placed)
    days = tuple(DayHours(weekday=weekday, claims=tuple(by_day[weekday]))
                 for weekday in sorted(by_day))

    # -- domains -------------------------------------------------------
    hosts: dict[str, list[str]] = {}
    for observation, _text, host in rows:
        if host:
            hosts.setdefault(host, []).append(
                str(getattr(observation, "observation_id", "")))
    domains = tuple(
        DomainNote(host=host, declared=_is_declared(host, declared),
                   observation_ids=tuple(ids),
                   resembles_name=bool(business_key
                                       and business_key in _squash(host)))
        for host, ids in sorted(hosts.items())
    )

    return BusinessState(name=name, declared_domain=declared,
                         offers=tuple(offers), days=days,
                         undated_hours=tuple(undated), domains=domains)


# ---------------------------------------------------------------------------
# The prompt block
# ---------------------------------------------------------------------------


def _clock(minutes: int | None) -> str:
    if minutes is None:
        return "closed"
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _window(claim: HoursClaim) -> str:
    if claim.closed:
        return "closed"
    label = f"{claim.service} " if claim.service else ""
    return f"{label}{_clock(claim.opens)}–{_clock(claim.closes)}"


def _host_of_claim(claim: HoursClaim) -> str:
    source = claim.source or ""
    return source.split("/")[0].split("#")[0] or "an uncredited row"


#: How many distinct windows one weekday may print before the line is cut short.
#: Asaka's Monday carries six. The block sits above twenty retrieved rows and
#: cannot be the longest thing in the prompt.
MAX_WINDOWS_PER_DAY = 4

#: How many observation ids an offer prints. Enough for the model to cite the
#: offer without the block turning into a list of UUIDs.
MAX_OFFER_CITATIONS = 3


def _rows_phrase(count: int) -> str:
    return "1 row" if count == 1 else f"{count} rows"


def _reconciled(day: DayHours) -> str:
    """One weekday's windows, with the sources that agree gathered together.

    Four sources stating one window is agreement, and printing it four times
    invites exactly the reading find cbac2b29 gave five sources: a pattern.
    """
    grouped: dict[tuple, list[str]] = {}
    for claim in day.claims:
        hosts = grouped.setdefault(
            (claim.service, claim.opens, claim.closes), [])
        host = _host_of_claim(claim)
        if host not in hosts:
            hosts.append(host)
    windows = []
    for (service, opens, closes), hosts in list(grouped.items())[:MAX_WINDOWS_PER_DAY]:
        label = f"{service} " if service else ""
        span = "closed" if opens is None else f"{_clock(opens)}–{_clock(closes)}"
        credit = ", ".join(sorted(hosts))
        if len(hosts) > 1:
            credit = f"{len(hosts)} sources: {credit}"
        windows.append(f"{label}{span} ({credit})")
    if len(grouped) > MAX_WINDOWS_PER_DAY:
        windows.append(f"and {len(grouped) - MAX_WINDOWS_PER_DAY} more")
    return "; ".join(windows)


def describe_business_state(state: BusinessState) -> str:
    """The "what this business already has" block, or "" when nothing is known.

    Short on purpose. It repeats in every nightly prompt beside twenty
    observations, and the three failures it exists to stop need six facts, not
    a profile dump.
    """
    if not state.known:
        return ""

    lines = ["What this business already has, from its own profile and memory. "
             "This is not market opinion — it is what is already true, and a "
             "move that contradicts it is wrong before it is priced."]

    if state.declared_domain:
        lines.append(f"Declared website: {state.declared_domain}")

    if state.offers:
        lines.append("Already sells. Do not propose launching, adding or "
                     "introducing any of these — propose improving, pricing or "
                     "promoting the named offer instead:")
        for offer in state.offers:
            if offer.origin == "owner":
                lines.append(f"- {offer.name} (the owner told us so)")
            else:
                ids = ", ".join(offer.observation_ids[:MAX_OFFER_CITATIONS])
                plural = "" if offer.sources == 1 else "s"
                lines.append(f"- {offer.name} ({offer.sources} source{plural}; "
                             f"cite {ids})")

    if state.days:
        lines.append("Opening hours, reconciled per weekday. Two sources that "
                     "cover different days do not contradict each other:")
        for day in state.days:
            note = "" if day.agreed else " — these sources disagree about this day"
            lines.append(f"- {day.name}: {_reconciled(day)}{note}")

    if state.undated_hours:
        lines.append("Windows that name no weekday. They describe the day the "
                     "page was fetched and cannot contradict anything above:")
        printed: list[str] = []
        for claim in state.undated_hours:
            entry = f"- {_window(claim)} ({_host_of_claim(claim)})"
            if entry not in printed:
                printed.append(entry)
        lines.extend(printed[:6])

    undeclared = [note for note in state.domains if not note.declared]
    if undeclared:
        if state.declared_domain:
            lines.append("Pages on a domain the owner did not declare. Say "
                         "whose page you believe each one is before you "
                         "criticise anything on it, and never call one "
                         "\"your website\":")
        else:
            lines.append("The owner declared no website, so every page below is "
                         "somebody else's until you say whose it is. Never call "
                         "one \"your website\":")
        # Lookalikes first, then the hosts with the most to say. The list is
        # truncated, and it was truncating alphabetically: Palsaik has 23
        # undeclared hosts and palsaikkoreanbbqca.com — four rows, almost
        # certainly the owner's own site — fell outside the first eight and
        # vanished from the prompt entirely. Asaka's asakacatogo.com survived
        # only by starting with "asa". A warning list that drops the very rows
        # it exists to warn about is worse than no list.
        ranked = sorted(
            undeclared,
            key=lambda note: (not note.resembles_name,
                              -len(note.observation_ids), note.host),
        )
        for note in ranked[:8]:
            alike = (" — the name resembles this business, but the owner did "
                     "not declare it" if note.resembles_name else "")
            lines.append(
                f"- {note.host} ({_rows_phrase(len(note.observation_ids))}){alike}")

    return "\n".join(lines)


__all__ = [
    "BusinessState", "DayHours", "DomainNote", "HoursClaim", "MAX_OFFERS_SHOWN",
    "Offer", "WEEKDAY_NAMES", "build_business_state", "declared_domain",
    "describe_business_state", "parse_hours",
]
