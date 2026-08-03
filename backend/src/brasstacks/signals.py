"""Where Radar's observations come from, and what it refuses to remember.

Signal sources are pluggable because they are the least reliable part of the
system: a search API can rate-limit, change shape, or go down. Radar treats every
source as best-effort — one failing costs us that source's signals for the night,
not the night itself.

The corpus source exists because scraping review sites violates their terms of
service. It replays a committed dataset, which is honest, reproducible for judges,
and available offline.

The second half of this module is ingest hygiene and statement typing, which are
here rather than in Radar because both are claims about a piece of text and have
to be arguable in a test without a repository, an embedder or a night.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RawSignal:
    """Something observed about the world, before it becomes memory."""

    content: str
    kind: str
    source_name: str | None = None
    source_url: str | None = None
    subject: str | None = None
    rating: float | None = None
    observed_at: datetime | None = None


@runtime_checkable
class SignalSource(Protocol):
    #: Used in the run's audit note, so a failure names the source that failed.
    name: str

    #: How long observations from this source may legally be retained, or None
    #: for "indefinitely". A source that carries a licence restriction declares
    #: it here rather than expecting Radar to know about it, so compliance
    #: travels with the source that is bound by it.
    retention_hours: int | None

    def fetch(self, *, business_name: str, city: str | None,
              limit: int) -> Sequence[RawSignal]: ...


class CorpusSignalSource:
    """Replays a committed observation corpus.

    Used for the demo and for offline development. Honest about what it is: the
    project does not scrape review platforms, and the hackathon rules invite a
    committed example dataset.
    """

    name = "corpus"
    #: Ours, and meant to accumulate forever. That is the whole point of it.
    retention_hours = None

    def __init__(self, path: Path, *, anchor: datetime | None = None) -> None:
        self._path = path
        self._anchor = anchor

    def fetch(self, *, business_name: str, city: str | None,
              limit: int) -> list[RawSignal]:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        anchor = self._anchor or datetime.now(timezone.utc)
        signals = []
        for entry in payload["observations"][:limit]:
            signals.append(RawSignal(
                content=entry["content"],
                kind=entry["kind"],
                source_name=entry.get("source_name"),
                subject=entry.get("subject"),
                rating=entry.get("rating"),
                observed_at=anchor - timedelta(days=entry.get("days_ago", 0)),
            ))
        return signals


class TavilySignalSource:
    """Live web signals via the Tavily search API.

    Deliberately narrow: it asks about the business and its immediate competitive
    context and returns result snippets as observations. Dedup downstream means
    re-seeing the same snippet nightly is free.
    """

    name = "web"
    retention_hours = None

    def __init__(self, *, api_key: str, client: Any | None = None,
                 queries: Sequence[str] | None = None) -> None:
        self._api_key = api_key
        self._client = client
        self._queries = list(queries) if queries else None

    def _default_queries(self, business_name: str, city: str | None) -> list[str]:
        where = f" {city}" if city else ""
        return [
            f"{business_name}{where} reviews",
            f"restaurants near {business_name}{where} lunch prices",
            f"{city or ''} restaurant dining trends".strip(),
        ]

    def fetch(self, *, business_name: str, city: str | None,
              limit: int) -> list[RawSignal]:
        # Imported lazily so the unit suite never needs the dependency.
        client = self._client
        if client is None:
            import httpx

            client = httpx.Client(timeout=20.0)

        queries = self._queries or self._default_queries(business_name, city)
        now = datetime.now(timezone.utc)
        signals: list[RawSignal] = []

        for query in queries:
            response = client.post(
                "https://api.tavily.com/search",
                json={"api_key": self._api_key, "query": query,
                      "max_results": max(1, limit // len(queries))},
            )
            response.raise_for_status()
            for result in response.json().get("results", []):
                content = (result.get("content") or "").strip()
                if not content:
                    continue
                signals.append(RawSignal(
                    content=content,
                    kind="trend",
                    source_name="web",
                    source_url=result.get("url"),
                    observed_at=now,
                ))

        return signals[:limit]


YELP_API_ROOT = "https://api.yelp.com/v3"

#: Yelp's API Terms of Use: "You may not cache, record, pre-fetch, or otherwise
#: store any portion of Yelp Content for a period longer than twenty-four (24)
#: hours from receipt." Business *ids* may be stored indefinitely; review text
#: may not. This number is a licence term, not a tuning knob.
YELP_RETENTION_HOURS = 24


class YelpSignalSource:
    """Reviews from the Yelp Fusion API.

    Two limits shape what this can be, and neither is a bug to work around:

    **Three reviews, truncated to 160 characters.** Yelp returns excerpts, not
    reviews, and chooses which three by its own ranking. This is a trickle of
    recent sentiment, not a corpus.

    **Twenty-four hour retention.** Yelp content may not be stored longer than
    that, which is in direct tension with a product built on permanent memory.
    The tension is resolved by declaring ``retention_hours`` and letting Radar
    purge — so Yelp informs the night it is seen and then goes, while the
    committed corpus accumulates as before.

    Because of both, this is opt-in and off by default. It is also useless for
    the demo tenant, which is fictional and therefore has no Yelp presence —
    ``fetch`` returning nothing is the expected path there, not a failure.
    """

    name = "yelp"
    retention_hours = YELP_RETENTION_HOURS

    def __init__(self, *, api_key: str, client: Any | None = None,
                 now: Any | None = None) -> None:
        self._api_key = api_key
        self._client = client
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx  # lazy, so the unit suite never needs it

        return httpx.Client(timeout=20.0)

    def _find_business_id(self, client: Any, business_name: str,
                          city: str | None) -> str | None:
        """The first search hit, or None.

        Only the first: Yelp returns neighbouring businesses too, and pulling
        reviews for the restaurant next door would poison the memory layer with
        someone else's customers — worse than having no reviews at all.
        """
        response = client.get(
            f"{YELP_API_ROOT}/businesses/search",
            headers=self._headers,
            params={"term": business_name, "location": city or "", "limit": 1},
        )
        response.raise_for_status()
        businesses = response.json().get("businesses") or []
        return businesses[0].get("id") if businesses else None

    def fetch(self, *, business_name: str, city: str | None,
              limit: int) -> list[RawSignal]:
        client = self._http()

        business_id = self._find_business_id(client, business_name, city)
        if business_id is None:
            return []

        response = client.get(
            f"{YELP_API_ROOT}/businesses/{business_id}/reviews",
            headers=self._headers,
            params={"limit": min(limit, 3), "sort_by": "newest"},
        )
        response.raise_for_status()

        signals: list[RawSignal] = []
        for review in response.json().get("reviews") or []:
            content = (review.get("text") or "").strip()
            if not content:
                continue
            signals.append(RawSignal(
                content=content,
                kind="review",
                source_name=self.name,
                source_url=review.get("url"),
                rating=float(review["rating"]) if review.get("rating") is not None else None,
                observed_at=_yelp_time(review.get("time_created")) or self._now(),
            ))
        return signals[:limit]


def _yelp_time(raw: str | None) -> datetime | None:
    """Parse Yelp's `time_created`, which carries no timezone.

    Treated as UTC. That is approximate — Yelp reports in the business's local
    time — but nothing downstream depends on sub-day precision, and inventing a
    timezone we do not know would be a worse kind of wrong.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Ingest hygiene — what never becomes memory in the first place
# ---------------------------------------------------------------------------
#
# An audit of the 124 live web observations on 2026-08-03 found roughly 40% of
# every tenant's corpus was page furniture: navigation, footers, consent text,
# and "people also viewed" carousels that put Pho So 1, Chick-fil-A and L&L
# Hawaiian inside Yellow Cow's own rows. The Analyst cannot tell that from the
# tenant's own text, so the fix belongs here and not in the prompt.
#
# Everything below is a pure function of a string plus what we already know
# about the tenant. No model call: it has to be free, offline, and identical on
# every run, or the corpus stops being reproducible.

#: How Tavily joins slices of one page that were not adjacent on it. Keeping the
#: marker is the honest thing — these fragments are not one paragraph, and
#: provenance.py exists because we once read three slices of one page as three
#: independent confirmations.
FRAGMENT_SEPARATOR = " [...] "

#: How many consecutive single-character tokens before a run is read as one
#: CSS-shredded word. Grubhub's fee banner arrives as 28 of them; the longest
#: innocent run in the live corpus is 3 ("L & L", "1835 W Redondo"). Six leaves
#: room on both sides. Word boundaries are not recoverable — the separator
#: between letters and the separator between words are the same string — so the
#: repair produces one joined token, which is still one token instead of thirty.
SHREDDED_RUN_MIN = 6

#: Words that identify a category rather than a business. Stripped before the
#: name test, or "Korean BBQ" would match every rival in Gardena.
_GENERIC_NAME_WORDS = frozenset({
    "restaurant", "restaurants", "cafe", "café", "coffee", "bar", "grill",
    "kitchen", "bbq", "barbecue", "sushi", "korean", "japanese", "cuisine",
    "food", "the", "and", "co", "inc", "llc", "shop", "house", "eatery",
    "bistro", "deli", "pizza", "sonagi",
})

#: Directionals and street-type suffixes. They carry no identity: "W" and "Blvd"
#: are shared by the tenant and by the condo listing four blocks away.
_STREET_NOISE = frozenset({
    "st", "street", "ave", "avenue", "blvd", "boulevard", "dr", "drive", "rd",
    "road", "way", "ln", "lane", "ct", "court", "pl", "place", "hwy", "highway",
    "pkwy", "ste", "suite", "unit", "apt", "n", "s", "e", "w", "ne", "nw", "se",
    "sw", "north", "south", "east", "west",
})

#: Lines that are the site, not the business. Matched on the whole line after
#: markdown heading markers and trailing punctuation are removed, because a
#: substring rule here would eat "Menu" out of the middle of a sentence.
_CHROME_LINES = frozenset({
    "sign in", "sign up", "log in", "login", "sign in to checkout", "directions",
    "advertisement", "advertisements", "read more", "more", "more photos",
    "javascript disabled", "enter an address", "enter address", "enter your code",
    "search restaurants or dishes", "search places or paste a link",
    "terms of service", "terms of use", "privacy policy", "privacy",
    "terms of service | privacy policy", "data and licenses", "about our ads",
    "do not sell", "cookie policy", "cookie preferences", "manage preferences",
    "claim this business", "claim this menu", "successfully reported!",
    "what's wrong with this menu?", "powered by tripadvisor", "reserve a table",
    "download on the app store", "get it on google play", "start new order",
    "start group order", "call now", "get directions", "your cart is empty",
    "filter", "sort by:", "next", "next »", "prev", "home", "menu", "info",
    "photos", "share", "save", "order online", "view menu", "order onlineview menu",
    "close icon", "view full infomation", "see all", "learn more", "rectangle",
    "call   menu   info", "skip to main content",
})

_CHROME_PREFIXES = (
    "skip to ", "javascript is needed to run", "© ", "copyright ",
    "we use cookies", "this site uses cookies", "accept all cookies",
    "already have an account", "don't have an account", "invalid address",
    "restaurant owners, add your location",
)

#: Scraped image alt text: "Image 5: Yorgos Burgers logo". Every one of these in
#: the live corpus belonged to a carousel card, i.e. to somebody else.
_CHROME_IMAGE = re.compile(r"^image\s*\d+", re.IGNORECASE)

#: The headings that open a block of *other people's* businesses. Named as
#: substrings because the same carousel ships under slightly different copy on
#: Grubhub, Uber Eats, Toast and Waze.
_CAROUSEL_HEADINGS = (
    "picked for you", "delivered fastest to you", "similar restaurants",
    "browse nearby", "nearby places", "people also viewed", "restaurants near you",
    "you might also like", "more like this", "related searches",
    "national favorites", "other stores nearby", "stores near", "explore options",
    "find something that will satisfy your cravings",
)

_HEADING = re.compile(r"^(#{1,6})\s*(.*)$")
_PUNCTUATION_ONLY = re.compile(r"^[\W_]+$")

#: Delivery chips ("• 14 min • 0.6 mi") and bare rating cards ("4.8 (632)").
#: Three of them in one fragment is a carousel, whatever heading it lost.
_RIVAL_CARD_MARKERS = (
    re.compile(r"^[•\s]*\d+\s*min\s*•", re.MULTILINE),
    re.compile(r"^\s*\d\.\d+\s*\(\d", re.MULTILINE),
    re.compile(r"^\s*(?:#{1,6}\s*)?\d+\.\s+\S", re.MULTILINE),
)
_RIVAL_CARD_MIN = 3

#: Grubhub and Seamless render the storefront as an H1 and every carousel card
#: as an H5. Nothing else in the live corpus uses heading level 5, so this is
#: safe to scope by host and dishonest to apply anywhere else.
_H5_CAROUSEL_HOSTS = frozenset({"grubhub.com", "seamless.com"})

#: A fragment that names nobody survives if it reads like a person talking about
#: an experience. Tripadvisor and Yelp excerpts routinely omit the restaurant,
#: and those are the most useful rows in the corpus.
_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|we|we're|my|me)\b", re.IGNORECASE)
_REVIEW_VOICE = re.compile(
    r"\b(delicious|tasty|yummy|friendly|staff|staffs|service|server|waiter|"
    r"waitress|ordered|ate|dined|recommends?|recommended|reviews?|stars?|"
    r"rating|ratings|waited|portions?|fresh|excellent|amazing|awful|terrible|"
    r"mediocre|disappoint(?:s|ed|ing)?|value|great|quality|loved|inedible)\b",
    re.IGNORECASE)


def _squash(text: str) -> str:
    """Lowercase, punctuation-free, space-free — so "YellowCowBBQ" matches."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _business_key(business_name: str | None) -> str:
    words = [word for word in re.findall(r"[A-Za-z0-9']+", business_name or "")
             if word.lower() not in _GENERIC_NAME_WORDS]
    return _squash(" ".join(words))


def _address_keys(address: str | None) -> tuple[str | None, frozenset[str]]:
    """The street number and the distinctive words of the street name.

    Both are required together. realtor.com came back for Yellow Cow because
    "1835 W 145th St" shares the street *number* with "1835 W Redondo Beach
    Blvd" — a condo four blocks away, matched on a digit.

    `business.city` carries the whole address for tenants that signed up
    through the deployed onboarding flow, and a plain city name for the seeded
    one. A city name yields no number, and then the address test simply never
    fires rather than firing wrongly.
    """
    head = (address or "").split(",")[0].strip()
    tokens = re.findall(r"[A-Za-z0-9]+", head)
    if not tokens or not tokens[0].isdigit():
        return None, frozenset()
    words = frozenset(
        token.lower() for token in tokens[1:]
        if token.isalpha() and len(token) >= 3 and token.lower() not in _STREET_NOISE
    )
    return tokens[0], words


def repair_shredded_text(text: str) -> str:
    """Rejoin a word a stylesheet exploded into one character per line.

    Grubhub's fee banner reaches us as "N\\n\\no\\n\\nf\\n\\ne..." — thirty
    tokens of one character, thirty positions in the embedding, and a
    content_hash that changes if the page ever renders it normally. Runs shorter
    than `SHREDDED_RUN_MIN` are left exactly as written: "L & L Hawaiian" and
    "1835 W Redondo Beach Blvd" are how people write, not how CSS breaks.
    """
    pieces: list[str] = []
    run: list[str] = []

    def flush() -> str:
        if not run:
            return ""
        characters = [token for token in run if token.strip()]
        trailing = run[-1] if not run[-1].strip() else ""
        if len(characters) >= SHREDDED_RUN_MIN:
            return "".join(characters) + trailing
        return "".join(run)

    for token in re.split(r"(\s+)", text):
        if not token.strip():
            (run if run else pieces).append(token)
            continue
        if len(token) == 1:
            run.append(token)
            continue
        pieces.append(flush())
        run.clear()
        pieces.append(token)
    pieces.append(flush())
    return "".join(pieces)


def _heading(line: str) -> tuple[int, str]:
    match = _HEADING.match(line.strip())
    if match is None:
        return 0, line.strip()
    return len(match.group(1)), match.group(2).strip()


def _is_chrome(line: str) -> bool:
    _, bare = _heading(line)
    if not bare:
        return True
    low = bare.lower().strip().rstrip(" .")
    if low in _CHROME_LINES or low.startswith(_CHROME_PREFIXES):
        return True
    if _CHROME_IMAGE.match(bare):
        return True
    return bool(_PUNCTUATION_ONLY.match(bare))


def _is_carousel_heading(line: str) -> bool:
    _, bare = _heading(line)
    low = bare.lower().rstrip(" .:")
    return any(heading in low for heading in _CAROUSEL_HEADINGS)


def _looks_like_rival_cards(fragment: str) -> bool:
    hits = sum(len(marker.findall(fragment)) for marker in _RIVAL_CARD_MARKERS)
    return hits >= _RIVAL_CARD_MIN


def _strip_chrome(fragment: str, *, host: str) -> str:
    """Drop furniture lines and whole carousel sections.

    A carousel heading opens a *section*, not the rest of the page:
    singleplatform.com puts "## Browse Nearby" immediately before "## General
    Info", which carries the address and the opening hours. Skipping to the end
    of the fragment would have thrown both away, so the skip stops at the next
    heading of the same or shallower level.
    """
    lines = fragment.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_carousel_heading(line):
            level, _ = _heading(line)
            index += 1
            while index < len(lines):
                next_level, _ = _heading(lines[index])
                if (next_level and (level == 0 or next_level <= level)
                        and not _is_carousel_heading(lines[index])):
                    break
                index += 1
            continue
        if host in _H5_CAROUSEL_HOSTS and _heading(line)[0] >= 5:
            index += 1
            continue
        if not _is_chrome(line):
            kept.append(line)
        index += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _host_of(url: str | None) -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def clean_observation_text(content: str, *, business_name: str | None,
                           address: str | None, source_url: str | None) -> str:
    """The text worth remembering, or "" when there is none.

    Three passes, in this order and for this reason:

    1. Repair shredding, because everything after this — the hash, the
       embedding, the classifier — sees the repaired string. A repaired and an
       unrepaired copy of one page must not both be storable.
    2. Strip chrome per fragment.
    3. Drop a fragment that names neither the tenant nor its street address and
       does not read like somebody describing a visit.

    Step 3 has one exception and one exception to the exception. A page whose
    URL names the tenant is the tenant's page, so its uncredited copy — the
    Postmates menu sections, the Instagram bio — stays. But the recommendation
    carousel on that same page is a list of other venues, and a URL must not
    launder it, so a fragment that reads as rival cards is dropped anyway.

    Measured over the 124 live web rows: 12 dropped whole (Asaka 6/44, Palsaik
    4/46, Yellow Cow 2/34) and a further 3 collapsed into an existing row by
    dedup, because the three Grubhub "independent captures" behind find
    7c4a9124 become the one page they always were. 109 of 124 rows survive.
    """
    key = _business_key(business_name)
    number, street_words = _address_keys(address)
    host = _host_of(source_url)
    url_names_tenant = bool(key and key in _squash(str(source_url or "")))

    kept: list[str] = []
    for fragment in repair_shredded_text(content).split(FRAGMENT_SEPARATOR):
        body = _strip_chrome(fragment, host=host)
        if not body:
            continue
        if key and key in _squash(body):
            kept.append(body)
            continue
        if number and number in body and any(w in body.lower() for w in street_words):
            kept.append(body)
            continue
        if _FIRST_PERSON.search(body) or _REVIEW_VOICE.search(body):
            kept.append(body)
            continue
        if url_names_tenant and not _looks_like_rival_cards(body):
            kept.append(body)
            continue
        if not key and not number:
            # We know neither the tenant's name nor its address, so there is no
            # subject test to fail. Dropping here would delete a corpus on the
            # strength of a check that never ran.
            kept.append(body)
    return FRAGMENT_SEPARATOR.join(kept)


# ---------------------------------------------------------------------------
# Statement typing — what a row asserts, as opposed to where it came from
# ---------------------------------------------------------------------------
#
# `observation.kind` answers "which source produced this", and every one of the
# 124 live web rows answers 'trend'. Reviews, opening hours, dish prices,
# marketing copy and "This menu isn't available right now" were all one label,
# so nothing downstream could tell a fact that expires in minutes from one that
# is true next month. That distinction is the whole reason this exists.

#: `unknown` is a decision — the classifier looked and found nothing it
#: recognised. A NULL in the column is different, and means we never looked.
#: `listing` is not in the vocabulary this started from. The corpus argued for
#: it: 18 of the 109 rows that survive hygiene are a directory card and nothing
#: else — name, address, phone, and what the platform lets you do about it. It
#: is durable, like `hours`, and it earned a label rather than staying `unknown`.
STATEMENT_TYPES = frozenset({
    "review", "hours", "price", "menu_item", "marketing", "page_state",
    "listing", "boilerplate", "unknown",
})

#: Which label wins when two carry the same weight of evidence. Order is by how
#: narrow the claim is: "Entrees $10-$22" pins a number, while "Entrees" is a
#: menu section heading, and both together are a price statement. `review`
#: outranks `menu_item` because a tie between somebody describing a visit and a
#: dish word appearing goes to the person — "Great value for the two meat combo"
#: is a review that mentions a dish, not a menu that mentions a diner.
_TYPE_PRECEDENCE = ("page_state", "hours", "price", "review", "menu_item",
                    "marketing", "listing", "boilerplate")

#: First person is the strongest single review signal in the corpus — nobody
#: writes "I" on a menu or in a fee disclosure — so one pronoun outweighs one
#: incidental dish word. "I paid $51 for 3 rolls" is a review, not a menu.
_FIRST_PERSON_WEIGHT = 2

#: (label, pattern, weight). Weight is 1 unless a pattern carries more evidence
#: than a single word normally does.
_STATEMENT_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    # page_state — true for minutes. Note that a bare "Closed" is deliberately
    # NOT here: "Tuesday: Closed" is an opening-hours fact, and reading it as
    # perishable would blocklist the hours table it sits in.
    ("page_state", re.compile(
        r"\b(?:is\s*n[o']t|is not|not)\s+available\s+right\s+now", re.I), 1),
    ("page_state", re.compile(
        r"\bcurrently\s+(?:unavailable|closed|not\s+accepting)", re.I), 1),
    ("page_state", re.compile(r"\bpreorder\s+for\s+\d", re.I), 1),
    ("page_state", re.compile(r"\b(?:delivery|pickup|ordering)\s+unavailable\b", re.I), 1),
    ("page_state", re.compile(r"\btoo\s+far\s+to\s+deliver\b", re.I), 1),
    ("page_state", re.compile(r"\b(?:closed|open)\s+now\b", re.I), 1),
    ("page_state", re.compile(r"\btemporarily\s+closed\b", re.I), 1),
    ("page_state", re.compile(r"\b(?:closes|opens)\s+at\s+\d", re.I), 1),
    ("page_state", re.compile(r"\b(?:sold\s+out|out\s+of\s+stock)\b", re.I), 1),
    ("page_state", re.compile(r"\bright\s+now\s*:", re.I), 1),

    # hours — durable. A staleness rule must never treat these as perishable.
    ("hours", re.compile(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b", re.I), 1),
    ("hours", re.compile(
        r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)\s*[-–—]\s*"
        r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)", re.I), 1),
    ("hours", re.compile(r"\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}"), 1),
    ("hours", re.compile(
        r"\b(?:hours\s+of\s+operation|opening\s+hours|all\s+hours)\b", re.I), 1),

    # menu_item — durable. A dish, its description, or its listed price.
    ("menu_item", re.compile(r"\$\d+\.\d{2}\b"), 1),
    ("menu_item", re.compile(
        r"\b(?:appetizers?|entrees?|desserts?|nigiri|sashimi|noodles?|soups?|"
        r"sides?|beverages?|platters?|rolls?|bibimbap|combo)\b", re.I), 1),
    ("menu_item", re.compile(r"\b(?:served\s+with|topped\s+with|comes\s+with)\b", re.I), 1),

    # price — a claim about price level rather than about a dish.
    ("price", re.compile(r"\$\d+\s*(?:and\s+under|\+)\b", re.I), 1),
    ("price", re.compile(r"\$\d+\s*[-–—]\s*\$?\d+"), 1),
    ("price", re.compile(r"(?<![\w$])\$\$+(?![\w$])"), 1),
    ("price", re.compile(r"\bprice[.:]", re.I), 1),
    ("price", re.compile(r"\b(?:delivery|service|other)\s+fees?\b", re.I), 1),
    ("price", re.compile(r"\bprices?\s+(?:have|went|are)\s+(?:definitely\s+)?"
                         r"(?:gone\s+)?up\b", re.I), 1),

    # review — somebody describing a visit, or the platform counting those.
    ("review", _FIRST_PERSON, _FIRST_PERSON_WEIGHT),
    ("review", _REVIEW_VOICE, 1),

    # marketing — the business selling itself, in its own voice.
    ("marketing", re.compile(
        r"\b(?:discover|indulge|savor|savour|crafted\s+with|unforgettable|"
        r"culinary\s+journey|welcome\s+to|place\s+your\s+online\s+order|"
        r"order\s+now|we\s+are\s+proud|our\s+story|authentic|experience\s+the|"
        r"specializ(?:e|es|ing)|since\s+\d{4}|stands\s+out\s+as|culinary|"
        r"captivat(?:e|es|ing)|extraordinary|exquisite|vibrant|delight(?:ful)?|"
        r"dining\s+experience|gem)\b", re.I), 1),

    # listing — the directory card. Durable: an address and a phone number are
    # as true next month as an hours table. The phone pattern is the load-
    # bearing one, and it is deliberately outweighed by anything more specific:
    # every hours page carries a phone number too, and filing those as a
    # listing would lose the fact the page was there to state.
    ("listing", re.compile(r"\bmore\s+info\s+about\b", re.I), 1),
    ("listing", re.compile(r"\border\s+takeout\s+or\s+delivery\b", re.I), 1),
    ("listing", re.compile(r"\bget\s+delivery\s+or\s+takeout\s+from\b", re.I), 1),
    ("listing", re.compile(r"\bin[- ]?store\s+pickup\b", re.I), 1),
    ("listing", re.compile(r"\bmeal\s+types\b", re.I), 1),
    ("listing", re.compile(
        r"\(\d{3}\)\s*\d{3}[-.\s]?\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b"), 1),

    # boilerplate — true of the platform, and of no particular business.
    ("boilerplate", re.compile(r"\bprices\s+may\s+be\s+lower\s+in[- ]store\b", re.I), 1),
    ("boilerplate", re.compile(r"\bvary\s+between\s+pickup\s+and\s+delivery\b", re.I), 1),
    ("boilerplate", re.compile(r"\bfree\s+online\s+ordering\b", re.I), 1),
    ("boilerplate", re.compile(r"\bbrowse\s+local\s+restaurants\b", re.I), 1),
    ("boilerplate", re.compile(r"\bjavascript\s+is\s+needed\b", re.I), 1),
    ("boilerplate", re.compile(r"\bto\s+support\s+your\s+local\s+restaurants\b", re.I), 1),
    ("boilerplate", re.compile(r"\bget\s+more\s+information\s+for\b", re.I), 1),
    ("boilerplate", re.compile(r"\bview\s+the\s+online\s+menu\s+of\b", re.I), 1),
    ("boilerplate", re.compile(r"\band\s+other\s+restaurants\s+in\b", re.I), 1),
    ("boilerplate", re.compile(r"\bsubject\s+to\s+change\b", re.I), 1),
    ("boilerplate", re.compile(r"\bcommission\s+free\s+and\s+go\s+directly\b", re.I), 1),
    ("boilerplate", re.compile(r"\bmake\s+the\s+most\s+informed\s+decision\b", re.I), 1),
    ("boilerplate", re.compile(r"\bgenerated\s+from\s+the\s+website\b", re.I), 1),
    ("boilerplate", re.compile(r"\ball\s+rights\s+reserved\b", re.I), 1),
    ("boilerplate", re.compile(r"\bpowered\s+by\b", re.I), 1),
)


def classify_statement(text: str) -> str:
    """What this text asserts. Deterministic, offline, free, and testable.

    No model call, on purpose. A label that changed between runs would make the
    corpus unreproducible, and a label that cost money would be the second
    Bedrock call on a path that already makes ~50.

    `page_state` takes the row unless an hours table or a menu carries at least
    as much weight. Both halves of that matter: a storefront capture that
    mentions fees, a rating and "This menu isn't available right now" has to be
    marked perishable, because that banner is exactly what a find once read as
    an outage — and an hours page that says "OPEN NOW" must not be, because
    suppressing durable facts is the failure typing exists to prevent.
    """
    scores: dict[str, int] = {}
    for label, pattern, weight in _STATEMENT_PATTERNS:
        hits = len(pattern.findall(text or ""))
        if hits:
            scores[label] = scores.get(label, 0) + hits * weight
    if not scores:
        return "unknown"

    perishable = scores.get("page_state", 0)
    if perishable and perishable >= max(scores.get("hours", 0),
                                        scores.get("menu_item", 0)):
        return "page_state"

    best = max(value for label, value in scores.items() if label != "page_state")
    for label in _TYPE_PRECEDENCE:
        if label != "page_state" and scores.get(label, 0) == best:
            return label
    return "unknown"
