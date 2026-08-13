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


#: "ask my produce rep: …", "tell my fish rep the order is late".
_REP_FORM = re.compile(
    r"^(?:message|tell|ask|email)\s+(?:my\s+)?(?P<target>.+?)\s+reps?\b"
    r"\s*[:,\-—]?\s*(?P<gist>.*)$",
    re.IGNORECASE | re.DOTALL)

#: "Message Dana: the gate code changed". The colon is load-bearing: without
#: it, ordinary questions that happen to start with "ask" would be stolen
#: from the order parser and the Ask agent.
_NAMED_FORM = re.compile(
    r"^(?:message|tell|ask|email)\s+(?P<target>[^:]{1,60}?)\s*:\s*"
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
        for row in rows:
            name = str(row.get("name") or "").strip().lower()
            category = str(row.get("category") or "").strip().lower()
            if target and (target == category or target in name):
                return {"contact": row, "gist": gist}
        if pattern is _REP_FORM:
            #  They said "rep" explicitly, so this was a rep message even
            #  though nobody matched. The named form without a match is more
            #  likely not about a rep at all — let it fall through.
            return {"unknown_target": found.group("target").strip()}
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


__all__ = ["compose_rep_message", "match_rep_message"]
