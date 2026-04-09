"""Async client library for the NemoClaw Message Bus.

``MessageBus`` is the **transport layer** — it handles the WebSocket
connection to the broker, serializes/deserializes frames, tracks
ACKs, and buffers incoming messages into queues.  It does **not**
interpret message payloads or decide what to do with them.

The consumer side is intentionally decoupled:

- **``listen()``** yields unmatched deliver frames from a bounded
  queue.  The caller (typically a sandbox's main loop or an
  orchestrator agent) iterates the async generator and dispatches
  each message to application logic::

      async for msg in bus.listen():
          if msg.type == "task.assign":
              await handle_task(msg.payload)

- **``subscribe(channel)``** yields channel-specific messages.
  Multiple concurrent subscriptions each get their own queue.

- **``request()``** blocks until the specific reply arrives (or
  times out) — the caller awaits the returned ``NMBMessage``
  directly.

In other words, ``MessageBus`` is the pipe; the agent code on top
reads from it and acts.  This separation keeps the transport
reusable across the orchestrator, coding sandboxes, and review
sandboxes without baking in any agent-specific logic.

Quick-start example::

    bus = MessageBus(sandbox_id="orchestrator")
    await bus.connect()
    await bus.send(target_sandbox_id, "task.assign", {"prompt": "..."})
    response = await bus.request(target_sandbox_id, "review.request",
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
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from websockets.asyncio.client import ClientConnection

from nemoclaw_escapades.config import (
    DEFAULT_NMB_ACK_TIMEOUT,
    DEFAULT_NMB_CHANNEL_QUEUE_SIZE,
    DEFAULT_NMB_CONNECT_MAX_RETRIES,
    DEFAULT_NMB_CONNECT_WAIT_MAX,
    DEFAULT_NMB_CONNECT_WAIT_MIN,
    DEFAULT_NMB_DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_NMB_LISTEN_QUEUE_SIZE,
    DEFAULT_NMB_URL,
)
from nemoclaw_escapades.nmb.models import NMBMessage, Op

logger = logging.getLogger("nmb.client")

# Extra seconds the client waits beyond the broker's request timeout so
# the broker's own TIMEOUT frame has time to arrive before the client
# gives up locally.
_REQUEST_TIMEOUT_GRACE_S = 5.0

# Seconds between listen() polls — controls how quickly listen() notices
# that _closed has been set when no messages are arriving.
_LISTEN_POLL_INTERVAL_S = 1.0


class NMBConnectionError(Exception):
    """Raised when the broker is unreachable or the connection is lost.

    This covers both transport-level failures (socket errors, TLS
    problems) and broker-level rejections (``TARGET_OFFLINE``,
    ``RATE_LIMITED``, etc.) that are surfaced through ACK tracking.
    """


class MessageBus:
    """Async client for the NemoClaw Message Bus.

    On construction, a globally unique ``sandbox_id`` is generated
    from the caller-supplied name + a random suffix (e.g.
    ``"coding-sandbox-1-a3f7b2c8"``).  This single ID is used for
    all routing, channel subscriptions, and audit.

    Public attributes:
        broker_url: WebSocket URL of the NMB broker.
        sandbox_id: Globally unique per-launch identifier (sent via
            ``X-Sandbox-ID``).  Used by the broker as the primary
            key for routing, channel subscriptions, and audit.

    Private attributes:
        _ws: The live ``ClientConnection``, or ``None`` before
            ``connect()`` / after ``close()``.
        _recv_task: Background ``asyncio.Task`` running
            ``_receive_loop``.  Spawned by ``connect()``, cancelled
            by ``close()``.
        _pending_futures: ``request_id`` → ``Future[NMBMessage]``
            for in-flight request-reply correlations.  Resolved by
            ``_dispatch_deliver`` (reply arrived) or failed by
            ``_dispatch_timeout`` / ``_dispatch_error``.
        _pending_acks: ``msg_id`` → ``Future[None]`` for ACK
            tracking on ``send`` / ``publish`` / ``subscribe``.
            Resolved by ``_dispatch_ack``, failed by
            ``_dispatch_error``.
        _listen_queue: Bounded ``asyncio.Queue`` of unmatched
            ``deliver`` messages consumed by ``listen()``.  Uses
            drop-oldest when full (``DEFAULT_NMB_LISTEN_QUEUE_SIZE``).
        _channel_queues: ``channel`` → list of per-subscriber
            ``asyncio.Queue`` instances.  Supports multiple
            concurrent subscriptions to the same channel within a
            single client.
        _closed: Set to ``True`` by ``close()`` to signal the
            receive loop and iterator methods to stop.
    """

    def __init__(
        self,
        sandbox_id: str,
        broker_url: str = DEFAULT_NMB_URL,
    ) -> None:
        """Initialise the client (does not connect).

        Args:
            sandbox_id: Human-readable name for this sandbox
                (e.g. ``"orchestrator"``).  A random 8-hex-char suffix
                is appended automatically to make it globally unique.
            broker_url: WebSocket URL of the NMB broker.
        """
        self.broker_url: str = broker_url

        # Globally unique per launch — the caller-supplied name is
        # used as a human-readable prefix.
        self.sandbox_id: str = f"{sandbox_id}-{uuid.uuid4().hex[:8]}"
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None

        # request_id → Future for request-reply correlation
        self._pending_futures: dict[str, asyncio.Future[NMBMessage]] = {}
        # msg_id → Future[None] for ACK tracking on send/publish/subscribe
        self._pending_acks: dict[str, asyncio.Future[None]] = {}
        # Bounded delivery queue for listen(); drop-oldest when full
        self._listen_queue: asyncio.Queue[NMBMessage] = asyncio.Queue(
            maxsize=DEFAULT_NMB_LISTEN_QUEUE_SIZE,
        )
        # channel → list of subscriber queues (supports concurrent subscribers)
        self._channel_queues: dict[str, list[asyncio.Queue[NMBMessage]]] = {}

        self._closed = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open a WebSocket connection to the broker.

        Sends ``X-Sandbox-ID`` in the upgrade request and starts a
        background task to receive and dispatch incoming frames.

        Raises:
            NMBConnectionError: If the broker is unreachable or the
                WebSocket handshake fails.

        Side effects:
            Spawns ``_receive_loop`` as a background ``asyncio.Task``.
        """
        try:
            self._ws = await websockets.connect(
                self.broker_url,
                additional_headers={"X-Sandbox-ID": self.sandbox_id},
            )
        except (OSError, websockets.WebSocketException) as exc:
            raise NMBConnectionError(
                f"Cannot connect to broker at {self.broker_url}: {exc}"
            ) from exc

        self._closed = False

        # Spawn the background frame reader — it runs for the lifetime
        # of this connection and feeds all the _dispatch_* handlers.
        self._recv_task = asyncio.create_task(self._receive_loop())
        logger.info("Connected to NMB broker at %s as %s", self.broker_url, self.sandbox_id)

    async def connect_with_retry(
        self,
        max_retries: int = DEFAULT_NMB_CONNECT_MAX_RETRIES,
        wait_min: float = DEFAULT_NMB_CONNECT_WAIT_MIN,
        wait_max: float = DEFAULT_NMB_CONNECT_WAIT_MAX,
    ) -> None:
        """Connect to the broker with exponential-backoff retry.

        Uses `tenacity <https://tenacity.readthedocs.io/>`_ under the
        hood.  Each failed attempt is logged at WARNING level before
        sleeping.

        Args:
            max_retries: Maximum number of connection attempts.
            wait_min: Minimum backoff delay in seconds (exponential
                floor).
            wait_max: Maximum backoff delay in seconds (exponential
                ceiling).

        Raises:
            NMBConnectionError: If all *max_retries* attempts fail.
        """
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(min=wait_min, max=wait_max),
            retry=retry_if_exception_type(NMBConnectionError),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                await self.connect()

    async def close(self) -> None:
        """Gracefully close the connection.

        Cancels the background receive task, closes the WebSocket, and
        wakes any pending request/ACK futures with ``NMBConnectionError``
        so callers don't hang.

        Side effects:
            Sets ``_closed`` to ``True`` and clears all internal state
            (pending futures, queues remain intact but no longer fed).
        """
        # Signal the receive loop and iterators (listen/subscribe) to stop.
        self._closed = True

        # Cancel the background reader task.
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        # Close the WebSocket transport.
        if self._ws:
            await self._ws.close()
            self._ws = None

        # Wake any callers blocked on request()/send()/publish() so
        # they don't hang forever.
        self._fail_pending_futures()
        logger.info("Disconnected from NMB broker")

    def _fail_pending_futures(self) -> None:
        """Wake all pending request and ACK futures with ``NMBConnectionError``.

        Called both from ``close()`` and from the ``_receive_loop``
        ``finally`` block so futures never hang after a connection loss.

        Side effects:
            Clears ``_pending_futures`` and ``_pending_acks``.
        """
        # Fail request-reply futures (callers awaiting a reply).
        for fut in self._pending_futures.values():
            if not fut.done():
                fut.set_exception(NMBConnectionError("Connection closed"))
        self._pending_futures.clear()

        # Fail ACK futures (callers awaiting send/publish/subscribe confirmation).
        for ack_fut in self._pending_acks.values():
            if not ack_fut.done():
                ack_fut.set_exception(NMBConnectionError("Connection closed"))
        self._pending_acks.clear()

    @property
    def _conn(self) -> ClientConnection:
        """Return the active WebSocket connection.

        Returns:
            The live ``ClientConnection``.

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
        ACK, error, and timeout frames are routed to their matching
        futures so that ``send``/``publish``/``subscribe`` can detect
        failures instead of silently dropping them.

        On unexpected connection loss, all pending request and ACK
        futures are woken with ``NMBConnectionError`` so callers don't
        hang.

        Side effects:
            Mutates ``_pending_futures``, ``_pending_acks``,
            ``_listen_queue``, and ``_channel_queues`` via the
            ``_dispatch_*`` helpers.  Calls ``_fail_pending_futures``
            in its ``finally`` block.
        """
        try:
            # websockets yields one frame per iteration; blocks until
            # a frame arrives or the connection closes.
            async for raw in self._conn:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    msg = NMBMessage.from_json(raw)
                except Exception:
                    # Malformed frame from the broker — log and skip.
                    logger.warning("Ignoring unparseable frame", exc_info=True)
                    continue

                self._dispatch(msg)
        except websockets.ConnectionClosed:
            # Expected when close() is called; unexpected otherwise.
            if not self._closed:
                logger.warning("Connection to broker lost")
        except asyncio.CancelledError:
            pass  # close() cancelled us — normal shutdown.
        finally:
            # Ensure no caller hangs on a future that will never resolve.
            self._fail_pending_futures()

    def _dispatch(self, msg: NMBMessage) -> None:
        """Route a parsed broker frame to the appropriate handler.

        Uses a dict-based dispatch table keyed by ``Op``, mirroring
        the broker's handler-map pattern.  Unknown ops are silently
        ignored — the broker should never send them, but defensive
        code is cheap.

        Args:
            msg: The parsed ``NMBMessage`` received from the broker.
        """
        handler_map = {
            Op.DELIVER: self._dispatch_deliver,
            Op.ACK: self._dispatch_ack,
            Op.ERROR: self._dispatch_error,
            Op.TIMEOUT: self._dispatch_timeout,
        }
        handler = handler_map.get(msg.op)
        if handler is not None:
            handler(msg)

    def _dispatch_deliver(self, msg: NMBMessage) -> None:
        """Route a delivered message to the correct future or queue.

        Priority: pending request future > channel subscription queues >
        general listen queue.  All delivery queues are bounded; when
        full the oldest item is evicted (drop-oldest policy).

        Args:
            msg: The ``deliver`` frame received from the broker.
        """
        # 1. Reply to a pending request? Resolve the caller's future.
        if msg.reply_to and msg.reply_to in self._pending_futures:
            fut = self._pending_futures.pop(msg.reply_to)
            if not fut.done():
                fut.set_result(msg)
            return

        # 2. Belongs to a subscribed channel? Fan out to all subscriber queues.
        if msg.channel and msg.channel in self._channel_queues:
            for q in self._channel_queues[msg.channel]:
                self._put_or_drop_oldest(q, msg)
            return

        # 3. Unmatched — goes to the general listen queue for listen().
        self._put_or_drop_oldest(self._listen_queue, msg)

    def _dispatch_ack(self, msg: NMBMessage) -> None:
        """Resolve the ACK future for a send/publish/subscribe operation.

        Args:
            msg: The ``ack`` frame received from the broker.
        """
        fut = self._pending_acks.pop(msg.id, None)
        if fut and not fut.done():
            fut.set_result(None)

    def _dispatch_error(self, msg: NMBMessage) -> None:
        """Route a broker error to the matching pending request or ACK future.

        Checks request-reply futures first, then ACK futures, so that
        errors like ``TARGET_OFFLINE`` are propagated to the caller for
        all operation types (send, publish, subscribe, request).

        Args:
            msg: The ``error`` frame received from the broker.
        """
        exc = NMBConnectionError(f"Broker error {msg.code}: {msg.message}")
        # Check request-reply futures first (request() callers), then
        # ACK futures (send/publish/subscribe callers).
        if msg.id in self._pending_futures:
            fut = self._pending_futures.pop(msg.id)
            if not fut.done():
                fut.set_exception(exc)
        elif msg.id in self._pending_acks:
            ack_fut = self._pending_acks.pop(msg.id)
            if not ack_fut.done():
                ack_fut.set_exception(exc)

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

    @staticmethod
    def _put_or_drop_oldest(queue: asyncio.Queue[NMBMessage], msg: NMBMessage) -> None:
        """Add a message to a bounded queue, evicting the oldest if full.

        Implements the design-specified "1 000 then drop oldest"
        buffering policy for delivery queues.

        Args:
            queue: The target ``asyncio.Queue`` (must have a maxsize).
            msg: The message to enqueue.
        """
        # Evict the oldest item when the queue is at capacity so the
        # newest message is never lost (design policy: "1 000 then
        # drop oldest").
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(msg)

    # ------------------------------------------------------------------
    # Internal: ACK-awaiting send helper
    # ------------------------------------------------------------------

    async def _send_and_await_ack(
        self, msg: NMBMessage, timeout: float = DEFAULT_NMB_ACK_TIMEOUT
    ) -> None:
        """Send a frame and wait for the broker's ACK or ERROR response.

        Registers an ACK future *before* sending so that a fast broker
        response is never missed by the receive loop.

        Args:
            msg: The message to send.
            timeout: Seconds to wait for an ACK before raising.

        Raises:
            NMBConnectionError: On broker error, ACK timeout, or
                connection loss.
        """
        # Register the ACK future BEFORE sending — the receive loop
        # may resolve it before `send()` returns to the event loop.
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_acks[msg.id] = fut
        await self._conn.send(msg.to_json())
        try:
            # _dispatch_ack resolves the future when the broker's ACK
            # arrives; _dispatch_error rejects it on TARGET_OFFLINE etc.
            await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            # ACK never arrived — clean up the dangling future.
            self._pending_acks.pop(msg.id, None)
            raise NMBConnectionError(f"No ACK for {msg.op.value} {msg.id} within {timeout}s")

    # ------------------------------------------------------------------
    # Public API: send
    # ------------------------------------------------------------------

    async def send(self, to: str, type: str, payload: dict[str, Any]) -> None:
        """Send a point-to-point message to a target sandbox.

        Waits for the broker's ACK to confirm acceptance.  Raises on
        broker errors (e.g. ``TARGET_OFFLINE``).

        Args:
            to: Target ``sandbox_id`` (globally unique per launch).
            type: Application-level message type (e.g. ``task.assign``).
            payload: Arbitrary JSON-serialisable payload.

        Raises:
            NMBConnectionError: If the target is offline, the broker
                rejects the message, or the connection is not open.
        """
        msg = NMBMessage(op=Op.SEND, to_sandbox=to, type=type, payload=payload)
        await self._send_and_await_ack(msg)

    # ------------------------------------------------------------------
    # Public API: request / reply
    # ------------------------------------------------------------------

    async def request(
        self,
        to: str,
        type: str,
        payload: dict[str, Any],
        timeout: float = DEFAULT_NMB_DEFAULT_REQUEST_TIMEOUT,
    ) -> NMBMessage:
        """Send a request and block until a reply arrives.

        The broker tracks the pending request and sends a ``timeout``
        frame if no reply appears within the given window.  The client
        adds a 5-second grace period on top of *timeout* to account for
        network jitter before the broker's own timeout fires.

        Args:
            to: Target ``sandbox_id`` (globally unique per launch).
            type: Application-level message type.
            payload: Request payload.
            timeout: Seconds to wait for a reply.

        Returns:
            The reply ``NMBMessage``.

        Raises:
            TimeoutError: If no reply arrives within *timeout*.
            NMBConnectionError: On connection errors or broker
                rejections (e.g. ``TARGET_OFFLINE``).
        """
        msg = NMBMessage(op=Op.REQUEST, to_sandbox=to, type=type, payload=payload, timeout=timeout)
        # Register the reply future before sending so a fast reply
        # (or a broker TIMEOUT frame) is never missed.
        fut: asyncio.Future[NMBMessage] = asyncio.get_running_loop().create_future()
        self._pending_futures[msg.id] = fut
        await self._conn.send(msg.to_json())
        try:
            # +5 s grace: the broker fires its own TIMEOUT at `timeout`
            # seconds.  We wait a bit longer so the broker's frame
            # reaches us before we give up locally.
            return await asyncio.wait_for(fut, timeout=timeout + _REQUEST_TIMEOUT_GRACE_S)
        except TimeoutError:
            # Clean up — the broker already sent (or will send) a
            # TIMEOUT frame, but we timed out waiting for it.
            self._pending_futures.pop(msg.id, None)
            raise

    async def reply(self, original: NMBMessage, type: str, payload: dict[str, Any]) -> None:
        """Reply to a received request message.

        Args:
            original: The request message being replied to.  Its ``id``
                is used as the ``reply_to`` correlation key.
            type: Reply message type (e.g. ``review.feedback``).
            payload: Reply payload.

        Raises:
            NMBConnectionError: If the connection is not open.
        """
        msg = NMBMessage(op=Op.REPLY, reply_to=original.id, type=type, payload=payload)
        await self._conn.send(msg.to_json())

    # ------------------------------------------------------------------
    # Public API: pub/sub
    # ------------------------------------------------------------------

    async def subscribe(self, channel: str) -> AsyncIterator[NMBMessage]:
        """Subscribe to a pub/sub channel and yield delivered messages.

        The delivery queue is registered *before* the SUBSCRIBE frame is
        sent so that a fast broker delivery is never missed.  Multiple
        concurrent subscriptions to the same channel within a single
        client are supported — each gets its own bounded queue.

        Sends an UNSUBSCRIBE frame when the caller exits the iterator
        (via ``break``, ``return``, or exception).

        Args:
            channel: Channel name to subscribe to.

        Yields:
            ``NMBMessage`` instances published to the channel.

        Raises:
            NMBConnectionError: If the subscription ACK fails.
        """
        # Create this subscriber's personal delivery queue and register
        # it BEFORE sending the SUBSCRIBE frame so a fast delivery from
        # the broker is never missed by _dispatch_deliver.
        queue: asyncio.Queue[NMBMessage] = asyncio.Queue(
            maxsize=DEFAULT_NMB_CHANNEL_QUEUE_SIZE,
        )
        queues = self._channel_queues.setdefault(channel, [])
        queues.append(queue)
        try:
            # Tell the broker to start routing this channel to us.
            sub_msg = NMBMessage(op=Op.SUBSCRIBE, channel=channel)
            await self._send_and_await_ack(sub_msg)

            # Yield messages until the caller breaks out or the
            # connection closes.
            while not self._closed:
                msg = await queue.get()
                yield msg
        finally:
            # ── Cleanup: remove this subscriber's queue ──
            queues = self._channel_queues.get(channel, [])
            if queue in queues:
                queues.remove(queue)
            if not queues:
                # Last local subscriber for this channel — remove the
                # entry so _dispatch_deliver stops looking here, and
                # tell the broker to stop routing this channel to us.
                self._channel_queues.pop(channel, None)
                unsub = NMBMessage(op=Op.UNSUBSCRIBE, channel=channel)
                try:
                    await self._conn.send(unsub.to_json())
                except (NMBConnectionError, websockets.ConnectionClosed):
                    pass

    async def publish(self, channel: str, type: str, payload: dict[str, Any]) -> None:
        """Publish a message to a channel.

        Waits for the broker's ACK to confirm the publish was accepted.

        Args:
            channel: Target channel name.
            type: Application-level message type.
            payload: Message payload.

        Raises:
            NMBConnectionError: On broker error or connection loss.
        """
        msg = NMBMessage(op=Op.PUBLISH, channel=channel, type=type, payload=payload)
        await self._send_and_await_ack(msg)

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

        Sends each chunk with an incrementing ``seq`` number under a
        shared ``stream_id``.  A final empty-payload chunk with
        ``done=True`` signals stream completion.

        Stream chunks are **not** individually ACKed by the broker.

        Args:
            to: Target ``sandbox_id`` (globally unique per launch).
            type: Application-level message type for the stream.
            chunks: Async iterator yielding payload dicts per chunk.

        Raises:
            NMBConnectionError: If the connection is not open.
        """
        # All chunks in this stream share a single stream_id so the
        # receiver can reassemble them in order.
        stream_id = uuid.uuid4().hex
        seq = 0
        async for chunk_payload in chunks:
            msg = NMBMessage(
                op=Op.STREAM,
                to_sandbox=to,
                type=type,
                stream_id=stream_id,
                seq=seq,
                done=False,
                payload=chunk_payload,
            )
            await self._conn.send(msg.to_json())
            seq += 1

        # Final sentinel chunk: empty payload, done=True.
        done_msg = NMBMessage(
            op=Op.STREAM,
            to_sandbox=to,
            type=type,
            stream_id=stream_id,
            seq=seq,
            done=True,
            payload={},
        )
        await self._conn.send(done_msg.to_json())

    # ------------------------------------------------------------------
    # Public API: listen
    # ------------------------------------------------------------------

    async def listen(self) -> AsyncIterator[NMBMessage]:
        """Yield incoming deliver messages not matched by a subscription or pending request.

        Use this in sub-agent mode to process incoming tasks.  The
        listen queue is bounded; if the consumer falls behind, the
        oldest unprocessed message is dropped.

        Yields:
            ``NMBMessage`` instances delivered to this sandbox.
        """
        while not self._closed:
            try:
                # Poll with a 1-second timeout so we re-check _closed
                # periodically rather than blocking forever on an empty queue.
                msg = await asyncio.wait_for(
                    self._listen_queue.get(),
                    timeout=_LISTEN_POLL_INTERVAL_S,
                )
                yield msg
            except TimeoutError:
                continue  # Queue empty — loop back and check _closed.


# ---------------------------------------------------------------------------
# CLI entry point (mirrors the broker's __main__ pattern)
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> None:
    """``nmb-client`` CLI — command-line access to the NMB for testing,
    shell-based agent integrations, and debugging.

    Uses the synchronous ``MessageBus`` wrapper so the CLI can be
    invoked from a plain shell without an event loop.

    Usage::

        nmb-client --sandbox-id cli-debug send <to> <type> '<json>'
        nmb-client --sandbox-id cli-debug request <to> <type> '<json>'
        nmb-client --sandbox-id cli-debug listen
        nmb-client --sandbox-id cli-debug subscribe <channel>
        nmb-client --sandbox-id cli-debug publish <channel> <type> '<json>'

    Args:
        argv: Argument list to parse.  Defaults to ``sys.argv[1:]``.
    """
    import argparse
    import json
    import sys

    # Lazy import to avoid circular dependency: sync → client → sync.
    from nemoclaw_escapades.nmb.sync import MessageBus as SyncMessageBus

    parser = argparse.ArgumentParser(
        prog="nmb-client",
        description="NemoClaw Message Bus CLI",
    )
    parser.add_argument(
        "--sandbox-id",
        required=True,
        help="Human-readable sandbox name (e.g. 'cli-debug')",
    )
    parser.add_argument(
        "--url",
        default="ws://messages.local:9876",
        help="Broker WebSocket URL",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="Send a fire-and-forget message")
    p_send.add_argument("to", help="Target sandbox ID")
    p_send.add_argument("type", help="Message type")
    p_send.add_argument("payload", help="JSON payload")

    p_req = sub.add_parser("request", help="Send a request and wait for reply")
    p_req.add_argument("to", help="Target sandbox ID")
    p_req.add_argument("type", help="Message type")
    p_req.add_argument("payload", help="JSON payload")
    p_req.add_argument("--timeout", type=float, default=300.0, help="Reply timeout (seconds)")

    sub.add_parser("listen", help="Listen for incoming messages (blocking)")

    p_sub = sub.add_parser("subscribe", help="Subscribe to a channel")
    p_sub.add_argument("channel", help="Channel name")

    p_pub = sub.add_parser("publish", help="Publish to a channel")
    p_pub.add_argument("channel", help="Channel name")
    p_pub.add_argument("type", help="Message type")
    p_pub.add_argument("payload", help="JSON payload")

    args = parser.parse_args(argv)

    bus = SyncMessageBus(sandbox_id=args.sandbox_id, broker_url=args.url)

    try:
        bus.connect()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.command == "send":
            payload = json.loads(args.payload)
            bus.send(args.to, args.type, payload)
            print("Sent.")

        elif args.command == "request":
            payload = json.loads(args.payload)
            reply = bus.request(args.to, args.type, payload, timeout=args.timeout)
            print(reply.to_json())

        elif args.command == "listen":
            for msg in bus.listen():
                print(msg.to_json())
                sys.stdout.flush()

        elif args.command == "subscribe":
            for msg in bus.subscribe(args.channel):
                print(msg.to_json())
                sys.stdout.flush()

        elif args.command == "publish":
            payload = json.loads(args.payload)
            bus.publish(args.channel, args.type, payload)
            print("Published.")

    except KeyboardInterrupt:
        pass
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON payload: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        bus.close()


if __name__ == "__main__":
    _main()
