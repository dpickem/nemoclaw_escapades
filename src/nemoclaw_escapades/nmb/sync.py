"""Synchronous wrapper around the async NMB client.

For agents that don't use asyncio (e.g. Claude Code integrations
via subprocess).  Runs a background event loop in a daemon thread
and proxies calls via ``run_coroutine_threadsafe``.

**Pump-and-sentinel pattern** — ``listen()`` and ``subscribe()`` need to
bridge an async iterator (running in the background event loop) to a
synchronous ``Iterator`` consumed by the caller's thread.  The naive
approach — wrapping each ``async for`` yield in a separate coroutine —
has two problems:

1. **PEP 479**: ``raise StopIteration`` inside a coroutine is converted
   to ``RuntimeError`` (mandatory since Python 3.7), so using it to
   signal iterator exhaustion crashes instead of stopping cleanly.
2. **Subscription churn**: For ``subscribe()``, each call to the async
   method sends a ``SUBSCRIBE`` frame and creates a queue; when the
   coroutine returns after one message, the ``finally`` block sends
   ``UNSUBSCRIBE`` and tears down the queue.  Every single message
   would trigger a full subscribe/receive/unsubscribe cycle with a
   race window where published messages are silently lost.

The pump-and-sentinel pattern solves both.  A single ``_pump`` coroutine
runs in the background loop for the full lifetime of the iterator.  It
reads from the async iterator and pushes each message into a thread-safe
``queue.Queue``.  When the async iterator exhausts (connection close,
cancellation, error), the pump pushes ``None`` as a sentinel.  The
synchronous caller blocks on ``queue.get()``; receiving ``None`` signals
clean exit.  ``task.cancel()`` in the ``finally`` block ensures the pump
stops if the caller breaks out of the ``for`` loop early.

Example::

    from nemoclaw_escapades.nmb.sync import MessageBus

    bus = MessageBus(sandbox_id="coding-sandbox-1")
    bus.connect()
    bus.send("orchestrator", "task.complete", {"diff": "..."})
    response = bus.request("review-sandbox-1", "review.request",
                           {"diff": "..."}, timeout=300)
    bus.close()
"""

from __future__ import annotations

import asyncio
import queue as queue_mod
import threading
from collections.abc import Iterator
from typing import Any

from nemoclaw_escapades.nmb.client import MessageBus as AsyncMessageBus
from nemoclaw_escapades.nmb.client import NMBConnectionError
from nemoclaw_escapades.nmb.models import NMBMessage

__all__ = ["MessageBus", "NMBConnectionError"]


class MessageBus:
    """Synchronous client for the NemoClaw Message Bus.

    Mirrors the async ``MessageBus`` API but blocks on each call.  A
    background daemon thread runs the event loop.

    Attributes:
        sandbox_id: Identity of this sandbox.
        url: WebSocket URL of the NMB broker.
    """

    def __init__(
        self,
        sandbox_id: str = "",
        url: str = "ws://messages.local:9876",
    ) -> None:
        """Initialise the sync client (does not connect).

        Args:
            sandbox_id: This sandbox's identity.
            url: WebSocket URL of the broker.
        """
        self.sandbox_id: str = sandbox_id
        self.url: str = url
        self._async_bus: AsyncMessageBus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def connect(self) -> None:
        """Open a connection to the broker.

        Starts a background daemon thread running the event loop and
        connects the async client within it.

        Raises:
            NMBConnectionError: If the broker is unreachable.
        """
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        self._async_bus = AsyncMessageBus(sandbox_id=self.sandbox_id, url=self.url)
        self._run(self._async_bus.connect())

    def close(self) -> None:
        """Close the connection and shut down the background loop."""
        if self._async_bus:
            self._run(self._async_bus.close())
            self._async_bus = None
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop.close()
            self._loop = None

    def send(self, to: str, type: str, payload: dict[str, Any]) -> None:
        """Send a fire-and-forget message.

        Args:
            to: Target sandbox ID.
            type: Application-level message type.
            payload: Message payload.
        """
        self._run(self._bus.send(to, type, payload))

    def request(
        self,
        to: str,
        type: str,
        payload: dict[str, Any],
        timeout: float = 300.0,
    ) -> NMBMessage:
        """Send a request and block until a reply arrives.

        Args:
            to: Target sandbox ID.
            type: Application-level message type.
            payload: Request payload.
            timeout: Seconds to wait for a reply.

        Returns:
            The reply ``NMBMessage``.

        Raises:
            TimeoutError: If no reply within *timeout*.
            NMBConnectionError: On connection errors.
        """
        result: NMBMessage = self._run(self._bus.request(to, type, payload, timeout=timeout))
        return result

    def reply(self, original: NMBMessage, type: str, payload: dict[str, Any]) -> None:
        """Reply to a received request.

        Args:
            original: The request message being replied to.
            type: Reply message type.
            payload: Reply payload.
        """
        self._run(self._bus.reply(original, type, payload))

    def publish(self, channel: str, type: str, payload: dict[str, Any]) -> None:
        """Publish a message to a channel.

        Args:
            channel: Target channel name.
            type: Message type.
            payload: Message payload.
        """
        self._run(self._bus.publish(channel, type, payload))

    def listen(self) -> Iterator[NMBMessage]:
        """Yield incoming deliver messages (blocking).

        Reads from the async client's listen queue one message at a
        time.  Stops when the connection is closed.

        Yields:
            ``NMBMessage`` instances delivered to this sandbox.
        """
        queue: queue_mod.Queue[NMBMessage | None] = queue_mod.Queue()

        async def _pump() -> None:
            try:
                async for msg in self._bus.listen():
                    queue.put(msg)
            finally:
                queue.put(None)

        task = asyncio.run_coroutine_threadsafe(_pump(), self._loop_or_raise)
        try:
            while True:
                item = queue.get()
                if item is None:
                    return
                yield item
        finally:
            task.cancel()

    def subscribe(self, channel: str) -> Iterator[NMBMessage]:
        """Subscribe to a channel and yield messages (blocking).

        Manages the async subscription lifecycle in the background loop
        so subscribe/unsubscribe happen exactly once, not per message.

        Args:
            channel: Channel name to subscribe to.

        Yields:
            ``NMBMessage`` instances published to the channel.
        """
        queue: queue_mod.Queue[NMBMessage | None] = queue_mod.Queue()

        async def _pump() -> None:
            try:
                async for msg in self._bus.subscribe(channel):
                    queue.put(msg)
            finally:
                queue.put(None)

        task = asyncio.run_coroutine_threadsafe(_pump(), self._loop_or_raise)
        try:
            while True:
                item = queue.get()
                if item is None:
                    return
                yield item
        finally:
            task.cancel()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def _bus(self) -> AsyncMessageBus:
        """Return the underlying async client.

        Raises:
            NMBConnectionError: If the client is not connected.
        """
        if self._async_bus is None:
            raise NMBConnectionError("Not connected — call connect() first")
        return self._async_bus

    @property
    def _loop_or_raise(self) -> asyncio.AbstractEventLoop:
        """Return the background event loop.

        Raises:
            NMBConnectionError: If the background loop is not running.
        """
        if self._loop is None:
            raise NMBConnectionError("Not connected — call connect() first")
        return self._loop

    def _run(self, coro: Any) -> Any:
        """Submit a coroutine to the background loop and block until it completes.

        Args:
            coro: The awaitable to execute on the background event loop.

        Returns:
            The coroutine's return value.

        Raises:
            NMBConnectionError: If the background loop is not running.
        """
        if self._loop is None:
            raise NMBConnectionError("Not connected — call connect() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()
