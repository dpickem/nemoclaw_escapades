"""Abstract base class for platform connectors.

A connector is the boundary between a messaging platform (Slack, Discord,
Telegram, a web UI, etc.) and the rest of the NemoClaw agent.  It has
two responsibilities:

1. **Inbound** — listen for platform-specific events, convert them into
   a ``NormalizedRequest``, and hand them to the orchestrator via the
   ``MessageHandler`` callback.
2. **Outbound** — take the orchestrator's platform-neutral
   ``RichResponse`` (text, buttons, confirmations, forms), render it
   into the platform's native format, and send it back to the user.

The orchestrator never imports a platform SDK.  All platform knowledge
lives inside the connector subclass.  Adding a new platform means
creating one new file with one new subclass — nothing else changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from nemoclaw_escapades.models.types import NormalizedRequest, RichResponse

#: Async callback the orchestrator invokes to push real-time status
#: updates back to the connector (e.g. "Searching Jira...").  The
#: connector uses this to update a thinking indicator in the chat UI
#: while the agent loop executes tool calls.
StatusCallback = Callable[[str], Awaitable[None]]

#: Signature of the function a connector calls when it receives an
#: inbound message.  In practice this is ``Orchestrator.handle`` — the
#: connector passes a platform-neutral request and an optional status
#: callback, and awaits a ``RichResponse`` to render back to the user.
MessageHandler = Callable[
    [NormalizedRequest, StatusCallback | None],
    Awaitable[RichResponse],
]


class ConnectorBase(ABC):
    """Translates platform-specific events into normalized requests and sends
    responses back through the originating platform.

    Subclasses implement platform-specific listening, normalization, rendering,
    and reply logic. The orchestrator never touches platform SDKs directly.
    """

    def __init__(self, handler: MessageHandler) -> None:
        self._handler = handler

    @abstractmethod
    async def start(self) -> None:
        """Start listening for platform events."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the connector."""
