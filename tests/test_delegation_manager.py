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
    """``max_concurrent`` is a hard cap on **live** workflows.

    Slots are held for the full workflow lifetime: acquired when
    :meth:`DelegationManager.delegate` first installs an entry in
    ``_spawned``, released when :meth:`terminate` (or :meth:`close`)
    pops it.  Sub-agents live well past the fire-and-forget
    ``send``, so scoping the semaphore to spawn+send only — as the
    Phase 3b refactor briefly did — let unbounded sub-agent
    processes accumulate while the cap silently became fiction.
    """

    @pytest.mark.asyncio
    async def test_max_concurrent_blocks_until_a_slot_is_released(self) -> None:
        """Excess calls block until ``terminate`` frees a slot."""
        spawn_calls: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            spawn_calls.append(sandbox_id)

            async def _term() -> None:
                return None

            return SpawnedAgent(sandbox_id=sandbox_id, terminate=_term)

        mgr = DelegationManager(
            _StubBus(),  # type: ignore[arg-type]
            DelegationConfig(max_concurrent=2),
            spawn_callback=_spawn,
        )

        # Fire three concurrent delegations.  Two spawn immediately;
        # the third must block on the semaphore.
        delegations = [asyncio.create_task(mgr.delegate(_task(f"wf-{i}"))) for i in range(3)]
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(spawn_calls) == 2, f"expected 2 spawned, got {len(spawn_calls)}"
        assert not delegations[2].done(), "third delegation must block on the semaphore"

        # Free a slot via terminate; the third proceeds.
        await mgr.terminate("wf-0")
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(spawn_calls) == 3
        await asyncio.gather(*delegations)

        # Cleanup.
        await mgr.close()

    @pytest.mark.asyncio
    async def test_send_completion_does_not_free_a_slot(self) -> None:
        """Regression for the fire-and-forget cap regression.

        ``async with self._semaphore`` previously released the slot
        as soon as ``send`` returned, so a fast send (the common
        case) would let an unbounded number of sub-agents pile up
        even though :class:`DelegationConfig.max_concurrent`
        promised a hard cap.  This test pins the contract: after
        two delegations land their sends, a third must still block
        because the slots are held by the live workflows.
        """
        spawn_calls: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            spawn_calls.append(sandbox_id)

            async def _term() -> None:
                return None

            return SpawnedAgent(sandbox_id=sandbox_id, terminate=_term)

        mgr = DelegationManager(
            _StubBus(),  # type: ignore[arg-type]  # send returns immediately
            DelegationConfig(max_concurrent=2),
            spawn_callback=_spawn,
        )

        # Two synchronous, fully-completed delegations.  Sends
        # finished, slots must still be held because the workflows
        # are alive in the registry.
        await mgr.delegate(_task("wf-0"))
        await mgr.delegate(_task("wf-1"))
        assert len(spawn_calls) == 2
        assert set(mgr._spawned) == {"wf-0", "wf-1"}

        # Third delegation must block — even though both prior
        # sends have already returned successfully.
        third = asyncio.create_task(mgr.delegate(_task("wf-2")))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not third.done(), (
            "max_concurrent must cap live workflows, not concurrent "
            "spawn operations; the third delegate must wait for a "
            "live workflow to terminate"
        )
        assert len(spawn_calls) == 2

        # Releasing a slot lets the third proceed.
        await mgr.terminate("wf-1")
        await third
        assert len(spawn_calls) == 3
        await mgr.close()

    @pytest.mark.asyncio
    async def test_re_delegation_does_not_consume_extra_slot(self) -> None:
        """Re-delegation reuses the workflow's existing slot.

        Iteration N+1 stays inside the slot iteration 1 acquired —
        otherwise a workflow that re-delegated more times than
        ``max_concurrent`` would deadlock against itself.
        """
        spawn_calls: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            spawn_calls.append(sandbox_id)

            async def _term() -> None:
                return None

            return SpawnedAgent(sandbox_id=sandbox_id, terminate=_term)

        mgr = DelegationManager(
            _StubBus(),  # type: ignore[arg-type]
            DelegationConfig(max_concurrent=1),
            spawn_callback=_spawn,
        )

        # Initial delegation grabs the only slot.
        await mgr.delegate(_task("wf-iter"))
        # Three re-delegations on the same workflow_id must all
        # succeed without blocking — same slot, different agents.
        for _ in range(3):
            await mgr.delegate(_task("wf-iter"))
        assert len(spawn_calls) == 4

        # An unrelated workflow must block — only one slot, held by
        # the iteration chain above.
        other = asyncio.create_task(mgr.delegate(_task("wf-other")))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not other.done(), (
            "unrelated workflow must still block on the cap even "
            "after multiple re-delegations on a peer workflow"
        )

        # Free the iteration's slot; the other workflow proceeds.
        await mgr.terminate("wf-iter")
        await other
        await mgr.close()

    @pytest.mark.asyncio
    async def test_send_failure_releases_slot(self) -> None:
        """A send-failure must give the slot back."""
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
            DelegationConfig(max_concurrent=1, spawn_ready_timeout_s=0.1),
            spawn_callback=_spawn,
        )

        # First delegation fails on send; slot must be released so
        # the second delegation isn't blocked by a phantom workflow.
        # ``_task`` derives ``agent_id`` from the last 4 chars of the
        # workflow_id, so wf-fails → coding-ails.
        with pytest.raises(DelegationError):
            await mgr.delegate(_task("wf-fails"))
        assert mgr._spawned == {}
        assert "coding-ails" in terminations

        # If the slot leaked, this would hang forever — the test
        # would time out.
        with pytest.raises(DelegationError):
            await asyncio.wait_for(mgr.delegate(_task("wf-bust")), timeout=1.0)
        assert mgr._spawned == {}

    @pytest.mark.asyncio
    async def test_re_delegation_send_failure_releases_slot(self) -> None:
        """Re-delegation send-failure releases the slot the prior iteration held."""
        terminations: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            tag = f"{sandbox_id}-{len(terminations)}"

            async def _term() -> None:
                terminations.append(tag)

            return SpawnedAgent(sandbox_id=tag, terminate=_term)

        class _SendFailsOnReDelegate(_StubBus):
            """Fails only on the second ``send`` call.

            Call 1 lets iteration 1 of wf-iter succeed; call 2 fails
            so we can exercise the re-delegation send-failure path;
            call 3+ succeed so the post-failure unrelated delegation
            (wf-other) can claim the freed slot.
            """

            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("re-delegation send failed")
                self.sent.append((to, type, payload))

        mgr = DelegationManager(
            _SendFailsOnReDelegate(),  # type: ignore[arg-type]
            DelegationConfig(max_concurrent=1, spawn_ready_timeout_s=0.1),
            spawn_callback=_spawn,
        )

        # Iteration 1 succeeds; the only slot is now held by wf-iter.
        await mgr.delegate(_task("wf-iter"))
        assert "wf-iter" in mgr._spawned

        # Iteration 2 send-fails; the workflow should be cleared
        # and its slot returned, even though *this* call did not
        # acquire (it was reusing iteration 1's slot).
        with pytest.raises(DelegationError):
            await mgr.delegate(_task("wf-iter"))
        assert mgr._spawned == {}

        # An unrelated workflow must now be able to claim the freed
        # slot without blocking.
        await asyncio.wait_for(mgr.delegate(_task("wf-other")), timeout=1.0)
        assert "wf-other" in mgr._spawned
        await mgr.close()

    @pytest.mark.asyncio
    async def test_close_releases_all_slots(self) -> None:
        """``close()`` must give every held slot back."""
        spawn_calls: list[str] = []

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            spawn_calls.append(sandbox_id)

            async def _term() -> None:
                return None

            return SpawnedAgent(sandbox_id=sandbox_id, terminate=_term)

        mgr = DelegationManager(
            _StubBus(),  # type: ignore[arg-type]
            DelegationConfig(max_concurrent=2),
            spawn_callback=_spawn,
        )
        await mgr.delegate(_task("wf-0"))
        await mgr.delegate(_task("wf-1"))
        await mgr.close()

        # After close, both slots must be free; a fresh delegate
        # call should not block.
        await asyncio.wait_for(mgr.delegate(_task("wf-2")), timeout=1.0)
        assert set(mgr._spawned) == {"wf-2"}
        await mgr.close()


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


class TestReDelegateReplacesPreviousAgent:
    """Re-delegation must terminate the old ``SpawnedAgent`` cleanly.

    Regression for the leak that landed when ``delegate`` started
    being called twice with the same ``workflow_id`` (the
    finalisation flow's ``re_delegate`` path):
    ``self._spawned[task.workflow_id] = agent`` overwrote the
    previous iteration's handle without terminating it, so
    ``terminate(workflow_id)`` and ``close()`` could only ever
    reach the latest agent and the leak grew by one stale handle
    per re-delegation.
    """

    @pytest.mark.asyncio
    async def test_re_delegate_terminates_previous_iteration(self) -> None:
        terminated: list[str] = []
        spawn_count = 0

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            nonlocal spawn_count
            spawn_count += 1
            tag = sandbox_id  # captured for the closure below
            iteration_id = f"{tag}-iter-{spawn_count}"

            async def _term() -> None:
                terminated.append(iteration_id)

            return SpawnedAgent(sandbox_id=iteration_id, terminate=_term)

        bus = _StubBus()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(),
            spawn_callback=_spawn,
        )
        # Iteration 1: register the first agent.
        await mgr.delegate(_task("wf-iter"))
        first_handle = mgr._spawned["wf-iter"]
        assert first_handle.sandbox_id == "coding-iter-iter-1"
        assert terminated == [], "First delegation must not terminate its own agent"

        # Iteration 2: re-delegation reuses the same workflow_id.
        await mgr.delegate(_task("wf-iter"))
        assert terminated == ["coding-iter-iter-1"], (
            "Iteration 1's SpawnedAgent must be terminated when "
            f"iteration 2 takes its slot; got: {terminated!r}"
        )
        # The dict now points at iteration 2's agent.
        second_handle = mgr._spawned["wf-iter"]
        assert second_handle is not first_handle
        assert second_handle.sandbox_id == "coding-iter-iter-2"

        # Iteration 3: same again, to confirm the cleanup is repeatable.
        await mgr.delegate(_task("wf-iter"))
        assert terminated == ["coding-iter-iter-1", "coding-iter-iter-2"]
        third_handle = mgr._spawned["wf-iter"]
        assert third_handle.sandbox_id == "coding-iter-iter-3"

        # ``close()`` finishes off the last live iteration's agent.
        await mgr.close()
        assert terminated == [
            "coding-iter-iter-1",
            "coding-iter-iter-2",
            "coding-iter-iter-3",
        ]
        assert mgr._spawned == {}

    @pytest.mark.asyncio
    async def test_re_delegate_send_failure_keeps_only_old_agent_terminated(
        self,
    ) -> None:
        """Iteration 2's send failure must not leak iteration 1.

        Pre-fix, the failure-path ``pop`` would have evicted
        iteration 2's handle (just installed by the unconditional
        overwrite) while iteration 1's handle had already been
        silently dropped from the dict.  Both leaked.  Post-fix,
        iteration 1 is terminated explicitly during the swap and
        iteration 2 is terminated explicitly on its send failure;
        ``_spawned`` ends up empty.
        """
        terminated: list[str] = []
        spawn_count = 0

        async def _spawn(sandbox_id: str, _workspace: str) -> SpawnedAgent:
            nonlocal spawn_count
            spawn_count += 1
            iteration_id = f"{sandbox_id}-iter-{spawn_count}"

            async def _term() -> None:
                terminated.append(iteration_id)

            return SpawnedAgent(sandbox_id=iteration_id, terminate=_term)

        class _BrokenOnSecondSend(_StubBus):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def send(self, *, to: str, type: str, payload: dict[str, Any]) -> None:
                self.calls += 1
                if self.calls >= 2:
                    raise RuntimeError("broker exploded on iteration 2")
                self.sent.append((to, type, payload))

        bus = _BrokenOnSecondSend()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(spawn_ready_timeout_s=0.1),
            spawn_callback=_spawn,
        )
        # Iteration 1 succeeds.
        await mgr.delegate(_task("wf-iter"))
        assert mgr._spawned["wf-iter"].sandbox_id == "coding-iter-iter-1"

        # Iteration 2 fails on send.
        with pytest.raises(DelegationError):
            await mgr.delegate(_task("wf-iter"))

        # Both iterations terminated; registry empty.
        assert sorted(terminated) == ["coding-iter-iter-1", "coding-iter-iter-2"]
        assert mgr._spawned == {}


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
