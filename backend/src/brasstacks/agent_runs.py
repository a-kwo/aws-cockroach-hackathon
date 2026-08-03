"""Guaranteeing that an `agent_run` row gets closed.

Every agent opens its run before it can know how the night will go, then closes
it on each path it anticipates. A path nobody anticipated — a provider that
fails in a new way, a write that raises something the handler does not name —
leaves the row at `running` with no process left to finish it.

That is not a cosmetic leak. The owner's board reads open runs as "the agents
are working", so an abandoned row makes the product claim work is in progress
when nothing is running. Yellow Cow Korean BBQ carried one for a day and a half.

The guard closes the row and re-raises. It never converts a crash into a
success, and it never invents a note: the row says which exception ended the
run, which is all anyone can honestly say about a path the code did not expect.

A process killed outright — a Lambda timeout, an OOM — cannot run this or any
other in-process handler. That case is why the board bounds how long it will
believe a `running` row rather than trusting it forever.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def closing_run(repo: Any, run_id: str) -> Iterator[None]:
    """Close `run_id` as failed if the body raises, then re-raise.

    A body that returns normally is left alone — including one that has already
    closed its own run with a considered note, which is the usual case.
    """
    try:
        yield
    except BaseException as error:
        try:
            repo.finish_run(run_id, status="failed",
                            error=f"{type(error).__name__}: {error}")
        except Exception:
            # The run row is the diagnostic, not the incident. If closing it
            # fails too, the original exception is the one worth raising.
            pass
        raise


__all__ = ["closing_run"]
