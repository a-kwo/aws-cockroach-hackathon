"""Menu photos in, a reviewable item list back. Writes nothing.

Dispatched from `handlers.onboarding.handler` on `/onboarding/menu-scan`, the
same way `/profile` is: it needs the same settings, session check and
connection, and does not justify a second container image.

Deliberately read-only. This runs partway through signup, *before* the
business row exists, so there is nothing to attach a menu to yet — the owner
corrects the parsed list in the browser and it arrives again on the signup
POST, where it is re-validated and stored in the same transaction as
everything else.

Two constraints shape the implementation:

* **It costs real money per call and the URL is public.** A vision call over
  four photos is the most expensive thing an unauthenticated stranger could
  make this system do, so the session check happens before the model call, not
  after.
* **API Gateway cuts the integration off at 30 seconds**, and that is the hard
  ceiling for an HTTP API. The function's own timeout is held just under it at
  29s so a slow scan dies when the browser is told it failed — left higher, the
  invocation ran on unwatched, finished its Claude call, and billed for a result
  nobody was waiting for. Transcription is not deep reasoning, so this runs at
  low effort for the same reason `DEFAULT_ASK_EFFORT` does: the thinking tokens
  buy nothing here and the seconds are the whole budget.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from brasstacks.auth import token_fingerprint
from brasstacks.handlers.login import bearer_token
from brasstacks.handlers.onboarding import parse_body, respond
from brasstacks.menu import MenuError, menu_to_payload, normalise_images, parse_menu
from brasstacks.providers import ProviderError

#: Transcription, not reasoning. See the module docstring.
MENU_SCAN_EFFORT = "low"


def scan_menu(event: Any, *, repo: Any, reasoner: Any,
              now: datetime | None = None) -> dict[str, Any]:
    """Parse menu photographs and hand the items back for the owner to check."""
    # Authorise first: everything below this line spends money.
    token = bearer_token(event)
    account = repo.account_for_session(
        token_fingerprint(token), now=now or datetime.now(timezone.utc)
    ) if token else None
    if account is None:
        return respond(401, {"error": "sign in first"})

    try:
        payload = parse_body(event)
    except ValueError as e:
        return respond(400, {"error": str(e)})

    try:
        images = normalise_images(payload.get("images"))
    except MenuError as e:
        return respond(400, {"error": str(e)})

    try:
        menu = parse_menu(images, reasoner=reasoner)
    except MenuError as e:
        # An unreadable photo is the owner's problem to fix, and they can:
        # a straighter, closer shot usually works. Saying so beats a 500.
        return respond(400, {"error": str(e)})
    except ProviderError as e:
        # The model refused, timed out, or returned something unparseable.
        # None of those are the owner's fault, so do not blame the photo.
        return respond(502, {"error": f"could not read the menu right now ({e})"})

    return respond(200, {
        "menu": menu_to_payload(menu),
        # Echoed so the review step can say "12 items, 3 without a printed
        # price" instead of making the owner count rows to find the gaps.
        "item_count": len(menu.items),
        "unpriced_count": len(menu.items) - len(menu.priced),
        # Each photo is read by its own call now, so one of them can come back
        # empty while the rest succeed. Reported rather than absorbed: the owner
        # is about to approve this list as their menu, and a page that silently
        # did not make it is a gap they would meet weeks later as an Analyst
        # find about a dish that is not on the board.
        "pages_scanned": menu.pages_scanned,
        "pages_failed": menu.pages_failed,
    })


__all__ = ["MENU_SCAN_EFFORT", "scan_menu"]
