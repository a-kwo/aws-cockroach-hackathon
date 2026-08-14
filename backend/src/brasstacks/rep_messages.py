"""Messages from the agent to a human sales rep.

The composition is a deterministic template, not a model call, and that is a
safety decision as much as a testing one: the owner's words travel verbatim,
so nothing can be put in their mouth, and the signature discloses that an
agent carried the message and that the owner approved it — the same promise
the order email makes in ``ordering.EmailOrderingTool``. A rep who replies
deserves to know exactly who they are talking to.

Sending lives elsewhere. This module only writes; the handler owns the one
dangerous verb, behind the owner's explicit approval.
"""

from __future__ import annotations

import re


#: The verb picks the channel. Plain verbs leave it to the contact's
#: channels; "email" forces email; "whatsapp"/"text" force WhatsApp; and
#: "imessage" is recognised so it can be refused with the real reason —
#: Apple offers no API a server could call.
_VERB_CHANNELS = {
    "email": "email",
    "whatsapp": "whatsapp",
    "text": "whatsapp",
    "imessage": "imessage",
}

_VERBS = r"(?P<verb>message|tell|ask|email|whatsapp|text|imessage)"

#: "ask my produce rep: …", "tell my fish rep the order is late".
_REP_FORM = re.compile(
    r"^" + _VERBS + r"\s+(?:my\s+)?(?P<target>.+?)\s+reps?\b"
    r"\s*[:,\-—]?\s*(?P<gist>.*)$",
    re.IGNORECASE | re.DOTALL)

#: "Message Dana: the gate code changed". The colon is load-bearing: without
#: it, ordinary questions that happen to start with "ask" would be stolen
#: from the order parser and the Ask agent.
_NAMED_FORM = re.compile(
    r"^" + _VERBS + r"\s+(?P<target>[^:]{1,60}?)\s*:\s*"
    r"(?P<gist>.+)$",
    re.IGNORECASE | re.DOTALL)


def match_rep_message(text: str, contacts) -> dict | None:
    """Whether the owner is asking to message a rep, decided without a model.

    Returns ``None`` when this is not a rep message at all (the text falls
    through to order parsing); ``{"unknown_target": …}`` when a rep was named
    but nobody on file matches — an honest miss the caller must surface,
    because silently ordering instead would be the worst wrong guess; and
    ``{"contact", "gist"}`` on a hit.
    """
    cleaned = str(text or "").strip()
    rows = list(contacts or [])
    for pattern in (_REP_FORM, _NAMED_FORM):
        found = pattern.match(cleaned)
        if not found:
            continue
        target = found.group("target").strip().lower()
        gist = found.group("gist").strip()
        channel = _VERB_CHANNELS.get(found.group("verb").lower())
        for row in rows:
            name = str(row.get("name") or "").strip().lower()
            category = str(row.get("category") or "").strip().lower()
            if target and (target == category or target in name):
                return {"contact": row, "gist": gist, "channel": channel}
        if pattern is _REP_FORM:
            #  They said "rep" explicitly, so this was a rep message even
            #  though nobody matched. The named form without a match is more
            #  likely not about a rep at all — let it fall through.
            return {"unknown_target": found.group("target").strip(),
                    "channel": channel}
    return None


def compose_rep_message(*, contact_name: str, gist: str,
                        business_name: str = "") -> dict[str, str]:
    """The email a rep receives, as ``{"subject", "body"}``.

    ``gist`` is the owner's message exactly as they gave it. It is framed,
    never rewritten.
    """
    text = str(gist or "").strip()
    if not text:
        raise ValueError("a message needs something to say")
    who = str(business_name or "").strip() or "a Brass Tacks business"
    name = str(contact_name or "").strip() or "there"
    subject = f"Message from {who}"
    body = (
        f"Hello {name},\n\n"
        f"A message from {who}:\n\n"
        f"{text}\n\n"
        "Sent by the owner's Brass Tacks supply agent, after the owner "
        "approved this message. Replies go to the owner."
    )
    return {"subject": subject, "body": body}


def compose_rep_text(*, gist: str, business_name: str = "") -> str:
    """The WhatsApp form: the same two promises, sized for a chat bubble.

    No letter framing — a greeting and sign-off read strangely in a thread —
    but the owner's words still travel verbatim, and the suffix still names
    the business, the agent, and the approval.
    """
    text = str(gist or "").strip()
    if not text:
        raise ValueError("a message needs something to say")
    who = str(business_name or "").strip() or "a Brass Tacks business"
    return (
        f"{text}\n\n— {who}, sent by the owner's Brass Tacks supply agent "
        "after the owner approved this message."
    )


#: A gist that OPENS with an order verb is a cart; anything else is prose.
#: "any tomatoes this week?" names an item but asks a question — turning it
#: into a cart would be the misfiling this boundary exists to prevent.
_CART_OPENERS = re.compile(r"^(?:order|buy|send|get|restock)\b", re.IGNORECASE)


def wants_cart(gist: str) -> bool:
    return bool(_CART_OPENERS.match(str(gist or "").strip()))


def _money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def compose_rep_order(*, contact_name: str, lines, total_cents: int,
                      business_name: str = "") -> dict[str, str]:
    """The email form of a structured cart to a human supplier.

    Prices are estimates at last known catalogue prices — the supplier
    invoices directly, which is why no card is ever charged on this path —
    and the disclosure is the same one every agent-carried mail wears.
    """
    if not lines:
        raise ValueError("an order needs at least one line")
    who = str(business_name or "").strip() or "a Brass Tacks business"
    name = str(contact_name or "").strip() or "there"
    listed = "\n".join(
        f"  - {line['name']}: {line['quantity']}" for line in lines)
    subject = f"Supply order from {who}"
    body = (
        f"Hello {name},\n\n{who} would like to order:\n\n{listed}\n\n"
        f"Estimated value {_money(total_cents)} at last known prices — "
        "please invoice as usual.\n\n"
        "Sent by the owner's Brass Tacks supply agent, after the owner "
        "approved this order. Replies go to the owner."
    )
    return {"subject": subject, "body": body}


def compose_rep_order_text(*, lines, total_cents: int,
                           business_name: str = "") -> str:
    """The WhatsApp form of the same cart, sized for a chat bubble."""
    if not lines:
        raise ValueError("an order needs at least one line")
    who = str(business_name or "").strip() or "a Brass Tacks business"
    listed = "\n".join(
        f"- {line['name']}: {line['quantity']}" for line in lines)
    return (
        f"Order from {who}:\n{listed}\n"
        f"Estimated {_money(total_cents)} at last known prices — please "
        "invoice as usual.\n\n— sent by the owner's Brass Tacks supply "
        "agent after the owner approved this order."
    )


__all__ = [
    "compose_rep_message",
    "compose_rep_order",
    "compose_rep_order_text",
    "compose_rep_text",
    "match_rep_message",
    "wants_cart",
]
