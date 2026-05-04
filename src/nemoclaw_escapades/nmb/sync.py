"""Synchronous wrapper around the async NMB client.

For agents that don't use asyncio (e.g. Claude Code integrations
via subprocess, shell scripts, or the ``nmb-client`` CLI).  A
background daemon thread runs a dedicated event loop; public
methods submit coroutines to that loop via
``run_coroutine_threadsafe`` and block the calling thread until
the result is ready.

**Pump-and-sentinel pattern** — ``listen()`` and ``subscribe()``
need to bridge an async iterator (running in the background event
loop) to a synchronous ``Iterator`` consumed by the caller's
thread.  The naive approach — wrapping each ``async for`` yield in
a separate coroutine — has two problems:

1. **PEP 479**: ``raise StopIteration`` inside a coroutine is
   converted to ``RuntimeError`` (mandatory since Python 3.7), so
   using it to signal iterator exhaustion crashes instead of
   stopping cleanly.
2. **Subscription churn**: For ``subscribe()``, each call to the
   async method sends a ``SUBSCRIBE`` frame and creates a queue;
   when the coroutine returns after one message, the ``finally``
   block sends ``UNSUBSCRIBE`` and tears down the queue.  Every
   single message would trigger a full
   subscribe/receive/unsubscribe cycle with a race window where
   published messages are silently lost.

The pump-and-sentinel pattern solves both.  A single ``_pump``
coroutine runs in the background loop for the full lifetime of the
iterator.  It reads from the async iterator and pushes each message
into a thread-safe ``queue.Queue``.  When the async iterator
exhausts (connection close, cancellation, error), the pump pushes
``None`` as a sentinel.  The synchronous caller blocks on
``queue.get()``; receiving ``None`` signals clean exit.
``task.cancel()`` in the ``finally`` block ensures the pump stops
if the caller breaks out of the ``for`` loop early.

Example::

    from nemoclaw_escapades.nmb.sync import MessageBus

    bus = MessageBus(sandbox_id="coding-sandbox-1")
    bus.connect()
    bus.send(target_sandbox_id, "task.complete", {"diff": "..."})
    response = bus.request(target_sandbox_id, "review.request",
                           {"diff": "..."}, timeout=300)
    bus.close()
"""

from __future__ import annotations

import asyncio
import queue as queue_mod
import threading
from collections.abc import Iterator
from typing import Any

from nemoclaw_escapades.config import DEFAULT_NMB_URL
from nemoclaw_escapades.nmb.client import MessageBus as AsyncMessageBus
from nemoclaw_escapades.nmb.client import NMBConnectionError
from nemoclaw_escapades.nmb.models import NMBMessage

__all__ = ["MessageBus", "NMBConnectionError"]


class MessageBus:
    """Synchronous (blocking) client for the NemoClaw Message Bus.

    Mirrors the async ``MessageBus`` API but blocks on each call.
    Internally creates a daemon thread running an ``asyncio`` event
    loop and an ``AsyncMessageBus`` connected within it.

    By default, the ``sandbox_id`` passed to the constructor is a
    human-readable name (e.g. ``"orchestrator"``), and the underlying
    async client appends a random 8-hex-char suffix to make it globally
    unique per launch.  Set ``append_random_suffix=False`` when the id
    is already an exact routing identity — see
    :class:`~nemoclaw_escapades.nmb.client.MessageBus`.

    Public attributes:
        sandbox_id: Human-readable name passed at construction.  The
            globally unique ID (with suffix) is available on the
            underlying async client after ``connect()``.
        broker_url: WebSocket URL of the NMB broker.

    Private attributes:
        _async_bus: The underlying ``AsyncMessageBus``, created in
            ``connect()``.  ``None`` before connect / after close.
        _loop: The background ``asyncio`` event loop.  ``None``
            before connect / after close.
        _thread: The daemon ``Thread`` running ``_loop``.  ``None``
            before connect / after close.
    """

    def __init__(
        self,
        sandbox_id: str,
        broker_url: str = DEFAULT_NMB_URL,
        *,
        append_random_suffix: bool = True,
    ) -> None:
        """Initialise the sync client (does not connect).

        Args:
            sandbox_id: Human-readable name for this sandbox
                (e.g. ``"orchestrator"``).  Passed to the async client
                which appends a random suffix for global uniqueness by
                default.
            broker_url: WebSocket URL of the NMB broker.
            append_random_suffix: Whether the async client should add a
                random suffix to ``sandbox_id``.
        """
        self.sandbox_id: str = sandbox_id
        self.broker_url: str = broker_url
        self.append_random_suffix = append_random_suffix
        self._async_bus: AsyncMessageBus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open a connection to the broker.

        Creates a new event loop, starts it in a daemon thread, then
        creates and connects the async ``MessageBus`` within that loop.

        Raises:
            NMBConnectionError: If the broker is unreachable.

        Side effects:
            Populates ``_loop``, ``_thread``, and ``_async_bus``.
        """
        # Create a dedicated event loop for this client's I/O.
        self._loop = asyncio.new_event_loop()

        # Run the loop in a daemon thread so it doesn't block the
        # caller and is automatically cleaned up on process exit.
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        # Create the async client and connect it inside the background
        # loop.  _run() blocks until the coroutine completes.
        self._async_bus = AsyncMessageBus(
            sandbox_id=self.sandbox_id,
            broker_url=self.broker_url,
            append_random_suffix=self.append_random_suffix,
        )
        self._run(self._async_bus.connect())

    def close(self) -> None:
        """Close the connection and shut down the background loop.

        Safe to call even if ``connect()`` was never called.

        Side effects:
            Sets ``_async_bus``, ``_loop``, and ``_thread`` to
            ``None``.
        """
        # Close the async client (cancels receive task, closes WS).
        if self._async_bus:
            self._run(self._async_bus.close())
            self._async_bus = None

        # Stop the background event loop and join the thread.
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop.close()
            self._loop = None

    # ------------------------------------------------------------------
    # Public API (blocking wrappers around the async client)
    # ------------------------------------------------------------------

    def send(self, to: str, type: str, payload: dict[str, Any]) -> None:
        """Send a point-to-point message and wait for the broker's ACK.

        Args:
            to: Target ``sandbox_id`` (globally unique per launch).
            type: Application-level message type.
            payload: Message payload.

        Raises:
            NMBConnectionError: If the target is offline or the
                connection is not open.
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
            to: Target ``sandbox_id`` (globally unique per launch).
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

        Raises:
            NMBConnectionError: If the connection is not open.
        """
        self._run(self._bus.reply(original, type, payload))

    def publish(self, channel: str, type: str, payload: dict[str, Any]) -> None:
        """Publish a message to a channel and wait for the broker's ACK.

        Args:
            channel: Target channel name.
            type: Message type.
            payload: Message payload.

        Raises:
            NMBConnectionError: On broker error or connection loss.
        """
        self._run(self._bus.publish(channel, type, payload))

    # ------------------------------------------------------------------
    # Iterators (pump-and-sentinel pattern)
    # ------------------------------------------------------------------

    def listen(self) -> Iterator[NMBMessage]:
        """Yield incoming deliver messages (blocking).

        Uses the pump-and-sentinel pattern (see module docstring) to
        bridge the async ``listen()`` generator to a synchronous
        ``Iterator``.  Stops when the connection is closed or the
        caller breaks out.

        Yields:
            ``NMBMessage`` instances delivered to this sandbox.
        """
        # Thread-safe bridge: _pump runs in the background loop and
        # pushes messages here; the caller's thread blocks on get().
        q: queue_mod.Queue[NMBMessage | None] = queue_mod.Queue()

        async def _pump() -> None:
            """Drain the async listen() iterator into the thread-safe queue."""
            try:
                async for msg in self._bus.listen():
                    q.put(msg)
            finally:
                # Sentinel: tells the caller's thread to stop iterating.
                q.put(None)

        # Submit the pump to the background loop.
        task = asyncio.run_coroutine_threadsafe(_pump(), self._loop_or_raise)
        try:
            while True:
                item = q.get()
                if item is None:
                    return  # Async side exhausted — clean exit.
                yield item
        finally:
            # If the caller breaks out early, cancel the pump so the
            # async listen() generator runs its cleanup (the finally
            # block is a no-op if the task already finished).
            task.cancel()

    def subscribe(self, channel: str) -> Iterator[NMBMessage]:
        """Subscribe to a channel and yield messages (blocking).

        Uses the pump-and-sentinel pattern (see module docstring).
        The SUBSCRIBE frame is sent once when the pump starts; the
        UNSUBSCRIBE frame is sent when the pump's ``finally`` block
        fires (on caller break, connection close, or error).

        Args:
            channel: Channel name to subscribe to.

        Yields:
            ``NMBMessage`` instances published to the channel.
        """
        q: queue_mod.Queue[NMBMessage | None] = queue_mod.Queue()

        async def _pump() -> None:
            """Drain the async subscribe() iterator into the thread-safe queue."""
            try:
                async for msg in self._bus.subscribe(channel):
                    q.put(msg)
            finally:
                q.put(None)

        task = asyncio.run_coroutine_threadsafe(_pump(), self._loop_or_raise)
        try:
            while True:
                item = q.get()
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

        Returns:
            The connected ``AsyncMessageBus``.

        Raises:
            NMBConnectionError: If ``connect()`` has not been called.
        """
        if self._async_bus is None:
            raise NMBConnectionError("Not connected — call connect() first")
        return self._async_bus

    @property
    def _loop_or_raise(self) -> asyncio.AbstractEventLoop:
        """Return the background event loop.

        Returns:
            The running ``asyncio.AbstractEventLoop``.

        Raises:
            NMBConnectionError: If the background loop is not running.
        """
        if self._loop is None:
            raise NMBConnectionError("Not connected — call connect() first")
        return self._loop

    def _run(self, coro: Any) -> Any:
        """Submit a coroutine to the background loop and block until it completes.

        This is the core bridge between the caller's synchronous thread
        and the async event loop running in the daemon thread.
        ``run_coroutine_threadsafe`` schedules the coroutine on the
        loop; ``.result()`` blocks the caller until it finishes.

        Args:
            coro: The awaitable to execute on the background event loop.

        Returns:
            The coroutine's return value.

        Raises:
            NMBConnectionError: If the background loop is not running.
            Exception: Any exception raised by the coroutine is
                re-raised in the caller's thread.
        """
        if self._loop is None:
            raise NMBConnectionError("Not connected — call connect() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()
