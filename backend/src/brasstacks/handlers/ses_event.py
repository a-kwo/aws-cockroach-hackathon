"""Persist Amazon SES delivery/open/click events from EventBridge.

SES event delivery is best-effort and may be duplicated or out of order. The
repository stores immutable provider events keyed by EventBridge event id. The
UI derives the current email state from that timeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from brasstacks.config import Settings
from brasstacks.secrets import hydrate_environment

DETAIL_TYPE_MAP = {
    "Email Sent": "sent",
    "Email Delivered": "delivered",
    "Email Opened": "opened",
    "Email Clicked": "clicked",
    "Email Bounced": "bounced",
    "Email Complaint Received": "complaint",
    "Email Delivery Delayed": "delivery_delayed",
    "Email Rejected": "rejected",
    "Email Rendering Failed": "rendering_failed",
    "Email Subscribed": "subscribed",
}


def _first_tag(tags: Mapping[str, Any], name: str) -> str | None:
    value = tags.get(name)
    if isinstance(value, list):
        return str(value[0]) if value else None
    if value is None:
        return None
    return str(value)


def _event_time(event: Mapping[str, Any], detail: Mapping[str, Any]) -> datetime:
    raw = (
        detail.get("timestamp")
        or (detail.get("mail") or {}).get("timestamp")
        or event.get("time")
    )
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def process_event(*, repo: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    detail_type = str(event.get("detail-type") or "")
    event_type = DETAIL_TYPE_MAP.get(detail_type)
    if event_type is None:
        return {"status": "ignored", "reason": "unsupported_event", "detail_type": detail_type}
    detail = event.get("detail") or {}
    mail = detail.get("mail") or {}
    message_id = str(mail.get("messageId") or "").strip()
    provider_event_id = str(event.get("id") or "").strip()
    if not message_id or not provider_event_id:
        return {"status": "ignored", "reason": "message_or_event_id_missing"}
    tags = mail.get("tags") or {}
    tool_execution_id = _first_tag(tags, "tool_execution_id")
    # This account may send email outside Brass Tacks. The EventBridge rule is
    # account-wide, so ignore messages that do not carry Maker's deterministic
    # tool receipt tag instead of retrying unrelated SES traffic.
    if not tool_execution_id:
        return {
            "status": "ignored",
            "reason": "maker_tool_tag_missing",
            "message_id": message_id,
        }
    destinations = mail.get("destination") or []
    recipient = str(destinations[0]) if destinations else None
    click = detail.get("click") or {}
    link = str(click.get("link") or "").strip() or None
    record = repo.record_email_event(
        provider_event_id=provider_event_id,
        message_id=message_id,
        event_type=event_type,
        event_at=_event_time(event, detail),
        tool_execution_id=tool_execution_id,
        recipient=recipient,
        link=link,
        data={
            "detail_type": detail_type,
            "event_type": detail.get("eventType"),
            "region": event.get("region"),
            "bounce": detail.get("bounce"),
            "complaint": detail.get("complaint"),
            "delivery": detail.get("delivery"),
            "delivery_delay": detail.get("deliveryDelay"),
            "open": detail.get("open"),
            "click": detail.get("click"),
            "reject": detail.get("reject"),
        },
    )
    if record is None:
        # A provider event can race the final tool receipt update. EventBridge
        # retries target failures, so raise and let it redeliver rather than
        # silently losing delivery evidence.
        raise RuntimeError("email tool receipt is not visible yet")
    return {
        "status": "recorded" if record.created else "duplicate",
        "event_type": record.event_type,
        "task_id": record.task_id,
        "tool_execution_id": record.tool_execution_id,
        "message_id": record.message_id,
    }


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    hydrate_environment()
    settings = Settings.load()

    import psycopg
    from brasstacks.repository_pg import PostgresRepository

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        return process_event(repo=PostgresRepository(conn), event=event or {})


__all__ = ["handler", "process_event", "DETAIL_TYPE_MAP"]
