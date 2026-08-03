"""Which retrieved rows are the same source, and which only look alike.

Find 7c4a9124 opened by citing "three separate captures of your delivery
storefront" — three rows carrying one URL and one ``observed_at`` to the
microsecond, sharing a 492-character prefix. They were three overlapping
fragments of one fetch of one page. The fourth cited row was the same store on
seamless.com, which is Grubhub under its other brand, store id 2033337 in both
URLs. Four of five cited rows were one storefront, and the find read as four
independent confirmations.

Identity lives here rather than in the retrieval SQL for two reasons: the rule
that merges a mirror is a claim about the world and has to be arguable in a
test, and burying it in a query would make it invisible to the receipt the
Analyst writes about what it dropped.

``source_identity`` returns ``None`` for a row with no usable URL. ``None`` means
*ungrouped*, never *grouped with every other row that has no URL* — the seeded
corpus and every owner upload have no URL, and giving them a shared identity
would hand a per-source cap most of memory.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

#: Hosts that serve one storefront under two brands. Grubhub bought Seamless and
#: kept both front doors over one catalogue, down to the store id in the path.
#: A pair belongs here only once the shared id has actually been seen, because
#: a wrong entry silently merges two real sources into one and hides a signal.
MIRROR_HOSTS = {
    "seamless.com": "grubhub.com",
}

#: The hosts the store-id rule is allowed to apply to — both sides of every
#: mirror pair. It has to be this narrow. Applied to any host, a trailing
#: four-digit segment reads a YEAR as a store id, so ``/guides/2026`` and
#: ``/news/2026`` on one magazine become one source and take a cap slot from
#: each other. Editorial URLs ending in a year are exactly what Radar's local
#: trends query returns.
MIRROR_PLATFORMS = frozenset(MIRROR_HOSTS) | frozenset(MIRROR_HOSTS.values())

#: Query keys that name a campaign rather than a page. Dropping them keeps one
#: page one source when Radar picks it up from an email link one night and a
#: plain link the next.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
})

#: How many digits a trailing path segment needs before it is read as a store
#: id. Four, because /page/2 and /reviews/12 are pagination, and treating those
#: as store ids would merge unrelated pages across an entire site.
STORE_ID_DIGITS = 4

_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)


def _parts(url: str | None):
    """Split a stored URL, or return ``None`` if it is not a web page."""
    text = str(url or "").strip()
    if not text:
        return None
    scheme = _SCHEME.match(text)
    if scheme is not None and scheme.group(1).lower() not in ("http", "https"):
        # An owner upload or a hand-seeded row can carry anything; a mailto: is
        # not a page and must not become a group everything else falls into.
        return None
    if scheme is None and not text.startswith("//"):
        # Rows seeded as "grubhub.com/restaurant/..." are still a page.
        text = "//" + text
    parts = urlsplit(text)
    return parts if parts.netloc else None


def source_host(url: str | None) -> str | None:
    """The site an owner would name, or ``None`` when there is no usable URL."""
    parts = _parts(url)
    if parts is None:
        return None
    try:
        host = (parts.hostname or "").strip(".")
    except ValueError:  # malformed authority, e.g. a bad IPv6 literal
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _stable_query(query: str) -> str:
    kept = sorted(
        (key, value) for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    )
    return urlencode(kept)


def source_identity(url: str | None) -> str | None:
    """A key two rows share when they came from the same page or storefront.

    Within a platform known to mirror itself, a trailing store id is the
    identity: ``/restaurant/rosas/2033337`` and ``/menu/rosas/2033337`` are one
    menu. Across unrelated hosts the same number is a coincidence, so the
    platform stays part of the key — merging on the digits alone would hide a
    genuinely second source.
    """
    host = source_host(url)
    if host is None:
        return None
    parts = _parts(url)
    platform = MIRROR_HOSTS.get(host, host)

    segments = [segment for segment in (parts.path or "").split("/") if segment]
    if (platform in MIRROR_PLATFORMS
            and segments and segments[-1].isdigit()
            and len(segments[-1]) >= STORE_ID_DIGITS):
        return f"{platform}#{segments[-1]}"

    path = "/".join(segment.lower() for segment in segments)
    query = _stable_query(parts.query or "")
    return f"{platform}/{path}" + (f"?{query}" if query else "")


__all__ = ["MIRROR_HOSTS", "MIRROR_PLATFORMS", "STORE_ID_DIGITS",
           "TRACKING_PARAMS", "source_host", "source_identity"]
