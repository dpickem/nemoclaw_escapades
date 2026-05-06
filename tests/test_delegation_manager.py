"""Tests for the orchestrator-side ``DelegationManager``.

Phase 3b's manager is fire-and-forget: ``delegate(task)`` spawns a
sub-agent process, sends ``task.assign``, and returns immediately
with a :class:`DelegationResult`.  The sub-agent's ``task.complete``
/ ``task.error`` arrivals are routed by the orchestrator's
:class:`WorkflowDispatcher` (tested in ``test_dispatcher.py``).

These tests cover the manager's lifecycle behaviour:

- spawn-depth gating,
- the readiness retry on ``TARGET_OFFLINE``,
- the per-agent semaphore concurrency cap,
- and the ``close()`` teardown contract.

They use a stub bus / spawn callback so no real subprocess or NMB
connection is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from nemoclaw_escapades.config import DelegationConfig
from nemoclaw_escapades.nmb.client import NMBConnectionError
from nemoclaw_escapades.nmb.models import NMBMessage
from nemoclaw_escapades.nmb.protocol import TaskAssignPayload
from nemoclaw_escapades.orchestrator.delegation import (
    DelegationError,
    DelegationManager,
    SpawnedAgent,
)


def _task(workflow_id: str = "wf-1") -> TaskAssignPayload:
    return TaskAssignPayload(
        prompt="x",
        workflow_id=workflow_id,
        parent_sandbox_id="orchestrator",
        agent_id=f"coding-{workflow_id[-4:]}",
        workspace_root="/tmp/wf",
    )


class _StubBus:
    """Minimal :class:`MessageBus` stub: records sends; never receives."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
        self.sent.append((to, type, payload))

    async def listen(self) -> AsyncIterator[NMBMessage]:
        # Block forever — the dispatcher tests use a different bus stub.
        await asyncio.Event().wait()
        # Unreachable, satisfies the type checker.
        yield  # type: ignore[unreachable]


async def _spawn_stub(sandbox_id: str, workspace_root: str) -> SpawnedAgent:
    """Spawn callback that records identity but doesn't fork a process."""

    async def _terminate() -> None:
        return None

    return SpawnedAgent(sandbox_id=sandbox_id, terminate=_terminate)


class TestDelegateFireAndForget:
    @pytest.mark.asyncio
    async def test_delegate_sends_task_assign_and_returns(self) -> None:
        bus = _StubBus()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(),
            spawn_callback=_spawn_stub,
        )
        result = await mgr.delegate(_task())
        assert result.workflow_id == "wf-1"
        assert result.sub_agent_sandbox_id == "coding-wf-1"
        assert len(bus.sent) == 1
        to, msg_type, _ = bus.sent[0]
        assert to == "coding-wf-1"
        assert msg_type == "task.assign"

    @pytest.mark.asyncio
    async def test_delegate_terminates_on_send_failure(self) -> None:
        terminations: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            async def _term() -> None:
                terminations.append(sandbox_id)

            return SpawnedAgent(sandbox_id=sandbox_id, terminate=_term)

        class _BrokenBus(_StubBus):
            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                raise RuntimeError("broker exploded")

        mgr = DelegationManager(
            _BrokenBus(),  # type: ignore[arg-type]
            DelegationConfig(spawn_ready_timeout_s=0.1),
            spawn_callback=_spawn,
        )
        with pytest.raises(DelegationError):
            await mgr.delegate(_task())
        assert terminations == ["coding-wf-1"]

    @pytest.mark.asyncio
    async def test_delegate_rejects_when_max_spawn_depth_zero(self) -> None:
        mgr = DelegationManager(
            _StubBus(),  # type: ignore[arg-type]
            DelegationConfig(max_spawn_depth=0),
            spawn_callback=_spawn_stub,
        )
        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_task())
        assert "max_spawn_depth=0" in str(excinfo.value)


class TestSemaphoreCap:
    @pytest.mark.asyncio
    async def test_max_concurrent_blocks_third_call(self) -> None:
        """``max_concurrent=2`` lets two run; the third waits for a slot."""
        # Each ``send`` waits on an event so we can hold the
        # delegation in flight; events[0..2] gate workflows 0..2.
        events = [asyncio.Event() for _ in range(3)]

        class _BlockingBus(_StubBus):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                idx = self.calls
                self.calls += 1
                self.sent.append((to, type, payload))
                await events[idx].wait()

        spawn_calls: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            spawn_calls.append(sandbox_id)

            async def _term() -> None:
                return None

            return SpawnedAgent(sandbox_id=sandbox_id, terminate=_term)

        bus = _BlockingBus()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(max_concurrent=2),
            spawn_callback=_spawn,
        )

        # Fire three concurrent delegations.
        delegations = [
            asyncio.create_task(mgr.delegate(_task(f"wf-{i}"))) for i in range(3)
        ]
        # Yield enough times for the first two to spawn; semaphore
        # blocks the third before it can spawn.
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(spawn_calls) == 2, f"expected 2 spawned, got {len(spawn_calls)}"

        # Release the first delegation → its semaphore slot frees,
        # the third spawns and proceeds.
        events[0].set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(spawn_calls) == 3

        events[1].set()
        events[2].set()
        await asyncio.gather(*delegations)


class TestSpawnReadinessRetry:
    """A freshly spawned sub-agent isn't immediately reachable on NMB.

    The manager must retry ``bus.send`` while the broker rejects
    with ``TARGET_OFFLINE``, capped by ``spawn_ready_timeout_s``.
    """

    @pytest.mark.asyncio
    async def test_retries_target_offline_until_subagent_connects(self) -> None:
        """First two sends fail with ``TARGET_OFFLINE``; the third succeeds."""

        class _ScriptedBus(_StubBus):
            def __init__(self, script: list[BaseException | None]) -> None:
                super().__init__()
                self._script = list(script)
                self.attempts = 0

            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                self.attempts += 1
                self.sent.append((to, type, payload))
                outcome = self._script.pop(0) if self._script else None
                if outcome is not None:
                    raise outcome

        bus = _ScriptedBus(
            [
                NMBConnectionError("Broker error TARGET_OFFLINE: not connected"),
                NMBConnectionError("Broker error TARGET_OFFLINE: not connected"),
                None,
            ]
        )
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(
                spawn_ready_timeout_s=2.0,
                spawn_ready_poll_interval_s=0.01,
            ),
            spawn_callback=_spawn_stub,
        )
        result = await mgr.delegate(_task())
        assert result.workflow_id == "wf-1"
        assert bus.attempts == 3

    @pytest.mark.asyncio
    async def test_persistent_target_offline_eventually_fails(self) -> None:
        """The readiness window enforces an upper bound."""

        class _AlwaysOfflineBus(_StubBus):
            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                self.sent.append((to, type, payload))
                raise NMBConnectionError("Broker error TARGET_OFFLINE: not connected")

        mgr = DelegationManager(
            _AlwaysOfflineBus(),  # type: ignore[arg-type]
            DelegationConfig(
                spawn_ready_timeout_s=0.1,
                spawn_ready_poll_interval_s=0.01,
            ),
            spawn_callback=_spawn_stub,
        )
        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_task())
        assert "never came online" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_non_target_offline_error_propagates_immediately(self) -> None:
        """Real broker errors don't trigger the readiness retry."""

        class _RateLimitedBus(_StubBus):
            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                self.sent.append((to, type, payload))
                raise NMBConnectionError("Broker error RATE_LIMITED: slow down")

        mgr = DelegationManager(
            _RateLimitedBus(),  # type: ignore[arg-type]
            DelegationConfig(spawn_ready_timeout_s=10.0),
            spawn_callback=_spawn_stub,
        )
        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_task())
        assert "RATE_LIMITED" in str(excinfo.value)


class TestDelegationManagerClose:
    @pytest.mark.asyncio
    async def test_close_terminates_in_flight_sub_agents(self) -> None:
        terminations: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            async def _term() -> None:
                terminations.append(sandbox_id)

            return SpawnedAgent(sandbox_id=sandbox_id, terminate=_term)

        # ``send`` blocks forever so the spawned agent is still tracked
        # when we call ``close()``.
        class _SlowBus(_StubBus):
            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                # Cooperate with the test by completing fast; the agent
                # is registered before this returns.
                self.sent.append((to, type, payload))

        bus = _SlowBus()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(),
            spawn_callback=_spawn,
        )
        await mgr.delegate(_task("wf-a"))
        await mgr.delegate(_task("wf-b"))
        await mgr.close()
        assert sorted(terminations) == ["coding-wf-a", "coding-wf-b"]

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        mgr = DelegationManager(
            _StubBus(),  # type: ignore[arg-type]
            DelegationConfig(),
            spawn_callback=_spawn_stub,
        )
        await mgr.close()
        await mgr.close()  # must not raise
