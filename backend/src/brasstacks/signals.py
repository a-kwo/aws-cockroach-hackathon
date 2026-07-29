"""Where Radar's observations come from.

Signal sources are pluggable because they are the least reliable part of the
system: a search API can rate-limit, change shape, or go down. Radar treats every
source as best-effort — one failing costs us that source's signals for the night,
not the night itself.

The corpus source exists because scraping review sites violates their terms of
service. It replays a committed dataset, which is honest, reproducible for judges,
and available offline.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


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
