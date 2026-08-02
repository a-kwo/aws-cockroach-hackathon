"""Turning a signup form into a tenant the agents can work for.

Two jobs, both of which happen exactly once per business and then never again:

* **Geocode the typed address.** The competitor scout searches by coordinates,
  and until this runs there are none. An environment-wide `PLACES_LATITUDE` was
  fine for a single hardcoded tenant and becomes wrong the moment a second one
  signs up, so the coordinates live on the business row.
* **Turn the answers into retrievable memory.** The form's fields are embedded
  as `business_fact` rows rather than stored as a blob, because the Analyst
  reaches for them the same way it reaches for a review — by cosine similarity
  against a hypothesis-shaped query. That is also why they are written as
  sentences: "segments: families" retrieves nothing.

Geocoding is best-effort. A business whose address Google cannot place is still
a business; it just has no competitor context until someone fixes the address.
Losing that must not cost the owner their signup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Places Text Search, **not** the Geocoding API. Two reasons, one practical and
#: one on the merits: Geocoding is a separate Google product that needs enabling
#: on the Cloud project independently of Places (discovered the hard way — it
#: returns REQUEST_DENIED with a 200), and Text Search resolves the *business*
#: rather than an address, so the coordinates are where the business actually
#: is rather than where the owner's typing landed.
GEOCODE_URL = "https://places.googleapis.com/v1/places:searchText"

#: Only what is needed. Same billing rule as the competitor scout: the mask
#: decides the SKU.
GEOCODE_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
])

#: Must be a value in the `fact_source` ENUM in db/schema.sql. Facts gathered at
#: signup are the owner telling us directly — the same category as a chat, in a
#: different interface. Pinned against the schema by a test, because
#: InMemoryRepository accepts any string and the first deploy did not.
FACT_SOURCE = "owner_chat"


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    #: Google's canonical form of the address, for showing back to the owner.
    formatted: str | None = None
    #: What Google thought the business was. Text Search is fuzzy — "Asaka"
    #: matches "OSAKA TON KATSU" — so the owner needs to see what was matched
    #: rather than trust that it was right.
    matched_name: str | None = None
    #: The one Places field the licence permits storing.
    place_id: str | None = None


@runtime_checkable
class Geocoder(Protocol):
    def locate(self, address: str) -> GeocodeResult | None: ...


class PlacesGeocoder:
    """Google Geocoding, on the same key the competitor scout already uses.

    Unlike Places content, a latitude and longitude derived from an address the
    owner typed about their own business is theirs, and storing it is what makes
    the scout work at all.
    """

    def __init__(self, *, api_key: str, client: Any | None = None) -> None:
        self._api_key = api_key
        self._client = client

    def locate(self, address: str) -> GeocodeResult | None:
        client = self._client
        if client is None:  # pragma: no cover - requires the dependency
            import httpx

            client = httpx.Client(timeout=15.0)

        response = client.post(
            GEOCODE_URL,
            headers={
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": GEOCODE_FIELD_MASK,
                "Content-Type": "application/json",
            },
            json={"textQuery": address, "maxResultCount": 1},
        )
        response.raise_for_status()
        payload = response.json()

        places = payload.get("places") or []
        if not places:
            return None

        location = places[0].get("location") or {}
        latitude, longitude = location.get("latitude"), location.get("longitude")
        if latitude is None or longitude is None:
            return None

        return GeocodeResult(
            latitude=float(latitude),
            longitude=float(longitude),
            formatted=places[0].get("formattedAddress"),
            matched_name=((places[0].get("displayName") or {}).get("text")),
            place_id=places[0].get("id"),
        )


def geocode_or_none(geocoder: Geocoder | None, address: str) -> GeocodeResult | None:
    """Locate the address, or give up quietly.

    Best-effort like every outside-world call in this system: an unplaceable
    address, an outage or an unconfigured key each cost the tenant its
    competitor context, never its signup.
    """
    if geocoder is None or not (address or "").strip():
        return None
    try:
        return geocoder.locate(address.strip())
    except Exception:
        return None


def _join(items: Any) -> str:
    """Human list: 'a', 'a and b', 'a, b and c'."""
    values = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f" and {values[-1]}"


def build_profile_facts(profile: dict) -> list[str]:
    """The signup answers as sentences, ready to embed.

    Sentences rather than key/value pairs because these are retrieved by
    similarity against questions like "who are our customers and what do they
    want" — a colon-delimited dump matches nothing.

    The owner's name and email are deliberately excluded. Facts are embedded
    into a vector index and surface in model prompts; contact details have no
    analytical value there and every privacy cost.
    """
    business = profile.get("business") or {}
    buyers = profile.get("buyers") or {}
    facts: list[str] = []

    name = str(business.get("name") or "").strip()
    label = str(business.get("categoryLabel")
                or business.get("category") or "").strip()
    location = str(business.get("location") or "").strip()
    if name and label and location:
        facts.append(f"{name} is a {label.lower()} in {location}.")
    elif name and label:
        facts.append(f"{name} is a {label.lower()}.")

    website = str(business.get("website") or "").strip()
    if website:
        facts.append(f"The business website is {website}.")

    segments = _join(buyers.get("segments"))
    if segments:
        facts.append(f"The customers we serve are {segments}.")

    offers = _join(buyers.get("offers"))
    if offers:
        facts.append(f"What we sell is {offers}.")

    channels = _join(buyers.get("channels"))
    if channels:
        facts.append(f"Customers find us through {channels}.")

    objective = str(profile.get("objective") or "").strip()
    if objective:
        facts.append(f"The owner's stated goal right now is to {objective}.")

    return facts


def validate(profile: dict) -> str | None:
    """The caller's mistakes, named. Returns an error message or None."""
    business = profile.get("business")
    if not isinstance(business, dict):
        return "a 'business' object is required"
    if not str(business.get("name") or "").strip():
        return "the business name is required"
    if not str(business.get("category") or "").strip():
        return "the business category is required"
    return None


__all__ = [
    "FACT_SOURCE",
    "GEOCODE_FIELD_MASK",
    "GEOCODE_URL",
    "GeocodeResult",
    "Geocoder",
    "PlacesGeocoder",
    "build_profile_facts",
    "geocode_or_none",
    "validate",
]
