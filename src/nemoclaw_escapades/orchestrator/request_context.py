"""Per-request context exposed to tools via :class:`contextvars.ContextVar`.

Some tools need the user's originating channel/thread when asynchronous results
arrive later.  The orchestrator binds a :class:`RequestContext` at the top of
``Orchestrator.handle`` and tools read it when invoked.

``ContextVar`` keeps concurrent Slack/CLI requests isolated while still flowing
through awaits and child tasks spawned inside the request.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    """Request-scoped context tools may read.

    Bound once per orchestrator request and read by tools that need to route
    later results back to the originating connector thread.
    """

    # Orchestrator request identifier.
    request_id: str
    # Connector channel id; None for headless or CLI invocations.
    channel_id: str | None = None
    # Connector thread timestamp or parent id inside channel_id.
    thread_ts: str | None = None
    # Connector source string, e.g. "slack" or "cli".
    source: str = ""


_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "nemoclaw_request_context",
    default=None,
)


def set_request_context(context: RequestContext | None) -> None:
    """Bind *context* to the current asyncio task.

    Call once at the start of ``Orchestrator.handle``.  Pass ``None`` to clear
    the binding for headless or teardown paths.
    """
    _REQUEST_CONTEXT.set(context)


def current_request_context() -> RequestContext | None:
    """Return the request context bound to this task, or ``None``."""
    return _REQUEST_CONTEXT.get()
