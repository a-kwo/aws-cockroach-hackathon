"""Structured owner and business profiles.

Contact details are stored on ``owner_account`` and never embedded. The business
brief is stored as JSON for deterministic editing, while a sentence-shaped copy
is embedded into ``business_fact`` so Analyst and Ask can retrieve only the
relevant profile facts later.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

from .menu import MenuError, menu_from_payload, menu_to_payload

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_TEXT = 240
MAX_LIST_ITEMS = 8

CATEGORY_LABELS = {
    "restaurant_cafe": "Restaurant or café",
    "restaurant": "Restaurant or café",
    "retail": "Retail store",
    "salon_wellness": "Salon or wellness",
    "professional_services": "Professional services",
    "home_services": "Home services",
    "fitness": "Fitness or studio",
    "hospitality": "Hospitality",
    "other": "Local business",
}


class ProfileError(ValueError):
    """A profile problem safe to return to the owner."""


def clean_text(value: Any, *, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = clean_text(item, limit=120)
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
        if len(result) >= MAX_LIST_ITEMS:
            break
    return result


def normalise_website(value: Any) -> str | None:
    raw = clean_text(value, limit=500)
    if not raw:
        return None
    candidate = raw if re.match(r"^https?://", raw, re.I) else f"https://{raw}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "." not in parsed.netloc:
        raise ProfileError("enter a valid website or public business page")
    return candidate


def normalise_profile(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProfileError("send a profile object")

    raw_owner = payload.get("owner") or {}
    raw_business = payload.get("business") or {}
    raw_buyers = payload.get("buyers") or {}
    if not isinstance(raw_owner, dict) or not isinstance(raw_business, dict) or not isinstance(raw_buyers, dict):
        raise ProfileError("owner, business and buyers must be objects")

    owner_name = clean_text(raw_owner.get("name"), limit=120)
    email = clean_text(raw_owner.get("email"), limit=254).casefold()
    if not owner_name:
        raise ProfileError("the owner name is required")
    if not EMAIL_PATTERN.match(email):
        raise ProfileError("enter a valid work email address")

    business_name = clean_text(raw_business.get("name"), limit=160)
    category = clean_text(raw_business.get("category"), limit=80).casefold()
    location = clean_text(raw_business.get("location"), limit=240)
    if not business_name:
        raise ProfileError("the business name is required")
    if not category:
        raise ProfileError("the business category is required")
    if not location:
        raise ProfileError("the business location is required")

    category_label = clean_text(raw_business.get("categoryLabel"), limit=120)
    if not category_label:
        category_label = CATEGORY_LABELS.get(category, category.replace("_", " ").title())

    owner_rules = clean_list(payload.get("ownerRules"))

    # The menu arrives already parsed — the browser scanned it a step earlier
    # and the owner corrected the result. That makes it client-controlled data
    # by the time it reaches us, so it is re-validated from scratch here rather
    # than trusted. `None` means the owner skipped the scan, which is normal:
    # plenty of businesses have no menu at all.
    try:
        menu = menu_from_payload(payload.get("menu"))
    except MenuError as e:
        raise ProfileError(str(e)) from None

    profile = {
        "version": 2,
        "menu": menu_to_payload(menu) if menu else None,
        "owner": {"name": owner_name, "email": email},
        "business": {
            "name": business_name,
            "category": category,
            "categoryLabel": category_label,
            "location": location,
            "website": normalise_website(raw_business.get("website")),
        },
        "buyers": {
            "segments": clean_list(raw_buyers.get("segments")),
            "offers": clean_list(raw_buyers.get("offers")),
            "channels": clean_list(raw_buyers.get("channels")),
        },
        "objective": clean_text(payload.get("objective"), limit=240),
        "ownerRules": owner_rules,
    }
    return profile


def business_profile_data(profile: dict[str, Any]) -> dict[str, Any]:
    """The structured, non-contact part stored on ``business``."""
    canonical = normalise_profile(profile)
    data = {
        "version": canonical["version"],
        "business": {
            "categoryLabel": canonical["business"]["categoryLabel"],
            "website": canonical["business"]["website"],
        },
        "buyers": deepcopy(canonical["buyers"]),
        "objective": canonical["objective"],
    }
    # The canonical copy of every menu price, as integer cents. The observation
    # and fact rows built from it are prose for retrieval; if the two ever
    # disagree, this is the one that is right.
    if canonical.get("menu"):
        data["menu"] = deepcopy(canonical["menu"])
    return data


def profile_from_record(record: dict[str, Any], *, owner_rules: list[str] | None = None) -> dict[str, Any]:
    stored = record.get("profile_data")
    if not isinstance(stored, dict):
        stored = {}
    stored_business = stored.get("business") if isinstance(stored.get("business"), dict) else {}
    stored_buyers = stored.get("buyers") if isinstance(stored.get("buyers"), dict) else {}
    category = clean_text(record.get("category"), limit=80).casefold()
    return {
        "version": int(stored.get("version") or 2),
        "owner": {
            "name": clean_text(record.get("display_name") or record.get("username") or "Business owner", limit=120),
            "email": clean_text(record.get("email"), limit=254),
            "username": clean_text(record.get("username"), limit=64),
        },
        "business": {
            "id": clean_text(record.get("business_id"), limit=64),
            "name": clean_text(record.get("business_name"), limit=160),
            "category": category,
            "categoryLabel": clean_text(stored_business.get("categoryLabel"), limit=120)
                or CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
            "location": clean_text(record.get("city"), limit=240),
            "website": normalise_website(stored_business.get("website")) if stored_business.get("website") else None,
        },
        "buyers": {
            "segments": clean_list(stored_buyers.get("segments")),
            "offers": clean_list(stored_buyers.get("offers")),
            "channels": clean_list(stored_buyers.get("channels")),
        },
        "objective": clean_text(stored.get("objective"), limit=240),
        "ownerRules": clean_list(owner_rules or []),
        # Read back so a profile edit re-saves the menu instead of dropping it.
        # `profile_from_record` feeds the PUT path, and a missing key there
        # would silently wipe the menu on the owner's next unrelated edit.
        "menu": deepcopy(stored["menu"]) if isinstance(stored.get("menu"), dict) else None,
        "updatedAt": record.get("profile_updated_at"),
    }


__all__ = [
    "CATEGORY_LABELS", "EMAIL_PATTERN", "ProfileError", "business_profile_data",
    "clean_list", "clean_text", "normalise_profile", "normalise_website",
    "profile_from_record",
]
