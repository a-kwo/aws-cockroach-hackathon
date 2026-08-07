"""Amazon SES review-email tool.

The email is a concise notification and decision prompt. The full Maker working
artifact stays in Brass Tacks, where it can be reviewed, revised, versioned, and
audited. Every rendered subject/body is stored with the tool receipt so an
operator can prove exactly what was sent.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from brasstacks.artifact_usage import artifact_use_context
from brasstacks.tools.base import ToolContext, ToolResult

TOOL_NAME = "ses.send_review_email"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "enabled", "on"}


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,;:—-") + "…"


def _questions(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    output: list[str] = []
    for item in value:
        text = _compact(item, 220)
        if text and text not in output:
            output.append(text)
        if len(output) >= 3:
            break
    return output


def render_review_email(payload: Mapping[str, Any], *, task_url: str, task_id: str) -> dict[str, str]:
    """Render the exact concise email that will be stored and sent."""
    title = _compact(payload.get("title") or "Maker draft", 110)
    summary = _compact(
        payload.get("summary") or "Maker prepared a draft for your review.", 240
    )
    review_state = str(payload.get("review_state") or "ready_for_review")
    questions = _questions(payload.get("owner_questions"))
    owner_action = _compact(
        payload.get("owner_action")
        or (
            "Answer the questions below so Maker can finish the draft."
            if review_state == "needs_owner_input"
            else "Review the draft, request a revision, or copy it when ready."
        ),
        260,
    )
    revision = max(1, int(payload.get("revision") or 1))
    use_context = artifact_use_context(
        str(payload.get("artifact_type") or "general_draft"),
        stored=(payload.get("use_context") if isinstance(payload.get("use_context"), Mapping) else None),
    )
    action_needed = review_state == "needs_owner_input"
    subject = (
        f"Action needed: {title}"
        if action_needed else f"Review ready: {title}"
    )
    heading = "Maker needs your input" if action_needed else "Your Maker draft is ready"
    button = "Answer and review" if action_needed else "Review the draft"

    question_lines = ""
    if questions:
        question_lines = "\n".join(f"- {question}" for question in questions)
    plain_parts = [
        heading,
        "",
        title,
        summary,
        "",
        "WHERE THIS DRAFT WILL BE USED",
        use_context["surface"],
        use_context["placement"],
        f"Audience: {use_context['audience']}",
        f"Status: {use_context['draft_state']}",
        "",
        "YOUR NEXT STEP",
        owner_action,
    ]
    if question_lines:
        plain_parts.extend(["", "DETAILS MAKER NEEDS", question_lines])
    plain_parts.extend([
        "",
        f"Open Brass Tacks: {task_url}" if task_url else "Sign in to Brass Tacks to continue.",
        "",
        "Nothing has been published or sent to customers.",
        f"Task receipt: {task_id} · draft revision {revision}",
    ])
    plain = "\n".join(plain_parts)

    questions_html = ""
    if questions:
        questions_html = (
            '<div style="margin:24px 0;padding:18px 20px;background:#f6f3ee;border-radius:14px">'
            '<div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:#68747a">DETAILS MAKER NEEDS</div>'
            '<ul style="margin:12px 0 0;padding-left:20px">'
            + "".join(f"<li style=\"margin:8px 0\">{html.escape(question)}</li>" for question in questions)
            + "</ul></div>"
        )
    cta = (
        f'<a href="{html.escape(task_url)}" style="display:inline-block;background:#1f292e;color:#fff;text-decoration:none;padding:14px 22px;border-radius:12px;font-weight:700">{html.escape(button)}</a>'
        if task_url else "<strong>Sign in to Brass Tacks to continue.</strong>"
    )
    html_body = f"""<!doctype html>
<html><body style="margin:0;background:#f4f1eb;font-family:Arial,sans-serif;color:#20282d">
  <div style="max-width:620px;margin:0 auto;padding:28px 16px">
    <div style="background:#ffffff;border:1px solid #e4ded5;border-radius:20px;padding:32px">
      <div style="font-size:12px;font-weight:700;letter-spacing:.12em;color:#2d7e72">BRASS TACKS · MAKER</div>
      <h1 style="font-size:26px;line-height:1.2;margin:16px 0 8px">{html.escape(heading)}</h1>
      <h2 style="font-size:19px;line-height:1.35;margin:0 0 12px">{html.escape(title)}</h2>
      <p style="font-size:16px;line-height:1.6;color:#56636a;margin:0 0 20px">{html.escape(summary)}</p>
      <div style="margin:0 0 22px;padding:16px 18px;border:1px solid #d8ebe6;border-radius:14px;background:#f3faf8">
        <div style="font-size:11px;font-weight:700;letter-spacing:.08em;color:#2d7e72">WHERE THIS DRAFT WILL BE USED</div>
        <div style="font-size:17px;font-weight:700;line-height:1.35;margin:8px 0 4px">{html.escape(use_context["surface"])}</div>
        <div style="font-size:14px;line-height:1.5;color:#56636a">{html.escape(use_context["placement"])}</div>
        <div style="font-size:12px;line-height:1.45;color:#778188;margin-top:8px">Audience: {html.escape(use_context["audience"])} · {html.escape(use_context["draft_state"])}</div>
      </div>
      <div style="border-left:4px solid #2d7e72;padding:2px 0 2px 16px;margin:0 0 22px">
        <div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:#68747a">YOUR NEXT STEP</div>
        <p style="font-size:16px;line-height:1.55;margin:8px 0 0">{html.escape(owner_action)}</p>
      </div>
      {questions_html}
      <div style="margin:26px 0">{cta}</div>
      <p style="font-size:13px;line-height:1.5;color:#778188;margin:0">Nothing has been published or sent to customers.</p>
      <p style="font-size:11px;color:#98a0a4;margin:18px 0 0">Task {html.escape(task_id)} · draft revision {revision}</p>
    </div>
  </div>
</body></html>"""
    return {"subject": subject, "plain": plain, "html": html_body}


class SendReviewEmailTool:
    name = TOOL_NAME

    def __init__(
        self, *, source: str | None, recipient: str | None,
        site_url: str | None = None, enabled: bool = False,
        configuration_set: str | None = None,
    ) -> None:
        self._source = str(source or "").strip()
        self._recipient = str(recipient or "").strip()
        self._site_url = str(site_url or "").rstrip("/")
        self._enabled = bool(enabled)
        self._configuration_set = str(configuration_set or "").strip()

    def execute(self, context: ToolContext, payload: Mapping[str, Any]) -> ToolResult:
        find_id = str(payload.get("find_id") or "").strip()
        revision = max(1, int(payload.get("revision") or 1))
        use_context = artifact_use_context(
            str(payload.get("artifact_type") or "general_draft"),
            stored=(
                payload.get("use_context")
                if isinstance(payload.get("use_context"), Mapping)
                else None
            ),
        )
        task_url = f"{self._site_url}/app/?task={context.task_id}" if self._site_url else ""
        rendered = render_review_email(
            payload, task_url=task_url, task_id=context.task_id
        )
        idempotency_key = (
            f"task:{context.task_id}:revision:{revision}:tool:{self.name}:v1"
        )
        execution = context.repo.start_tool_execution(
            task_id=context.task_id,
            business_id=context.business_id,
            tool_name=self.name,
            idempotency_key=idempotency_key,
            input_data={
                "sender": self._source,
                "recipient": self._recipient,
                "subject": rendered["subject"],
                "plain_body": rendered["plain"],
                "html_body": rendered["html"],
                "title": payload.get("title"),
                "summary": payload.get("summary"),
                "owner_action": payload.get("owner_action"),
                "owner_questions": list(_questions(payload.get("owner_questions"))),
                "review_state": payload.get("review_state"),
                "artifact_type": use_context["artifact_type"],
                "use_context": use_context,
                "revision": revision,
                "find_id": find_id,
                "artifact_id": payload.get("artifact_id"),
                "task_url": task_url or None,
            },
        )
        if execution.status in {"succeeded", "skipped"}:
            return ToolResult(
                status=execution.status,
                tool_name=self.name,
                execution_id=execution.execution_id,
                external_reference=execution.external_reference,
                message="existing tool receipt reused",
                data=execution.output_data,
            )
        if execution.status == "running" and not execution.created:
            return ToolResult(
                status="running",
                tool_name=self.name,
                execution_id=execution.execution_id,
                message="existing email execution is still in progress",
                data=execution.output_data,
            )

        if not self._enabled:
            finished = context.repo.finish_tool_execution(
                execution.execution_id,
                status="skipped",
                output_data={"reason": "email_disabled", "revision": revision},
            )
            return ToolResult(
                status="skipped", tool_name=self.name,
                execution_id=finished.execution_id,
                message="Review email is disabled.", data=finished.output_data,
            )
        if not self._source or not self._recipient:
            finished = context.repo.finish_tool_execution(
                execution.execution_id,
                status="skipped",
                output_data={"reason": "sender_or_recipient_missing", "revision": revision},
            )
            return ToolResult(
                status="skipped", tool_name=self.name,
                execution_id=finished.execution_id,
                message="SES sender or review recipient is not configured.",
                data=finished.output_data,
            )

        client = context.clients.get("ses")
        if client is None:
            raise RuntimeError("SES client is not available")
        request: dict[str, Any] = {
            "Source": self._source,
            "Destination": {"ToAddresses": [self._recipient]},
            "Message": {
                "Subject": {"Data": rendered["subject"], "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": rendered["plain"], "Charset": "UTF-8"},
                    "Html": {"Data": rendered["html"], "Charset": "UTF-8"},
                },
            },
            "Tags": [
                {"Name": "task_id", "Value": context.task_id},
                {"Name": "business_id", "Value": context.business_id},
                {"Name": "tool_execution_id", "Value": execution.execution_id},
                {"Name": "revision", "Value": str(revision)},
            ],
        }
        if self._configuration_set:
            request["ConfigurationSetName"] = self._configuration_set
        try:
            response = client.send_email(**request)
        except Exception as exc:
            finished = context.repo.finish_tool_execution(
                execution.execution_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                output_data={
                    "sender": self._source,
                    "recipient": self._recipient,
                    "subject": rendered["subject"],
                    "revision": revision,
                },
            )
            return ToolResult(
                status="failed", tool_name=self.name,
                execution_id=finished.execution_id,
                message=finished.error, data=finished.output_data,
            )

        message_id = str(response.get("MessageId") or "")
        accepted_at = datetime.now(timezone.utc).isoformat()
        finished = context.repo.finish_tool_execution(
            execution.execution_id,
            status="succeeded",
            external_reference=message_id or None,
            output_data={
                "sender": self._source,
                "recipient": self._recipient,
                "subject": rendered["subject"],
                "message_id": message_id,
                "task_url": task_url or None,
                "revision": revision,
                "delivery_status": "accepted",
                "accepted_at": accepted_at,
                "configuration_set": self._configuration_set or None,
            },
        )
        return ToolResult(
            status="succeeded", tool_name=self.name,
            execution_id=finished.execution_id,
            external_reference=message_id or None,
            message=f"Review email accepted for {self._recipient}.",
            data=finished.output_data,
        )


def build_email_tool(
    env: Mapping[str, str], *, recipient: str | None = None,
) -> SendReviewEmailTool:
    return SendReviewEmailTool(
        source=env.get("MAKER_EMAIL_FROM"),
        recipient=recipient or env.get("MAKER_REVIEW_EMAIL", "virtual.icfd@gmail.com"),
        site_url=env.get("BRASSTACKS_SITE_URL"),
        enabled=_truthy(env.get("MAKER_EMAIL_ENABLED")),
        configuration_set=env.get("MAKER_EMAIL_CONFIGURATION_SET"),
    )


__all__ = [
    "SendReviewEmailTool",
    "build_email_tool",
    "render_review_email",
    "TOOL_NAME",
]
