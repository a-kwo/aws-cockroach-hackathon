"""Validating a model-proposed find before it becomes a stored prediction.

This is the trust boundary between the Analyst's reasoning and the ledger. Once
a find is written it is a commitment: the Meter will judge it later and the
published hit rate will reflect it. So a proposal that fails validation fails
the whole run rather than being coerced into something plausible-looking.

Pure by design — no database, no model client, no clock. `today` is injected so
the verify window is deterministic under test.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

#: A literal \uXXXX that survived JSON decoding, because the model wrote a
#: doubled backslash. Only well-formed four-hex-digit sequences match, so a
#: legitimate backslash in ordinary text is left alone.
_STRAY_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")

#: Internal observation ids belong in the Analyst trace, not in owner-facing
#: copy. Older rows can fall back from ``summary`` to a rationale that cites
#: ids such as ``(c62cebb0, 51db9a91)``. Keep those links in the database while
#: removing that implementation detail from the card.
_EVIDENCE_ID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{8}"
_EVIDENCE_GROUP = re.compile(
    rf"\s*[\(\[]\s*(?:{_EVIDENCE_ID})(?:\s*[,;]\s*(?:{_EVIDENCE_ID}))*\s*[\)\]]",
    re.IGNORECASE,
)
_BARE_EVIDENCE_ID = re.compile(
    rf"(?<![0-9A-Za-z])(?:{_EVIDENCE_ID})(?![0-9A-Za-z])", re.IGNORECASE
)

#: Ceiling on a single find's predicted daily value. A neighbourhood business
#: earning +$5,000/day from one menu change means the model confused units or
#: hallucinated; publishing it would poison the ledger.
MAX_PREDICTED_DAILY_CENTS = 1_000_000  # $10,000/day

#: The Meter needs real outcome data, which does not exist the same day. And a
#: find nobody can check within a season is not a prediction.
MIN_VERIFY_AFTER_DAYS = 1
MAX_VERIFY_AFTER_DAYS = 180

DEFAULT_EMOJI = "💡"


class InvalidFindError(ValueError):
    """The model's proposal is not trustworthy enough to store."""


@dataclass(frozen=True)
class ParsedFind:
    """A validated find, ready to be written to the `find` table."""

    emoji: str
    title: str
    #: One sentence, for the card face. The rationale is the full argument and
    #: runs to a paragraph; a paragraph on a card is what made the first real
    #: deck unreadable.
    summary: str
    rationale: str
    move: str
    predicted_daily_cents: int
    confidence: float
    verify_after: date
    #: The ids the model chose to cite, in the order it wrote them. That order
    #: is not stored: `find_evidence.rank` is the row's position in what
    #: retrieval returned, looked up per id by the Analyst.
    evidence_observation_ids: tuple[str, ...]
    #: The strongest rival reading of the same evidence, and why it was
    #: rejected. Optional rather than required: this is a prompt rule, not a
    #: gate, and losing a whole night's deck because a model skipped a field
    #: would trade a real deck for a procedural one. Absent means the model
    #: said nothing, which is not the same as having no alternative.
    alternative_explanation: str | None = None


def repair_escapes(text: str) -> str:
    """Decode ``\\uXXXX`` sequences the model left as literal characters.

    Models occasionally double-escape their own JSON, so ``json.loads`` yields
    the seven characters ``\\u2014`` rather than an em dash. Observed in
    production, where it reached the database and rendered as garbage on screen.

    Repairing here means every consumer of a find gets clean text, rather than
    each one reimplementing the same workaround.
    """
    return _STRAY_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def clean_owner_copy(text: str) -> str:
    """Remove internal evidence identifiers from owner-facing prose."""
    cleaned = _EVIDENCE_GROUP.sub("", repair_escapes(str(text or "")))
    cleaned = _BARE_EVIDENCE_ID.sub("", cleaned)
    cleaned = re.sub(r"\(\s*\)|\[\s*\]", "", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" ,;:")


#: The card face. Long enough for a real sentence, short enough that three of
#: them fit on a deck without the owner scrolling a card to learn what it says.
SUMMARY_MAX_CHARS = 180


def owner_card_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Return a short, complete owner-facing sentence without an ellipsis.

    Older rows were hard-cut at the card limit and end in an ellipsis. When a
    useful signal clause precedes an em dash or semicolon, keep that complete
    clause instead of showing a visibly truncated thought. New Analyst runs are
    already prompted to produce a complete sentence within the limit.
    """
    cleaned = " ".join(clean_owner_copy(text).split())
    if not cleaned:
        return ""

    was_truncated = cleaned.endswith("…") or cleaned.endswith("...")
    stem = re.sub(r"(?:…|\.{3})$", "", cleaned).strip()

    if was_truncated:
        for marker in (" — ", "; ", ": "):
            index = stem.find(marker)
            if index >= 60:
                stem = stem[:index]
                break

    if len(stem) > limit:
        window = stem[:limit]
        boundary = max(window.rfind(" — "), window.rfind("; "), window.rfind(": "))
        if boundary >= 60:
            stem = window[:boundary]
        else:
            stem = window.rsplit(" ", 1)[0]

    stem = stem.rstrip(" ,;:—-")
    if stem and stem[-1] not in ".!?":
        stem += "."
    return stem


def _require_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidFindError(
            f"{field} must be a non-empty string, got {value!r}"
        )
    return repair_escapes(value.strip())


def _summary(payload: Mapping[str, Any], rationale: str) -> str:
    """One sentence for the card, from the model or from the rationale.

    Optional rather than required: a row written before this field existed, or
    a model that skips it, must still render a card rather than fail the night.
    Falling back to the rationale's first sentence is what the page would have
    had to do anyway, done once here instead of in every renderer.
    """
    value = payload.get("summary")
    text = value.strip() if isinstance(value, str) and value.strip() else ""
    if not text:
        # First sentence of the argument. Not perfect, but it is the model's
        # own opening line and reads as a summary far more often than not.
        first, _, _ = rationale.partition(". ")
        text = first.strip()
        if text and not text.endswith("."):
            text += "."

    return owner_card_summary(text)


def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
    """A field the ledger can live without, cleaned the same way as the rest.

    Blank collapses to absence: an empty string in the column would read as
    "the model considered the alternative and had nothing to say", which is a
    claim, and a stronger one than the model made.
    """
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return repair_escapes(value.strip())


def _require_cents(payload: Mapping[str, Any]) -> int:
    field = "predicted_daily_cents"
    value = payload.get(field)

    # bool is an int subclass, and True is never a cent amount.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFindError(
            f"{field} must be a number of integer cents, got {value!r}. "
            "A string here usually means the model ignored the output schema."
        )

    if isinstance(value, float):
        # 2300.0 is unambiguous; 23.5 cents is not a real amount.
        if not value.is_integer():
            raise InvalidFindError(
                f"{field} must be whole cents, got {value!r}"
            )
        value = int(value)

    if value < 0:
        raise InvalidFindError(f"{field} must be non-negative, got {value}")
    if value > MAX_PREDICTED_DAILY_CENTS:
        raise InvalidFindError(
            f"{field}={value} is implausible (ceiling "
            f"{MAX_PREDICTED_DAILY_CENTS}). Likely a unit confusion — dollars "
            "reported as cents, or a monthly figure reported as daily."
        )
    return value


def _require_confidence(payload: Mapping[str, Any]) -> float:
    value = payload.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFindError(f"confidence must be a number, got {value!r}")
    if not 0.0 <= value <= 1.0:
        raise InvalidFindError(
            f"confidence must be within [0.0, 1.0], got {value}. A value like "
            "82 means the model emitted a percentage."
        )
    return float(value)


def _require_verify_after(payload: Mapping[str, Any], today: date) -> date:
    field = "verify_after_days"
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidFindError(f"{field} must be an integer, got {value!r}")
    if not MIN_VERIFY_AFTER_DAYS <= value <= MAX_VERIFY_AFTER_DAYS:
        raise InvalidFindError(
            f"{field} must be within "
            f"[{MIN_VERIFY_AFTER_DAYS}, {MAX_VERIFY_AFTER_DAYS}], got {value}"
        )
    return today + timedelta(days=value)


def _require_evidence(
    payload: Mapping[str, Any], known: Collection[str]
) -> tuple[str, ...]:
    field = "evidence_observation_ids"
    raw = payload.get(field)
    if not isinstance(raw, (list, tuple)):
        raise InvalidFindError(f"{field} must be a list, got {raw!r}")

    known_set = set(known)
    seen: list[str] = []
    for obs_id in raw:
        if not isinstance(obs_id, str):
            raise InvalidFindError(f"{field} entries must be strings, got {obs_id!r}")
        if obs_id not in known_set:
            raise InvalidFindError(
                f"{field} cites {obs_id!r}, which was not retrieved for this "
                "find. Evidence must come from the vector search results — a "
                "citation outside them is a hallucination."
            )
        if obs_id not in seen:
            seen.append(obs_id)

    if not seen:
        raise InvalidFindError(
            f"{field} must cite at least one retrieved observation. A find with "
            "no evidence cannot be defended."
        )
    return tuple(seen)


def parse_find(
    payload: Mapping[str, Any],
    *,
    today: date,
    known_observation_ids: Collection[str],
) -> ParsedFind:
    """Validate a model-proposed find.

    Args:
        payload: The model's structured output.
        today: The run date. Injected rather than read from a clock so the
            verify window is deterministic.
        known_observation_ids: Observation IDs the vector search actually
            returned. Evidence citations must fall within this set.

    Raises:
        InvalidFindError: on any field the ledger cannot safely inherit.
    """
    emoji = payload.get("emoji")
    if not isinstance(emoji, str) or not emoji.strip():
        emoji = DEFAULT_EMOJI

    rationale = _require_text(payload, "rationale")

    return ParsedFind(
        emoji=emoji.strip(),
        title=_require_text(payload, "title"),
        summary=_summary(payload, rationale),
        rationale=rationale,
        move=_require_text(payload, "move"),
        predicted_daily_cents=_require_cents(payload),
        confidence=_require_confidence(payload),
        verify_after=_require_verify_after(payload, today),
        evidence_observation_ids=_require_evidence(payload, known_observation_ids),
        alternative_explanation=_optional_text(payload, "alternative_explanation"),
    )
