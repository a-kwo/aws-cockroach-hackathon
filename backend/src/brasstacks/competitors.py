"""Who else is on the street tonight, and what they are charging.

Deliberately **not** a `SignalSource`. Everything Radar consults becomes an
observation: embedded, stored, retrieved months later. Google's Places policy
forbids precisely that — *"You must not pre-fetch, cache, or store Places API
content beyond the allowed exceptions"* — and `place_id` is the only exception.

So this is fetched at reasoning time and handed to the Analyst in its prompt.
Nothing here touches the repository, and that is a licence boundary rather than
a style preference.

It is also the right shape on the merits. Reviews are history and want to
accumulate; a rival's price is a fact about *now*, and a stale one is worse than
none. The Analyst gets today's street, alongside everything it remembers.

**Cost.** Google bills Nearby Search at the most expensive SKU any requested
field belongs to, so the field mask is a billing decision, not a convenience.
Ratings, price level and opening hours put this in the Enterprise SKU; the free
allowance is 10,000 calls per SKU per month and one nightly scan uses about 30.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"

#: Exactly the fields used below, and no more. Adding one silently raises the
#: SKU on every call. Pinned by a test for that reason.
PLACES_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.primaryTypeDisplayName",
    "places.regularOpeningHours.weekdayDescriptions",
])

#: Nearby Search caps at 20 per request and offers no pagination, so this is
#: the ceiling rather than a choice.
MAX_RESULTS = 20

_PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": "free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


@dataclass(frozen=True)
class Competitor:
    """A neighbouring business as it stands today.

    `place_id` is the only field Google permits storing. Everything else is
    live-only and must not reach the database.
    """

    place_id: str
    name: str
    rating: float | None = None
    rating_count: int | None = None
    price_level: str | None = None
    kind: str | None = None
    hours_today: str | None = None


@runtime_checkable
class CompetitorScout(Protocol):
    def scan(self, *, on: date) -> list[Competitor]: ...


def _hours_for(descriptions: Any, on: date) -> str | None:
    """Today's line out of Google's seven weekday strings.

    Twenty competitors times seven days is a great deal of prompt for no gain —
    the Analyst is reasoning about tonight.
    """
    if not descriptions:
        return None
    weekday = on.strftime("%A")
    for line in descriptions:
        if line.startswith(f"{weekday}:"):
            return line.split(":", 1)[1].strip()
    return None


class PlacesCompetitorScout:
    """Google Places Nearby Search (New).

    Searched by location rather than by a stored list of rivals: new
    competitors are exactly the thing worth noticing, and a fixed list would
    never see one open.
    """

    def __init__(self, *, api_key: str, latitude: float, longitude: float,
                 radius_m: int = 1500, exclude_names: tuple[str, ...] = (),
                 client: Any | None = None) -> None:
        self._api_key = api_key
        self._latitude = latitude
        self._longitude = longitude
        self._radius_m = radius_m
        self._exclude = {n.strip().casefold() for n in exclude_names}
        self._client = client

    def scan(self, *, on: date) -> list[Competitor]:
        client = self._client
        if client is None:  # pragma: no cover - requires the dependency
            import httpx

            client = httpx.Client(timeout=20.0)

        response = client.post(
            PLACES_NEARBY_URL,
            headers={
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": PLACES_FIELD_MASK,
                "Content-Type": "application/json",
            },
            json={
                "includedTypes": ["restaurant"],
                "maxResultCount": MAX_RESULTS,
                "rankPreference": "DISTANCE",
                "locationRestriction": {"circle": {
                    "center": {"latitude": self._latitude,
                               "longitude": self._longitude},
                    "radius": float(self._radius_m),
                }},
            },
        )
        response.raise_for_status()

        competitors: list[Competitor] = []
        for place in response.json().get("places") or []:
            name = ((place.get("displayName") or {}).get("text") or "").strip()
            # Nearby Search returns her own restaurant too. Feeding her own
            # ratings back as a rival would be quietly wrong.
            if not name or name.casefold() in self._exclude:
                continue
            competitors.append(Competitor(
                place_id=place.get("id", ""),
                name=name,
                rating=place.get("rating"),
                rating_count=place.get("userRatingCount"),
                price_level=_PRICE_LEVELS.get(place.get("priceLevel") or ""),
                kind=((place.get("primaryTypeDisplayName") or {}).get("text")),
                hours_today=_hours_for(
                    (place.get("regularOpeningHours") or {}).get("weekdayDescriptions"),
                    on),
            ))
        return competitors


class FakeCompetitorScout:
    """Offline stand-in. Pass an exception to simulate an outage."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.scans = 0

    def scan(self, *, on: date) -> list[Competitor]:
        self.scans += 1
        if isinstance(self._result, Exception):
            raise self._result
        return list(self._result)


def describe_competitors(competitors: list[Competitor]) -> str:
    """Render the street for the Analyst's prompt.

    Says "today" explicitly. This is a single snapshot with no history behind
    it — Google's terms forbid keeping one — and the Analyst must not describe
    it as a trend it cannot see.
    """
    if not competitors:
        return ""

    lines = ["Nearby restaurants, as they stand TODAY (a live snapshot — there "
             "is no history behind these numbers, so do not describe them as "
             "rising or falling):"]
    for c in competitors:
        bits = []
        if c.rating is not None:
            count = f" from {c.rating_count} reviews" if c.rating_count else ""
            bits.append(f"{c.rating}{count}")
        else:
            bits.append("no rating yet")
        if c.price_level:
            bits.append(c.price_level)
        if c.kind:
            bits.append(c.kind)
        bits.append(f"today: {c.hours_today}" if c.hours_today else "hours unknown")
        lines.append(f"- {c.name} — " + "; ".join(bits))
    return "\n".join(lines)


def build_competitor_scout(settings: Any) -> CompetitorScout | None:
    """Real scout from settings, or None when it is not configured.

    None rather than an exception: competitor scouting is an enhancement, and a
    missing key should cost the night its competitor context rather than the
    night itself.
    """
    key = getattr(settings, "google_maps_api_key", None)
    latitude = getattr(settings, "places_latitude", None)
    longitude = getattr(settings, "places_longitude", None)
    if not key or latitude is None or longitude is None:
        return None

    return PlacesCompetitorScout(
        api_key=key,
        latitude=latitude,
        longitude=longitude,
        radius_m=getattr(settings, "places_radius_m", 1500),
        exclude_names=tuple(
            n for n in (getattr(settings, "places_exclude_name", None),) if n),
    )
