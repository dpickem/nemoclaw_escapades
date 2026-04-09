"""Async client library for the NemoClaw Message Bus.

Agents import ``MessageBus`` and use a Hermes-like API to communicate
with other sandboxes through the NMB broker.  The client connects to
``messages.local:9876`` (configurable), sends messages, and receives
deliveries via a background receive task.

Example::

    bus = MessageBus(sandbox_id="orchestrator")
    await bus.connect()
    await bus.send("coding-sandbox-1", "task.assign", {"prompt": "..."})
    response = await bus.request("review-sandbox-1", "review.request",
                                 {"diff": "..."}, timeout=300)
    await bus.close()
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from nemoclaw_escapades.nmb.models import NMBMessage, Op, parse_frame, serialize_frame

logger = logging.getLogger("nmb.client")

DEFAULT_URL = "ws://messages.local:9876"


class NMBConnectionError(Exception):
    """Raised when the broker is unreachable or the connection is lost."""


class MessageBus:
    """Async client for the NemoClaw Message Bus.

    Attributes:
        url: WebSocket URL of the NMB broker.
        sandbox_id: Identity of this sandbox (sent via ``X-Sandbox-ID``).
    """

    def __init__(
        self,
        sandbox_id: str = "",
        url: str = DEFAULT_URL,
    ) -> None:
        """Initialise the client (does not connect).

        Args:
            sandbox_id: This sandbox's identity.  If empty, reads from
                the ``NMB_SANDBOX_ID`` environment variable.
            url: WebSocket URL of the broker.
        """
        import os

        self.url: str = url
        self.sandbox_id: str = sandbox_id or os.environ.get("NMB_SANDBOX_ID", "unknown")
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None

        # request_id -> Future for request-reply
        self._pending_futures: dict[str, asyncio.Future[NMBMessage]] = {}
        # General delivery queue for listen()
        self._listen_queue: asyncio.Queue[NMBMessage] = asyncio.Queue()
        # channel -> queue for subscribe()
        self._channel_queues: dict[str, asyncio.Queue[NMBMessage]] = {}

        self._closed = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open a WebSocket connection to the broker.

        Sends ``X-Sandbox-ID`` in the upgrade request and starts a
        background task to receive and dispatch incoming frames.

        Raises:
            NMBConnectionError: If the broker is unreachable.
        """
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers={"X-Sandbox-ID": self.sandbox_id},
            )
        except (OSError, websockets.WebSocketException) as exc:
            raise NMBConnectionError(f"Cannot connect to broker at {self.url}: {exc}") from exc

        self._closed = False
        self._recv_task = asyncio.create_task(self._receive_loop())
        logger.info("Connected to NMB broker at %s as %s", self.url, self.sandbox_id)

    async def close(self) -> None:
        """Gracefully close the connection.

        Cancels the background receive task, closes the WebSocket, and
        wakes any pending request futures with ``NMBConnectionError``.
        """
        self._closed = True
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._fail_pending_futures()
        logger.info("Disconnected from NMB broker")

    def _fail_pending_futures(self) -> None:
        """Wake all pending request futures with ``NMBConnectionError``.

        Called both from ``close()`` and from the ``_receive_loop``
        ``finally`` block so futures never hang after a connection loss.
        """
        for fut in self._pending_futures.values():
            if not fut.done():
                fut.set_exception(NMBConnectionError("Connection closed"))
        self._pending_futures.clear()

    @property
    def _conn(self) -> ClientConnection:
        """Return the active WebSocket connection.

        Raises:
            NMBConnectionError: If the client is not connected.
        """
        if self._ws is None:
            raise NMBConnectionError("Not connected — call connect() first")
        return self._ws

    # ------------------------------------------------------------------
    # Background receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Background task: read frames from the broker and dispatch them.

        Deliver frames are routed to pending futures (request-reply),
        channel queues (subscriptions), or the general listen queue.
        Error and timeout frames are routed to their matching futures.

        On unexpected connection loss, all pending request futures are
        woken with ``NMBConnectionError`` so callers don't hang.
        """
        try:
            async for raw in self._conn:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    msg = parse_frame(raw)
                except Exception:
                    logger.warning("Ignoring unparseable frame", exc_info=True)
                    continue

                if msg.op == Op.DELIVER:
                    self._dispatch_deliver(msg)
                elif msg.op == Op.ACK:
                    pass  # ACKs are currently fire-and-forget
                elif msg.op == Op.ERROR:
                    self._dispatch_error(msg)
                elif msg.op == Op.TIMEOUT:
                    self._dispatch_timeout(msg)
        except websockets.ConnectionClosed:
            if not self._closed:
                logger.warning("Connection to broker lost")
        except asyncio.CancelledError:
            pass
        finally:
            self._fail_pending_futures()

    def _dispatch_deliver(self, msg: NMBMessage) -> None:
        """Route a delivered message to the correct future or queue.

        Priority: pending request future > channel subscription queue >
        general listen queue.

        Args:
            msg: The ``deliver`` frame received from the broker.
        """
        # Check if this is a reply to a pending request
        if msg.reply_to and msg.reply_to in self._pending_futures:
            fut = self._pending_futures.pop(msg.reply_to)
            if not fut.done():
                fut.set_result(msg)
            return

        # Check channel subscriptions
        if msg.channel and msg.channel in self._channel_queues:
            self._channel_queues[msg.channel].put_nowait(msg)
            return

        # General listen queue
        self._listen_queue.put_nowait(msg)

    def _dispatch_error(self, msg: NMBMessage) -> None:
        """Route a broker error to the matching pending request future.

        Args:
            msg: The ``error`` frame received from the broker.
        """
        if msg.id in self._pending_futures:
            fut = self._pending_futures.pop(msg.id)
            if not fut.done():
                fut.set_exception(NMBConnectionError(f"Broker error {msg.code}: {msg.message}"))

    def _dispatch_timeout(self, msg: NMBMessage) -> None:
        """Route a broker timeout to the matching pending request future.

        Sets a ``TimeoutError`` on the future so the caller's ``await``
        raises.

        Args:
            msg: The ``timeout`` frame received from the broker.
        """
        if msg.id in self._pending_futures:
            fut = self._pending_futures.pop(msg.id)
            if not fut.done():
                fut.set_exception(TimeoutError(f"Request {msg.id} timed out: {msg.message}"))

    # ------------------------------------------------------------------
    # Public API: send
    # ------------------------------------------------------------------

    async def send(self, to: str, type: str, payload: dict[str, Any]) -> None:
        """Send a fire-and-forget message to a target sandbox.

        Args:
            to: Target sandbox ID.
            type: Application-level message type (e.g. ``task.assign``).
            payload: Arbitrary JSON-serialisable payload.

        Raises:
            NMBConnectionError: If the connection is not open.
        """
        msg = NMBMessage(op=Op.SEND, to=to, type=type, payload=payload)
        await self._conn.send(serialize_frame(msg))

    # ------------------------------------------------------------------
    # Public API: request / reply
    # ------------------------------------------------------------------

    async def request(
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
            TimeoutError: If no reply arrives within *timeout*.
            NMBConnectionError: On connection errors.
        """
        msg = NMBMessage(op=Op.REQUEST, to=to, type=type, payload=payload, timeout=timeout)
        fut: asyncio.Future[NMBMessage] = asyncio.get_running_loop().create_future()
        self._pending_futures[msg.id] = fut
        await self._conn.send(serialize_frame(msg))
        try:
            return await asyncio.wait_for(fut, timeout=timeout + 5.0)
        except TimeoutError:
            self._pending_futures.pop(msg.id, None)
            raise

    async def reply(self, original: NMBMessage, type: str, payload: dict[str, Any]) -> None:
        """Reply to a received request message.

        Args:
            original: The request message being replied to.
            type: Reply message type (e.g. ``review.feedback``).
            payload: Reply payload.
        """
        msg = NMBMessage(op=Op.REPLY, reply_to=original.id, type=type, payload=payload)
        await self._conn.send(serialize_frame(msg))

    # ------------------------------------------------------------------
    # Public API: pub/sub
    # ------------------------------------------------------------------

    async def subscribe(self, channel: str) -> AsyncIterator[NMBMessage]:
        """Subscribe to a pub/sub channel and yield delivered messages.

        Args:
            channel: Channel name to subscribe to.

        Yields:
            ``NMBMessage`` instances published to the channel.
        """
        sub_msg = NMBMessage(op=Op.SUBSCRIBE, channel=channel)
        await self._conn.send(serialize_frame(sub_msg))
        queue: asyncio.Queue[NMBMessage] = asyncio.Queue()
        self._channel_queues[channel] = queue
        try:
            while not self._closed:
                msg = await queue.get()
                yield msg
        finally:
            self._channel_queues.pop(channel, None)
            unsub = NMBMessage(op=Op.UNSUBSCRIBE, channel=channel)
            try:
                await self._conn.send(serialize_frame(unsub))
            except (NMBConnectionError, websockets.ConnectionClosed):
                pass

    async def publish(self, channel: str, type: str, payload: dict[str, Any]) -> None:
        """Publish a message to a channel.

        Args:
            channel: Target channel name.
            type: Application-level message type.
            payload: Message payload.
        """
        msg = NMBMessage(op=Op.PUBLISH, channel=channel, type=type, payload=payload)
        await self._conn.send(serialize_frame(msg))

    # ------------------------------------------------------------------
    # Public API: stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        to: str,
        type: str,
        chunks: AsyncIterator[dict[str, Any]],
    ) -> None:
        """Stream ordered chunks to a target sandbox.

        Args:
            to: Target sandbox ID.
            type: Application-level message type for the stream.
            chunks: Async iterator yielding payload dicts per chunk.
        """
        stream_id = uuid.uuid4().hex
        seq = 0
        async for chunk_payload in chunks:
            msg = NMBMessage(
                op=Op.STREAM,
                to=to,
                type=type,
                stream_id=stream_id,
                seq=seq,
                done=False,
                payload=chunk_payload,
            )
            await self._conn.send(serialize_frame(msg))
            seq += 1

        # Final empty chunk to signal completion
        done_msg = NMBMessage(
            op=Op.STREAM,
            to=to,
            type=type,
            stream_id=stream_id,
            seq=seq,
            done=True,
            payload={},
        )
        await self._conn.send(serialize_frame(done_msg))

    # ------------------------------------------------------------------
    # Public API: listen
    # ------------------------------------------------------------------

    async def listen(self) -> AsyncIterator[NMBMessage]:
        """Yield all incoming deliver messages not matched by a subscription or pending request.

        Use this in sub-agent mode to process incoming tasks.

        Yields:
            ``NMBMessage`` instances delivered to this sandbox.
        """
        while not self._closed:
            try:
                msg = await asyncio.wait_for(self._listen_queue.get(), timeout=1.0)
                yield msg
            except TimeoutError:
                continue
