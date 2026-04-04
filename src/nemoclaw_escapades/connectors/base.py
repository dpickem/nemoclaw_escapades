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

``MessageHandler`` is the callback type the orchestrator exposes::

    MessageHandler = Callable[[NormalizedRequest], Awaitable[RichResponse]]

It is passed to the connector at construction time so the connector can
invoke ``await self._handler(request)`` whenever an event arrives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from nemoclaw_escapades.models.types import NormalizedRequest, RichResponse

MessageHandler = Callable[[NormalizedRequest], Awaitable[RichResponse]]


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
