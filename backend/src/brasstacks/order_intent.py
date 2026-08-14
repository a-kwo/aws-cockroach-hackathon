"""Reading an owner's words as a shopping list.

"Order 20lb of tomatoes and a case of olive oil" is the most natural way to ask
for something and the least structured. A model does the reading; this module
does the distrusting.

Everything the model returns is untrusted input. It can name an item that was
never mentioned, invent a quantity, return "3" where 3 was meant, or decide a
question about last month's takings was a request to buy something. Parsing is
therefore generous about *shape* — numeric strings are fine, a missing quantity
means one — and unforgiving about *substance*: anything ambiguous stops here
rather than turning into a cart.

There is deliberately no ceiling on quantity. The protection against an absurd
order is that the cart gets priced and ``purchase_authority`` refuses it; a
second, arbitrary limit here would only add a worse one.

See docs/ORDERS_AGENT.md section 5.1.
"""

from __future__ import annotations

from typing import Any, Mapping

from brasstacks.agents.quartermaster import TRIGGER_OWNER_INSTRUCTION, OrderRequest

MAX_TOKENS = 600

ORDER_INTENT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    #  Required by the structured-output API on every object, not a style
    #  choice: omitting it is a 400 on each live call, which the ask
    #  handler's keyword fallback then swallows without a trace.
    "additionalProperties": False,
    "properties": {
        "is_order_request": {
            "type": "boolean",
            "description": "True only if the owner is asking to buy something.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The item, as plainly as possible.",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "How many units. Omit if unstated.",
                    },
                },
                "required": ["name"],
            },
        },
        "category": {
            "type": ["string", "null"],
            "description": "A broad grouping such as produce or dry goods.",
        },
    },
    "required": ["is_order_request", "items"],
}

SYSTEM = """You read a small business owner's message and decide whether they \
are asking to order supplies.

Extract only what they actually asked for. Never add an item they did not \
mention, never guess a brand, and never substitute something similar for \
something you think is unavailable — the owner approves the cart later and must \
recognise it.

If the message is a question, a comment, or anything other than a request to \
buy something, set is_order_request to false and return no items.

If a quantity is not stated, omit it rather than guessing a number."""


class NotAnOrderRequest(ValueError):
    """The message was not a request to buy anything."""


def _quantity(raw: Any, *, name: str) -> int:
    if raw is None:
        # "Order some olive oil" has no number in it. One is the smallest
        # useful assumption, and the price still passes the spend limit.
        return 1
    if isinstance(raw, bool):
        raise ValueError(f"quantity for {name!r} was a boolean")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text.lstrip("+-").isdigit():
            raise ValueError(
                f"quantity for {name!r} was {raw!r}, which is not a number")
        value = int(text)
    else:
        # Floats included: 2.5 boxes of tomatoes is not a thing to order, and
        # rounding it would be inventing what the owner meant.
        raise ValueError(
            f"quantity for {name!r} was {type(raw).__name__}, expected a whole "
            "number"
        )
    if value <= 0:
        raise ValueError(
            f"quantity for {name!r} was {value}, which is not orderable")
    return value


def parse_order_request(text: str, *, reasoner: Any) -> OrderRequest:
    """Read the owner's message into an :class:`OrderRequest`.

    Raises :class:`NotAnOrderRequest` when the message was not asking to buy
    anything, and ``ValueError`` when the model's answer cannot be trusted.
    Model failures propagate — a parse that failed must never be mistaken for a
    successful parse of nothing.
    """
    message = str(text or "").strip()
    if not message:
        raise ValueError("nothing to parse: the message was empty")

    answer = reasoner.complete_json(
        system=SYSTEM,
        user=message,
        schema=ORDER_INTENT_SCHEMA,
        max_tokens=MAX_TOKENS,
    )

    if not isinstance(answer, Mapping):
        raise ValueError(
            f"expected a JSON object from the model, got {type(answer).__name__}")
    if "is_order_request" not in answer:
        raise ValueError("model answer omitted is_order_request")
    if not answer.get("is_order_request"):
        raise NotAnOrderRequest("that did not look like a request to buy anything")

    raw_items = answer.get("items")
    if not isinstance(raw_items, list):
        raise ValueError(
            f"expected items to be a list, got {type(raw_items).__name__}")

    # Merged rather than appended, so "tomatoes and more tomatoes" cannot become
    # two lines that each look small next to a limit the total would breach.
    merged: dict[str, int] = {}
    order: list[str] = []
    for entry in raw_items:
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"expected each item to be an object, got "
                f"{type(entry).__name__}")
        name = " ".join(str(entry.get("name") or "").strip().lower().split())
        if not name:
            raise ValueError("an item came back without a name")
        quantity = _quantity(entry.get("quantity"), name=name)
        if name not in merged:
            merged[name] = 0
            order.append(name)
        merged[name] += quantity

    if not merged:
        raise NotAnOrderRequest(
            "the message asked to order something but named nothing")

    category = answer.get("category")
    category = str(category).strip() if isinstance(category, str) and category.strip() else None

    return OrderRequest(
        items=tuple((name, merged[name]) for name in order),
        trigger=TRIGGER_OWNER_INSTRUCTION,
        category=category,
        # The owner's own words, kept for the approval screen. They should see
        # what they typed, not the model's paraphrase of it.
        note=message,
    )


__all__ = [
    "MAX_TOKENS",
    "NotAnOrderRequest",
    "ORDER_INTENT_SCHEMA",
    "SYSTEM",
    "parse_order_request",
]
