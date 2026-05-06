"""Per-request context exposed to tools via :class:`contextvars.ContextVar`.

Some tools — notably ``delegate_task`` — need to address the user's
originating channel/thread when finalisation results arrive
asynchronously much later.  They can't know that at tool-registration
time (channel/thread vary per request), so the orchestrator drops a
:class:`RequestContext` into a ``ContextVar`` at the top of
:meth:`Orchestrator.handle` and the tools read it back when invoked.

``ContextVar`` is the right primitive here because asyncio tasks
spawned inside the request automatically inherit the parent's
context, so a tool that ``await``s another tool sees the same
binding.  A naïve module-level global would race across concurrent
Slack events — two simultaneous requests would clobber each other's
channel id.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    """Request-scoped context tools may read.

    Attributes:
        request_id: Orchestrator request identifier (mirrors
            ``NormalizedRequest.request_id``).
        channel_id: Connector channel identity (Slack channel, etc.).
            ``None`` for headless / CLI invocations.
        thread_ts: Thread / parent timestamp inside *channel_id*.
            ``None`` posts to channel root.
        source: Connector source string (``"slack"``, ``"cli"``, …);
            useful for tools that branch on platform.
    """

    request_id: str
    channel_id: str | None = None
    thread_ts: str | None = None
    source: str = ""


_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "nemoclaw_request_context",
    default=None,
)


def set_request_context(context: RequestContext | None) -> None:
    """Bind *context* to the current asyncio task.

    Call once at the start of :meth:`Orchestrator.handle`; subsequent
    tool invocations in the same task (and in any tasks the tools
    spawn) will read the same binding.
    """
    _REQUEST_CONTEXT.set(context)


def current_request_context() -> RequestContext | None:
    """Return the request context bound to this task, or ``None``."""
    return _REQUEST_CONTEXT.get()
