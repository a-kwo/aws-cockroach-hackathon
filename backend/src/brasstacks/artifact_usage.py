"""Deterministic owner-facing placement metadata for Maker artifacts.

Maker may choose only a bounded artifact type. The application translates that
value into a safe description of where the draft belongs, who will see it, and
what owner approval is still required. These labels are presentation metadata
only: they never select an account, recipient list, URL, or external destination
for execution.
"""

from __future__ import annotations

from typing import Any, Mapping

ARTIFACT_USE_CONTEXTS: dict[str, dict[str, str]] = {
    "customer_email": {
        "surface": "Customer email",
        "placement": "An email message or campaign in the approved email platform",
        "audience": "Only the customer segment you choose",
        "visibility": "Private delivery to selected recipients",
        "draft_state": "Not sent",
        "owner_gate": "Choose the recipients and approve the final message before sending.",
        "icon": "@",
    },
    "google_business_post": {
        "surface": "Google Business Profile",
        "placement": "A public update on the selected business location",
        "audience": "People viewing that business profile on Google",
        "visibility": "Public after owner approval",
        "draft_state": "Not published",
        "owner_gate": "Nothing becomes public until you review and confirm publishing.",
        "icon": "G",
    },
    "review_reply": {
        "surface": "Customer review platform",
        "placement": "A reply beneath the matching customer review",
        "audience": "The reviewer and future customers reading the review",
        "visibility": "Public after owner approval",
        "draft_state": "Not posted",
        "owner_gate": "Match the draft to the correct review and approve it before posting.",
        "icon": "R",
    },
    "menu_update": {
        "surface": "Menu or ordering system",
        "placement": "The matching menu item, section, or ordering screen",
        "audience": "Customers deciding what to order",
        "visibility": "Customer-facing after owner approval",
        "draft_state": "Not applied",
        "owner_gate": "Confirm the item, price, and availability before applying the change.",
        "icon": "M",
    },
    "offer_package": {
        "surface": "Promotion package",
        "placement": "The owner-selected mix of posts, email, website, or in-store materials",
        "audience": "The customer segment targeted by the offer",
        "visibility": "Depends on the channels you approve",
        "draft_state": "Not launched",
        "owner_gate": "Choose the channels, dates, and audience before launching the offer.",
        "icon": "P",
    },
    "staff_checklist": {
        "surface": "Staff workflow",
        "placement": "A shift checklist, team task board, or printed operating sheet",
        "audience": "The staff responsible for the work",
        "visibility": "Internal",
        "draft_state": "Not assigned",
        "owner_gate": "Assign an owner and due time before the relevant shift.",
        "icon": "✓",
    },
    "operating_plan": {
        "surface": "Internal operations plan",
        "placement": "The owner calendar, task system, or operating playbook",
        "audience": "The owner and assigned staff",
        "visibility": "Internal",
        "draft_state": "Not scheduled",
        "owner_gate": "Choose owners and dates before putting the plan into operation.",
        "icon": "O",
    },
    "general_draft": {
        "surface": "Owner-selected destination",
        "placement": "The business tool or channel named in the approved recommendation",
        "audience": "The intended customer, staff member, or owner",
        "visibility": "Draft only until a destination is confirmed",
        "draft_state": "Draft only",
        "owner_gate": "Confirm the destination and audience before using the draft.",
        "icon": "D",
    },
}

_ALLOWED_FIELDS = (
    "surface",
    "placement",
    "audience",
    "visibility",
    "draft_state",
    "owner_gate",
    "icon",
)


def artifact_use_context(
    artifact_type: str | None,
    *,
    stored: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return a complete, bounded placement description.

    Persisted metadata may refine wording from an older or future producer, but
    only known owner-facing text fields are accepted. Missing or malformed
    values fall back to the deterministic map.
    """

    normalized = str(artifact_type or "general_draft").strip()
    if normalized not in ARTIFACT_USE_CONTEXTS:
        normalized = "general_draft"
    result = dict(ARTIFACT_USE_CONTEXTS[normalized])
    if isinstance(stored, Mapping):
        for key in _ALLOWED_FIELDS:
            value = " ".join(str(stored.get(key) or "").split())
            if value:
                result[key] = value[:280]
    result["artifact_type"] = normalized
    return result


__all__ = ["ARTIFACT_USE_CONTEXTS", "artifact_use_context"]
