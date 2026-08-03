"""Amazon SES review-email tool.

This first external Maker tool is intentionally narrow: it sends one completed
draft to the configured owner/test address. The model cannot choose the
recipient or sender, and the execution is idempotent per task.
"""

from __future__ import annotations

import html
from typing import Any, Mapping

from brasstacks.tools.base import ToolContext, ToolResult

TOOL_NAME = "ses.send_review_email"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "enabled", "on"}


class SendReviewEmailTool:
    name = TOOL_NAME

    def __init__(
        self, *, source: str | None, recipient: str | None,
        site_url: str | None = None, enabled: bool = False,
    ) -> None:
        self._source = str(source or "").strip()
        self._recipient = str(recipient or "").strip()
        self._site_url = str(site_url or "").rstrip("/")
        self._enabled = bool(enabled)

    def execute(self, context: ToolContext, payload: Mapping[str, Any]) -> ToolResult:
        title = str(payload.get("title") or "Draft ready").strip()
        body = str(payload.get("body") or "").strip()
        find_id = str(payload.get("find_id") or "").strip()
        idempotency_key = f"task:{context.task_id}:tool:{self.name}:v1"
        execution = context.repo.start_tool_execution(
            task_id=context.task_id,
            business_id=context.business_id,
            tool_name=self.name,
            idempotency_key=idempotency_key,
            input_data={
                "recipient": self._recipient,
                "title": title,
                "find_id": find_id,
            },
        )
        if execution.status in {"succeeded", "skipped"}:
            return ToolResult(
                status=execution.status, tool_name=self.name,
                execution_id=execution.execution_id,
                external_reference=execution.external_reference,
                message="existing tool receipt reused",
                data=execution.output_data,
            )
        if execution.status == "running" and not execution.created:
            # SES SendEmail has no caller-supplied idempotency token. If an
            # earlier invocation timed out after SES accepted the message but
            # before the receipt was committed, an automatic resend could email
            # the owner twice. Mark this as in-flight/in-doubt and require an
            # explicit operator retry rather than guessing.
            return ToolResult(
                status="running", tool_name=self.name,
                execution_id=execution.execution_id,
                message="existing email execution is still in progress",
                data=execution.output_data,
            )

        if not self._enabled:
            finished = context.repo.finish_tool_execution(
                execution.execution_id,
                status="skipped",
                output_data={"reason": "email_disabled"},
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
                output_data={"reason": "sender_or_recipient_missing"},
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
        task_url = (
            f"{self._site_url}/app/?task={context.task_id}"
            if self._site_url else ""
        )
        plain = "\n".join([
            "Brass Tacks finished a Maker draft.",
            "",
            title,
            "",
            body,
            "",
            (f"Review in Brass Tacks: {task_url}" if task_url else
             "Sign in to Brass Tacks to review and approve the draft."),
            "",
            f"Task receipt: {context.task_id}",
        ])
        escaped_body = html.escape(body)
        link = (
            f'<p><a href="{html.escape(task_url)}">Review this task in Brass Tacks</a></p>'
            if task_url else "<p>Sign in to Brass Tacks to review this task.</p>"
        )
        html_body = (
            "<h2>Brass Tacks Maker draft ready</h2>"
            f"<h3>{html.escape(title)}</h3>"
            f"<pre style=\"white-space:pre-wrap;font-family:Arial,sans-serif\">{escaped_body}</pre>"
            f"{link}<p>Task receipt: <code>{html.escape(context.task_id)}</code></p>"
        )
        try:
            response = client.send_email(
                Source=self._source,
                Destination={"ToAddresses": [self._recipient]},
                Message={
                    "Subject": {"Data": f"Brass Tacks draft ready: {title}", "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": plain, "Charset": "UTF-8"},
                        "Html": {"Data": html_body, "Charset": "UTF-8"},
                    },
                },
            )
        except Exception as exc:
            finished = context.repo.finish_tool_execution(
                execution.execution_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                output_data={"recipient": self._recipient},
            )
            return ToolResult(
                status="failed", tool_name=self.name,
                execution_id=finished.execution_id,
                message=finished.error, data=finished.output_data,
            )

        message_id = str(response.get("MessageId") or "")
        finished = context.repo.finish_tool_execution(
            execution.execution_id,
            status="succeeded",
            external_reference=message_id or None,
            output_data={
                "recipient": self._recipient,
                "message_id": message_id,
                "task_url": task_url or None,
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
        # The authenticated owner's profile is authoritative. The deployment
        # value remains a test fallback for imported or system-created tasks.
        recipient=recipient or env.get("MAKER_REVIEW_EMAIL", "virtual.icfd@gmail.com"),
        site_url=env.get("BRASSTACKS_SITE_URL"),
        enabled=_truthy(env.get("MAKER_EMAIL_ENABLED")),
    )


__all__ = ["SendReviewEmailTool", "build_email_tool", "TOOL_NAME"]
