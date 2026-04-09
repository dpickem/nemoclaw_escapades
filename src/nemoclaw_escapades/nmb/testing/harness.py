"""Integration test harness for multi-sandbox NMB testing.

:class:`IntegrationHarness` manages a :class:`PolicyBroker` and a
set of :class:`SandboxHandle` instances.  It handles the full
lifecycle — start broker, connect sandboxes, collect messages,
and teardown — so that test functions can focus on sending traffic
and asserting outcomes.

The harness bridges the gap between **display names** (human-readable
names used in test fixtures, e.g. ``"orchestrator"``) and
**sandbox_ids** (globally unique per-launch IDs that the broker
routes on, e.g. ``"orchestrator-a3f7b2c8"``).  Policies are
initially registered by display name, then re-keyed to unique IDs
after each ``MessageBus`` is constructed.  The ``_resolve`` helper
translates display names back to unique IDs so tests can write::

    await harness["orchestrator"].send("coding-1", "task.assign", {})

without knowing the random suffixes.

Usage in a pytest fixture::

    @pytest.fixture
    async def harness():
        h = IntegrationHarness()
        yield h
        await h.stop()

    async def test_example(harness):
        await harness.start([
            SandboxPolicy(sandbox_id="orch"),
            SandboxPolicy(sandbox_id="worker"),
        ])
        await harness["orch"].send("worker", "ping", {})
        msg = await harness["worker"].wait_for_message("ping")
        assert msg.payload == {}
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from collections.abc import Callable

from nemoclaw_escapades.config import BrokerConfig
from nemoclaw_escapades.nmb.client import MessageBus
from nemoclaw_escapades.nmb.models import NMBMessage
from nemoclaw_escapades.nmb.testing.policy import PolicyBroker, SandboxPolicy

# Callable that maps a display name (or already-unique sandbox_id)
# to the globally unique sandbox_id used for routing.
_Resolver = Callable[[str], str]


# ---------------------------------------------------------------------------
# Sandbox handle
# ---------------------------------------------------------------------------


class SandboxHandle:
    """Convenience wrapper around a :class:`MessageBus` connected to the
    test broker.

    Automatically collects incoming ``deliver`` messages in a
    background task so tests can assert on received traffic after
    the fact (via :attr:`received` and :meth:`wait_for_message`).

    The ``send``, ``request``, and ``stream`` helpers accept either a
    display name (e.g. ``"coding-1"``) or the full unique
    ``sandbox_id`` in the *to* parameter.  Display names are resolved
    to unique IDs via the harness's ``_resolve`` method.

    Attributes:
        display_name: Human-readable name passed to the harness
            (e.g. ``"orchestrator"``).
        bus: The underlying async ``MessageBus``.  Its ``sandbox_id``
            is the globally unique per-launch ID.
        policy: The :class:`SandboxPolicy` under which this sandbox
            operates in the test.
        received: All ``deliver`` messages collected by the background
            listener (append-only list, never cleared).
    """

    def __init__(
        self,
        display_name: str,
        bus: MessageBus,
        policy: SandboxPolicy,
        resolver: _Resolver,
    ) -> None:
        """Create a handle wrapping an already-connected bus.

        Args:
            display_name: Human-readable name for this sandbox.
            bus: Connected ``MessageBus`` instance.
            policy: Policy declaration for this sandbox.
            resolver: Callable that maps display names → unique IDs.
                Provided by the harness.
        """
        self.display_name = display_name
        self.bus = bus
        self.policy = policy
        self._resolve = resolver
        self.received: list[NMBMessage] = []
        self._listen_task: asyncio.Task[None] | None = None

    # -- Background collection ---------------------------------------------

    async def start_collecting(self) -> None:
        """Start collecting unmatched deliver messages in the background.

        Spawns ``_collect_loop`` as an ``asyncio.Task`` that appends
        every message from ``bus.listen()`` to :attr:`received`.

        Side effects:
            Populates ``_listen_task``.
        """
        self._listen_task = asyncio.create_task(self._collect_loop())

    async def _collect_loop(self) -> None:
        """Background task: drain ``bus.listen()`` into :attr:`received`.

        Runs until cancelled by :meth:`stop_collecting` or until the
        bus connection closes.
        """
        try:
            async for msg in self.bus.listen():
                self.received.append(msg)
        except asyncio.CancelledError:
            pass

    async def stop_collecting(self) -> None:
        """Cancel the background collection task.

        Safe to call even if collection was never started.
        """
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

    # -- Query helpers -----------------------------------------------------

    async def wait_for_message(
        self,
        msg_type: str | None = None,
        *,
        timeout: float = 5.0,
    ) -> NMBMessage:
        """Block until a matching message appears in :attr:`received`.

        Polls :attr:`received` every 50 ms.  Returns the first message
        whose ``type`` matches *msg_type* (or any message if *msg_type*
        is ``None``).

        Args:
            msg_type: Required ``type`` field value, or ``None`` to
                match any message.
            timeout: Maximum seconds to wait.

        Returns:
            The first matching ``NMBMessage``.

        Raises:
            TimeoutError: If no matching message arrives within
                *timeout*.  The error includes a summary of what WAS
                received to aid debugging.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            # Scan the received list for a match.
            for msg in self.received:
                if msg_type is None or msg.type == msg_type:
                    return msg
            # No match yet — check deadline.
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                types = [m.type for m in self.received]
                raise TimeoutError(
                    f"No message (type={msg_type!r}) within {timeout}s; "
                    f"received {len(self.received)} messages: {types}"
                )
            # Yield control briefly so the collect loop can append more.
            await asyncio.sleep(0.05)

    def messages_of_type(self, msg_type: str) -> list[NMBMessage]:
        """Return all received messages with the given ``type``.

        Args:
            msg_type: The ``type`` field to filter on.

        Returns:
            A (possibly empty) list of matching messages in arrival
            order.
        """
        return [m for m in self.received if m.type == msg_type]

    # -- Delegated MessageBus API ------------------------------------------
    # These thin wrappers resolve display names → unique sandbox_ids
    # before delegating to the underlying bus.

    async def send(self, to: str, type: str, payload: dict[str, Any]) -> None:
        """Send a point-to-point message via the bus.

        Args:
            to: Display name or unique ``sandbox_id`` of the target.
            type: Application-level message type.
            payload: Message payload.

        Raises:
            NMBConnectionError: If the target is offline.
        """
        await self.bus.send(self._resolve(to), type, payload)

    async def request(
        self,
        to: str,
        type: str,
        payload: dict[str, Any],
        timeout: float = 5.0,
    ) -> NMBMessage:
        """Send a request and await the reply.

        Args:
            to: Display name or unique ``sandbox_id`` of the target.
            type: Application-level message type.
            payload: Request payload.
            timeout: Seconds to wait for a reply.

        Returns:
            The reply ``NMBMessage``.

        Raises:
            TimeoutError: If no reply within *timeout*.
            NMBConnectionError: On connection errors.
        """
        return await self.bus.request(self._resolve(to), type, payload, timeout)

    async def reply(
        self, original: NMBMessage, type: str, payload: dict[str, Any]
    ) -> None:
        """Reply to a received request.

        Args:
            original: The request message being replied to.
            type: Reply message type.
            payload: Reply payload.
        """
        await self.bus.reply(original, type, payload)

    async def publish(
        self, channel: str, type: str, payload: dict[str, Any]
    ) -> None:
        """Publish to a channel.

        Args:
            channel: Target channel name.
            type: Message type.
            payload: Message payload.
        """
        await self.bus.publish(channel, type, payload)

    def subscribe(self, channel: str) -> Any:
        """Subscribe to a channel (returns an async iterator).

        Args:
            channel: Channel name to subscribe to.

        Returns:
            An ``AsyncIterator[NMBMessage]`` — use with ``async for``.
        """
        return self.bus.subscribe(channel)

    async def stream(
        self, to: str, type: str, chunks: Any
    ) -> None:
        """Stream ordered chunks to a target.

        Args:
            to: Display name or unique ``sandbox_id`` of the target.
            type: Application-level message type.
            chunks: Async iterator yielding payload dicts.
        """
        await self.bus.stream(self._resolve(to), type, chunks)

    async def close(self) -> None:
        """Stop collecting and close the bus connection.

        Side effects:
            Cancels the background collection task and disconnects
            the underlying ``MessageBus``.
        """
        await self.stop_collecting()
        await self.bus.close()


# ---------------------------------------------------------------------------
# Integration harness
# ---------------------------------------------------------------------------


class IntegrationHarness:
    """Lifecycle manager for multi-sandbox NMB integration tests.

    Manages a :class:`PolicyBroker` and a set of connected
    :class:`SandboxHandle` instances.  Designed for use as a
    pytest fixture — call :meth:`start` to spin up the topology
    and :meth:`stop` to tear it down.

    Sandboxes are accessible by display name via dict-style indexing::

        harness["orchestrator"].send("coding-1", "task.assign", {})

    The harness maintains a ``_display_to_unique`` mapping so tests
    can address sandboxes by human-readable names while the broker
    routes on globally unique IDs.

    Attributes:
        _broker: The running ``PolicyBroker``, or ``None`` before
            ``start()``.
        _sandboxes: Display name → ``SandboxHandle`` for all
            connected sandboxes.
        _display_to_unique: Display name → globally unique
            ``sandbox_id`` (used by ``_resolve``).
        _broker_url: The ``ws://`` URL of the running broker.
    """

    def __init__(self) -> None:
        self._broker: PolicyBroker | None = None
        self._sandboxes: dict[str, SandboxHandle] = {}
        self._display_to_unique: dict[str, str] = {}
        self._broker_url: str = ""

    # -- Lifecycle ---------------------------------------------------------

    async def start(
        self,
        policies: list[SandboxPolicy],
        *,
        broker_config: BrokerConfig | None = None,
    ) -> None:
        """Start the policy broker and connect all allowed sandboxes.

        For each policy:

        1. Creates a ``MessageBus`` (which generates a unique
           ``sandbox_id``).
        2. Re-keys the broker's policy from the display name to the
           unique ID so that ``_process_request`` and
           ``_enforce_policy`` find the right policy at runtime.
        3. If ``can_connect`` is ``True``, connects the bus and starts
           a background message collector.

        Sandboxes whose ``can_connect`` is ``False`` are registered
        in the broker's policy table (so the broker can reject them)
        but are *not* connected.

        Args:
            policies: Per-sandbox policy declarations.
            broker_config: Optional override for the broker config.
                Defaults to ``127.0.0.1:0`` (OS-assigned port) with a
                temp audit DB and a 5 s request timeout.

        Side effects:
            Populates ``_broker``, ``_broker_url``, ``_sandboxes``,
            and ``_display_to_unique``.
        """
        # ── Start the broker ──
        db_path = str(Path(tempfile.mkdtemp()) / "integration_test.db")
        config = broker_config or BrokerConfig(
            host="127.0.0.1",
            port=0,  # Let the OS pick a free port.
            audit_db_path=db_path,
            default_request_timeout=5.0,
        )

        self._broker = PolicyBroker(config, policies)
        await self._broker.start()

        # Extract the actual bound address (port=0 → OS-assigned).
        if self._broker._server is None:
            raise RuntimeError("PolicyBroker.start() did not create a server")
        for sock in self._broker._server.sockets:
            addr = sock.getsockname()
            self._broker_url = f"ws://{addr[0]}:{addr[1]}"
            break

        # ── Connect each sandbox ──
        for policy in policies:
            display_name = policy.sandbox_id

            # Creating a MessageBus generates the unique sandbox_id
            # (e.g. "orchestrator-a3f7b2c8").
            bus = MessageBus(sandbox_id=display_name, broker_url=self._broker_url)

            # Re-key the broker's policy dict from the display name to
            # the unique ID so policy lookups work at connection and
            # dispatch time.
            self._broker.rekey_policy(display_name, bus.sandbox_id)
            self._display_to_unique[display_name] = bus.sandbox_id

            if not policy.can_connect:
                # Policy says this sandbox is blocked — don't connect,
                # but the re-keyed policy stays so the broker can
                # reject the connection with 403 if someone tries.
                continue

            await bus.connect()
            handle = SandboxHandle(display_name, bus, policy, self._resolve)
            await handle.start_collecting()
            self._sandboxes[display_name] = handle

        # Brief sleep so all connections finish registering with the
        # broker before the test starts sending traffic.
        await asyncio.sleep(0.05)

    async def stop(self) -> None:
        """Disconnect all sandboxes and stop the broker.

        Safe to call even if ``start()`` was never called.

        Side effects:
            Clears ``_sandboxes`` and stops ``_broker``.
        """
        for handle in list(self._sandboxes.values()):
            await handle.close()
        self._sandboxes.clear()
        if self._broker:
            await self._broker.stop()

    # -- Accessors ---------------------------------------------------------

    @property
    def broker(self) -> PolicyBroker:
        """The running :class:`PolicyBroker`.

        Raises:
            AssertionError: If ``start()`` has not been called.
        """
        if self._broker is None:
            raise RuntimeError("Harness not started — call start() first")
        return self._broker

    @property
    def broker_url(self) -> str:
        """The ``ws://`` URL of the running broker."""
        return self._broker_url

    def sandbox(self, name: str) -> SandboxHandle:
        """Look up a connected sandbox by display name.

        Args:
            name: The display name (e.g. ``"orchestrator"``).

        Returns:
            The corresponding ``SandboxHandle``.

        Raises:
            KeyError: If no sandbox with that name is connected.
        """
        return self._sandboxes[name]

    def __getitem__(self, name: str) -> SandboxHandle:
        """Dict-style access: ``harness["orchestrator"]``."""
        return self._sandboxes[name]

    def _resolve(self, name: str) -> str:
        """Resolve a display name to the unique ``sandbox_id``.

        If *name* is already a unique ID (not in the display map),
        it is returned as-is — so callers can pass either form.

        Args:
            name: Display name (e.g. ``"coding-1"``) or unique
                sandbox_id.

        Returns:
            The globally unique ``sandbox_id``.
        """
        return self._display_to_unique.get(name, name)

    # -- Dynamic topology --------------------------------------------------

    async def add_sandbox(self, policy: SandboxPolicy) -> SandboxHandle:
        """Connect a new sandbox after :meth:`start`.

        Follows the same create → rekey → connect → collect pattern
        as ``start()`` but for a single sandbox.

        Args:
            policy: Policy declaration for the new sandbox.

        Returns:
            The newly created ``SandboxHandle``.

        Side effects:
            Adds to ``_sandboxes`` and ``_display_to_unique``.
            Re-keys the broker's policy table.
        """
        display_name = policy.sandbox_id
        bus = MessageBus(sandbox_id=display_name, broker_url=self._broker_url)

        if self._broker:
            self._broker.rekey_policy(display_name, bus.sandbox_id)

        self._display_to_unique[display_name] = bus.sandbox_id
        await bus.connect()
        handle = SandboxHandle(display_name, bus, policy, self._resolve)
        await handle.start_collecting()
        self._sandboxes[display_name] = handle

        # Brief sleep so the connection registers with the broker.
        await asyncio.sleep(0.05)
        return handle

    async def remove_sandbox(self, name: str) -> None:
        """Disconnect and remove a sandbox by display name.

        Args:
            name: The display name of the sandbox to remove.

        Side effects:
            Removes from ``_sandboxes``.  The bus is closed and the
            broker receives a disconnection event.
        """
        handle = self._sandboxes.pop(name, None)
        if handle:
            await handle.close()
            # Brief sleep so the broker processes the disconnect and
            # broadcasts the shutdown system event.
            await asyncio.sleep(0.1)
