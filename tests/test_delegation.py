"""Tests for ``orchestrator/delegation.py`` — ``DelegationManager``.

The manager owns three concerns:

1. **Per-agent semaphore** caps concurrent in-flight delegations.
2. **Spawn callback** invocation per delegation, with a clean
   ``terminate`` even when the request fails.
3. **NMB request → validated reply** for the ``task.assign`` →
   ``task.complete`` round-trip.

These tests use fakes for both ``MessageBus.request`` and the
spawn callback — that keeps them pure unit-level.  Integration
testing of the orchestrator + sub-agent through a real broker
lives in ``tests/integration/test_delegation.py`` (Phase 3a-7).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nemoclaw_escapades.config import DelegationConfig
from nemoclaw_escapades.nmb.client import NMBConnectionError
from nemoclaw_escapades.nmb.protocol import (
    TASK_COMPLETE,
    TASK_ERROR,
    TaskAssignPayload,
    TaskCompletePayload,
    TaskErrorPayload,
    dump,
)
from nemoclaw_escapades.orchestrator.delegation import (
    DelegationError,
    DelegationManager,
    SpawnedAgent,
)

# ── Fakes ──────────────────────────────────────────────────────────


class _FakeNMBMessage:
    """Stand-in for :class:`NMBMessage` carrying just the fields the manager reads."""

    def __init__(self, type: str, payload: dict[str, Any]) -> None:
        self.type = type
        self.payload = payload


class _FakeBus:
    """Records the most recent ``request`` call and returns a scripted reply.

    The manager only uses ``bus.request``; nothing else is exercised,
    so we keep this minimal.
    """

    def __init__(
        self,
        reply_type: str = TASK_COMPLETE,
        reply_payload: dict[str, Any] | None = None,
        raise_on_request: BaseException | None = None,
    ) -> None:
        self.last_request: dict[str, Any] = {}
        self._reply_type = reply_type
        self._reply_payload = reply_payload
        self._raise = raise_on_request

    async def request(
        self,
        *,
        to: str,
        type: str,
        payload: dict[str, Any],
        timeout: float | None,
    ) -> _FakeNMBMessage:
        self.last_request = {
            "to": to,
            "type": type,
            "payload": payload,
            "timeout": timeout,
        }
        if self._raise:
            raise self._raise
        return _FakeNMBMessage(type=self._reply_type, payload=self._reply_payload or {})


class _SpawnRecorder:
    """Records every spawn invocation; returns a SpawnedAgent that just records terminate."""

    def __init__(self) -> None:
        self.spawned: list[tuple[str, str]] = []
        self.terminated: list[str] = []

    async def __call__(self, sandbox_id: str, workspace_root: str) -> SpawnedAgent:
        self.spawned.append((sandbox_id, workspace_root))

        async def _terminate() -> None:
            self.terminated.append(sandbox_id)

        return SpawnedAgent(sandbox_id=sandbox_id, terminate=_terminate)


# ── Helpers ────────────────────────────────────────────────────────


def _make_task(workflow_id: str = "wf-1", agent_id: str = "coding-12345678") -> TaskAssignPayload:
    """Build a minimal valid ``TaskAssignPayload`` for delegation tests."""
    return TaskAssignPayload(
        prompt="test task",
        workflow_id=workflow_id,
        parent_sandbox_id="orchestrator",
        agent_id=agent_id,
        workspace_root=f"/tmp/ws-{workflow_id}",
    )


def _complete_payload_dict(workflow_id: str = "wf-1") -> dict[str, Any]:
    """Minimal valid ``TaskCompletePayload`` dict."""
    return dump(TaskCompletePayload(workflow_id=workflow_id, summary="done"))


def _error_payload_dict(workflow_id: str = "wf-1", recoverable: bool = False) -> dict[str, Any]:
    return dump(
        TaskErrorPayload(
            workflow_id=workflow_id,
            error="something broke",
            error_kind="other",
            recoverable=recoverable,
        )
    )


# ── Successful delegation ──────────────────────────────────────────


class TestSuccessfulDelegation:
    @pytest.mark.asyncio
    async def test_assign_payload_round_trips_through_request(self) -> None:
        """The manager dumps the typed payload and forwards via ``bus.request``."""
        bus = _FakeBus(reply_payload=_complete_payload_dict())
        spawn = _SpawnRecorder()
        mgr = DelegationManager(bus, DelegationConfig(), spawn_callback=spawn)  # type: ignore[arg-type]
        task = _make_task()

        result = await mgr.delegate(task)

        # Bus saw the right ``to`` (sub-agent sandbox id), ``type``,
        # and a JSON-shaped payload that round-trips back to our task.
        assert bus.last_request["to"] == "coding-12345678"
        assert bus.last_request["type"] == "task.assign"
        assert bus.last_request["payload"]["prompt"] == "test task"
        assert bus.last_request["payload"]["workflow_id"] == "wf-1"
        assert bus.last_request["timeout"] == DelegationConfig().task_timeout_s

        # Result carries the validated complete payload + sandbox id.
        assert result.complete.workflow_id == "wf-1"
        assert result.complete.summary == "done"
        assert result.sub_agent_sandbox_id == "coding-12345678"

    @pytest.mark.asyncio
    async def test_spawn_and_terminate_called_in_order(self) -> None:
        """Sub-agent process is spawned *before* assign and torn down after."""
        bus = _FakeBus(reply_payload=_complete_payload_dict())
        spawn = _SpawnRecorder()
        mgr = DelegationManager(bus, DelegationConfig(), spawn_callback=spawn)  # type: ignore[arg-type]
        await mgr.delegate(_make_task())
        # One spawn and one terminate, both for the same sandbox.
        assert spawn.spawned == [("coding-12345678", "/tmp/ws-wf-1")]
        assert spawn.terminated == ["coding-12345678"]

    @pytest.mark.asyncio
    async def test_terminate_runs_even_when_request_raises(self) -> None:
        """A transport failure must not leak the spawned sub-agent process."""
        bus = _FakeBus(raise_on_request=RuntimeError("broker offline"))
        spawn = _SpawnRecorder()
        mgr = DelegationManager(bus, DelegationConfig(), spawn_callback=spawn)  # type: ignore[arg-type]

        with pytest.raises(DelegationError):
            await mgr.delegate(_make_task())

        # Spawn happened; terminate ran in the finally block.
        assert spawn.spawned == [("coding-12345678", "/tmp/ws-wf-1")]
        assert spawn.terminated == ["coding-12345678"]


# ── Failure modes ──────────────────────────────────────────────────


class TestFailureModes:
    @pytest.mark.asyncio
    async def test_task_error_reply_raises_with_payload(self) -> None:
        """A ``task.error`` reply raises with the typed payload preserved."""
        bus = _FakeBus(reply_type=TASK_ERROR, reply_payload=_error_payload_dict(recoverable=True))
        spawn = _SpawnRecorder()
        mgr = DelegationManager(bus, DelegationConfig(), spawn_callback=spawn)  # type: ignore[arg-type]

        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_make_task())

        # The wrapped TaskErrorPayload is preserved so finalization
        # can branch on ``recoverable``.
        assert excinfo.value.error_payload is not None
        assert excinfo.value.error_payload.recoverable is True
        assert excinfo.value.error_payload.error == "something broke"

    @pytest.mark.asyncio
    async def test_malformed_complete_reply_raises(self) -> None:
        """An unparseable ``task.complete`` raises with a clear message."""
        bus = _FakeBus(
            reply_type=TASK_COMPLETE,
            reply_payload={"workflow_id": "wf-1"},  # missing required `summary`
        )
        spawn = _SpawnRecorder()
        mgr = DelegationManager(bus, DelegationConfig(), spawn_callback=spawn)  # type: ignore[arg-type]

        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_make_task())

        assert "validation failed" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_unknown_reply_type_raises(self) -> None:
        """A reply that's neither ``task.complete`` nor ``task.error`` raises.

        The manager tries to interpret unknown reply types as
        ``task.error`` (so a misrouted reply still surfaces as a
        delegation failure rather than a hang).  Validation against
        the error model fails, and the wrapped error message names
        the unexpected reply type so the operator can chase the
        misroute.
        """
        bus = _FakeBus(
            reply_type="task.totally_unrelated",
            reply_payload={"foo": "bar"},
        )
        spawn = _SpawnRecorder()
        mgr = DelegationManager(bus, DelegationConfig(), spawn_callback=spawn)  # type: ignore[arg-type]

        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_make_task())

        assert "task.totally_unrelated" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_max_spawn_depth_zero_blocks_all_delegations(self) -> None:
        """A misconfigured ``max_spawn_depth=0`` must refuse every delegation.

        Defensive: an operator who accidentally sets the cap to 0
        in YAML should see the first ``delegate_task`` fail loudly,
        not silently produce a hang.
        """
        bus = _FakeBus(reply_payload=_complete_payload_dict())
        spawn = _SpawnRecorder()
        mgr = DelegationManager(
            bus,
            DelegationConfig(max_spawn_depth=0),
            spawn_callback=spawn,  # type: ignore[arg-type]
        )

        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_make_task())

        # Failed before spawn — caller's process count stays clean.
        assert "max_spawn_depth=0" in str(excinfo.value)
        assert spawn.spawned == []


# ── Concurrency caps ───────────────────────────────────────────────


class TestSemaphoreCap:
    @pytest.mark.asyncio
    async def test_third_call_blocks_until_a_slot_frees(self) -> None:
        """``max_concurrent=2`` lets two run; the third waits.

        Block the first two requests on an Event so the third has to
        queue.  Release one Event → the third call finishes and the
        spawn count goes to 3.
        """
        # Each request will await this event before replying — gives
        # us deterministic control over when delegations "complete".
        events = [asyncio.Event() for _ in range(3)]
        replies = [_complete_payload_dict(workflow_id=f"wf-{i}") for i in range(3)]

        class _BlockingBus:
            def __init__(self) -> None:
                self.calls = 0

            async def request(
                self,
                *,
                to: str,
                type: str,  # noqa: A002
                payload: dict[str, Any],
                timeout: float | None,
            ) -> _FakeNMBMessage:
                idx = self.calls
                self.calls += 1
                await events[idx].wait()
                return _FakeNMBMessage(type=TASK_COMPLETE, payload=replies[idx])

        spawn = _SpawnRecorder()
        bus = _BlockingBus()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(max_concurrent=2),
            spawn_callback=spawn,  # type: ignore[arg-type]
        )

        async def _run(idx: int) -> None:
            await mgr.delegate(_make_task(workflow_id=f"wf-{idx}", agent_id=f"coding-{idx:08d}"))

        # Start all three concurrently.
        tasks = [asyncio.create_task(_run(i)) for i in range(3)]
        # Yield enough times for the first two to spawn; semaphore
        # blocks the third before it can spawn.
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(spawn.spawned) == 2, f"expected 2 spawned, got {len(spawn.spawned)}"

        # Release the first delegation → its semaphore slot frees,
        # the third spawns and proceeds.
        events[0].set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(spawn.spawned) == 3

        # Release the rest so the test exits cleanly.
        events[1].set()
        events[2].set()
        await asyncio.gather(*tasks)
        assert spawn.terminated == ["coding-00000000", "coding-00000001", "coding-00000002"]


# ── Readiness retry on TARGET_OFFLINE ──────────────────────────────


class _ScriptedBus:
    """Bus that runs through a queue of scripted ``request`` outcomes.

    Each outcome is either a ``BaseException`` to raise or a payload
    to return as a ``task.complete`` reply.  Lets us simulate "broker
    rejected the first N requests with TARGET_OFFLINE, then accepted
    the (N+1)th".
    """

    def __init__(self, script: list[BaseException | dict[str, Any]]) -> None:
        self._script = list(script)
        self.attempts = 0

    async def request(
        self,
        *,
        to: str,
        type: str,  # noqa: A002
        payload: dict[str, Any],
        timeout: float | None,
    ) -> _FakeNMBMessage:
        self.attempts += 1
        if not self._script:
            raise RuntimeError(f"_ScriptedBus exhausted after {self.attempts} calls")
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeNMBMessage(type=TASK_COMPLETE, payload=outcome)


class TestSpawnReadinessRetry:
    """The fix for the spawn-vs-connect race documented in §3.

    A subprocess takes hundreds of ms to start; the broker rejects
    requests to a not-yet-connected sandbox with ``TARGET_OFFLINE``
    instantly.  ``DelegationManager`` must retry until the sub-agent
    registers, capped by ``spawn_ready_timeout_s``.
    """

    @pytest.mark.asyncio
    async def test_retries_target_offline_until_subagent_connects(self) -> None:
        """First two attempts hit ``TARGET_OFFLINE``; the third succeeds."""
        bus = _ScriptedBus(
            [
                NMBConnectionError("Broker error TARGET_OFFLINE: coding-12345678 not connected"),
                NMBConnectionError("Broker error TARGET_OFFLINE: coding-12345678 not connected"),
                _complete_payload_dict(),
            ]
        )
        spawn = _SpawnRecorder()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(
                spawn_ready_timeout_s=2.0,
                spawn_ready_poll_interval_s=0.0,  # tight loop, no real sleep
            ),
            spawn_callback=spawn,  # type: ignore[arg-type]
        )

        result = await mgr.delegate(_make_task())

        assert bus.attempts == 3, "manager should have retried twice before succeeding"
        assert result.complete.workflow_id == "wf-1"
        # Spawn was not redone on each retry — one process, then the
        # successful delivery, then a clean terminate.
        assert spawn.spawned == [("coding-12345678", "/tmp/ws-wf-1")]
        assert spawn.terminated == ["coding-12345678"]

    @pytest.mark.asyncio
    async def test_persistent_target_offline_fails_with_clear_error(self) -> None:
        """When the sub-agent never registers, fail with a readiness-specific error.

        Crucial for production debugging: a generic
        ``NMBConnectionError`` would look indistinguishable from a
        broker outage; the readiness window failure must call out
        ``spawn_ready_timeout_s`` so operators know to bump it (or
        fix the sub-agent) instead of the broker.
        """
        # The bus rejects every attempt — script never resolves.
        offline = NMBConnectionError("Broker error TARGET_OFFLINE: coding-12345678 not connected")
        bus = _ScriptedBus([offline] * 50)
        spawn = _SpawnRecorder()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(
                spawn_ready_timeout_s=0.05,  # blow through the budget fast
                spawn_ready_poll_interval_s=0.01,
            ),
            spawn_callback=spawn,  # type: ignore[arg-type]
        )

        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_make_task())

        assert "spawn_ready_timeout_s=0.05" in str(excinfo.value)
        assert "never came online" in str(excinfo.value)
        # The terminate ran in the finally block — no orphaned process.
        assert spawn.terminated == ["coding-12345678"]

    @pytest.mark.asyncio
    async def test_non_target_offline_error_propagates_immediately(self) -> None:
        """Real transport failures bypass the retry loop.

        We retry only on ``TARGET_OFFLINE`` (the documented "not yet
        connected" signal); a different broker error — broker
        misconfig, rate-limited, etc. — should fail fast with the
        original message preserved.  Otherwise an operator chasing a
        broker bug would wait the full ``spawn_ready_timeout_s``
        before seeing it.
        """
        bus = _ScriptedBus(
            [NMBConnectionError("Broker error RATE_LIMITED: too many pending requests")]
        )
        spawn = _SpawnRecorder()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(spawn_ready_timeout_s=10.0),
            spawn_callback=spawn,  # type: ignore[arg-type]
        )

        with pytest.raises(DelegationError) as excinfo:
            await mgr.delegate(_make_task())

        assert "RATE_LIMITED" in str(excinfo.value)
        # Only one attempt — no retry on a non-readiness error.
        assert bus.attempts == 1

    @pytest.mark.asyncio
    async def test_first_attempt_success_skips_retry(self) -> None:
        """Happy path: the broker accepts the first delivery.

        Regression guard for the readiness fix: if the manager were
        to redundantly probe before sending the real assign, this
        test would flag it (we'd see two attempts when only one is
        needed).
        """
        bus = _ScriptedBus([_complete_payload_dict()])
        spawn = _SpawnRecorder()
        mgr = DelegationManager(
            bus,  # type: ignore[arg-type]
            DelegationConfig(spawn_ready_timeout_s=10.0),
            spawn_callback=spawn,  # type: ignore[arg-type]
        )

        await mgr.delegate(_make_task())
        assert bus.attempts == 1
