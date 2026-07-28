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

    def fetch(self, *, business_name: str, city: str | None,
              limit: int) -> Sequence[RawSignal]: ...


class CorpusSignalSource:
    """Replays a committed observation corpus.

    Used for the demo and for offline development. Honest about what it is: the
    project does not scrape review platforms, and the hackathon rules invite a
    committed example dataset.
    """

    name = "corpus"

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
