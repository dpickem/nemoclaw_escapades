"""End-to-end delegation integration test (Phase 3a-7).

Drives the full M2b §6.1 message flow through the in-process NMB
harness:

    Orchestrator              NMB Broker             Sub-Agent
        │                         │                      │
        │── delegate_task ───────▶│                      │
        │  (DelegationManager)    │                      │
        │                         │                      │
        │── task.assign ─────────▶│─────────────────────▶│
        │                         │                      │ (validate
        │                         │                      │  payload,
        │                         │                      │  run loop,
        │                         │                      │  build complete)
        │                         │                      │
        │                         │◀── task.complete ────│
        │◀────────────────────────│                      │

The integration harness substitutes the real OpenShell sandbox
spawn with an in-process ``MessageBus`` per "sandbox"; the
sub-agent's :func:`_run_assigned_task` runs against a
:class:`MockBackend` so we don't need a real inference endpoint.

What this test covers (the §15 ``Orchestrator delegation full
flow`` row of the testing plan, minus the finalisation step which
is Phase 3b):

- ``delegate_task`` builds and ships a valid ``TaskAssignPayload``.
- The orchestrator's :class:`DelegationManager` rounds-trips it
  via ``MessageBus.request`` with the right timeout.
- The sub-agent validates the typed payload, runs the loop, and
  replies with ``task.complete``.
- The audit DB ends up with a single ``status="complete"``
  delegation row.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nemoclaw_escapades.agent import __main__ as agent_main
from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import (
    AppConfig,
    DelegationConfig,
    create_coding_agent_config,
)
from nemoclaw_escapades.models.types import (
    InferenceRequest,
    InferenceResponse,
    TokenUsage,
)
from nemoclaw_escapades.nmb.protocol import (
    TASK_ASSIGN,
    TASK_COMPLETE,
    TaskAssignPayload,
    TaskCompletePayload,
    dump,
    load,
)
from nemoclaw_escapades.nmb.testing import IntegrationHarness
from nemoclaw_escapades.orchestrator.delegation import DelegationManager, SpawnedAgent
from nemoclaw_escapades.tools.delegation import register_delegation_tool
from nemoclaw_escapades.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


class _MockBackend(BackendBase):
    """Minimal in-process backend that returns one canned reply.

    Local copy of ``tests/conftest.py``'s ``MockBackend`` — the
    integration test directory does not share ``tests/`` ' s
    ``conftest`` imports under ``mypy``, so we keep a small, typed
    duplicate here instead of plumbing a new shared fixture file.
    """

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(
            content=self.response_text,
            model="mock-model",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=42.0,
            finish_reason="stop",
            raw_response={"mock": True},
        )


# ── Sub-agent shim ─────────────────────────────────────────────────


async def _run_sub_agent_handler(
    sandbox_handle: Any,
    config: AppConfig,
) -> None:
    """Run the sub-agent NMB receive-loop body once.

    Subs in for ``python -m nemoclaw_escapades.agent --nmb`` — the
    integration harness can't easily exec a child process, but the
    receive-loop body in ``agent_main._run_assigned_task`` is
    process-agnostic, so we drive it directly with our harness handle.

    The handle's auto-collector means we wait on
    ``wait_for_message`` instead of iterating ``bus.listen()``
    directly.
    """
    backend = _MockBackend(response_text="Sub-agent finished the task.")
    msg = await sandbox_handle.wait_for_message(TASK_ASSIGN, timeout=10.0)
    task = load(TaskAssignPayload, "task.assign", msg.payload)
    Path(task.workspace_root).expanduser().mkdir(parents=True, exist_ok=True)

    import logging

    complete = await agent_main._run_assigned_task(
        task,
        config=config,
        backend=backend,
        logger=logging.getLogger("test.sub_agent"),
    )
    await sandbox_handle.reply(msg, type=TASK_COMPLETE, payload=dump(complete))


def _make_redirected_spawn(target_sandbox_id: str) -> Any:
    """Spawn callback that returns ``target_sandbox_id`` instead of dialling a fresh one.

    The integration harness already created the sub-agent's bus
    with a known sandbox id; ``register_delegation_tool`` wants to
    invent a fresh ``coding-<hex>`` id at delegation time.  This
    helper lets the test redirect the manager to dial the bus the
    harness actually has on the broker, regardless of what id the
    tool generated.
    """

    async def _spawn(_sandbox_id: str, _workspace_root: str) -> SpawnedAgent:
        async def _terminate() -> None:
            return

        return SpawnedAgent(sandbox_id=target_sandbox_id, terminate=_terminate)

    return _spawn


# ── Orchestrator → sub-agent delegation ────────────────────────────


class TestDelegationEndToEnd:
    async def test_delegate_task_round_trip_through_broker(
        self,
        two_sandbox_harness: IntegrationHarness,
        tmp_path: Path,
    ) -> None:
        """One delegation, success path, audit row populated."""
        orch = two_sandbox_harness["orchestrator"]
        sub = two_sandbox_harness["coding-1"]

        manager = DelegationManager(
            orch.bus,
            DelegationConfig(task_timeout_s=10.0),
            spawn_callback=_make_redirected_spawn(sub.bus.sandbox_id),
        )

        audit = AuditDB(str(tmp_path / "delegation_audit.db"))
        await audit.open()
        try:
            registry = ToolRegistry()
            register_delegation_tool(
                registry,
                manager=manager,
                parent_sandbox_id="orchestrator",
                workspace_root=str(tmp_path / "ws"),
                audit=audit,
            )

            # Run the sub-agent handler concurrently with the
            # orchestrator's delegate_task call.
            sub_agent_task = asyncio.create_task(
                _run_sub_agent_handler(sub, create_coding_agent_config()),
            )

            spec = registry.get("delegate_task")
            assert spec is not None
            result = await spec.handler(prompt="add a /api/health endpoint")
            await sub_agent_task

            # Tool returned the sub-agent's summary verbatim.
            assert result == "Sub-agent finished the task."

            # Audit DB has the full lifecycle for the workflow.
            rows = await audit.query("SELECT * FROM delegations")
            assert len(rows) == 1
            row = rows[0]
            assert row["status"] == "complete"
            assert row["completed_at"] is not None
            assert row["prompt"] == "add a /api/health endpoint"
            assert row["summary"] == "Sub-agent finished the task."
            assert row["rounds_used"] == 1  # MockBackend returns one round
        finally:
            await audit.close()

    async def test_per_task_max_turns_lands_on_assign_payload(
        self,
        two_sandbox_harness: IntegrationHarness,
        tmp_path: Path,
    ) -> None:
        """Caller-side max_turns + model flow all the way through the wire."""
        orch = two_sandbox_harness["orchestrator"]
        sub = two_sandbox_harness["coding-1"]

        captured: dict[str, Any] = {}

        async def _capturing_handler() -> None:
            msg = await sub.wait_for_message(TASK_ASSIGN, timeout=5.0)
            task = load(TaskAssignPayload, "task.assign", msg.payload)
            captured["task"] = task
            complete = TaskCompletePayload(
                workflow_id=task.workflow_id,
                summary="captured",
            )
            await sub.reply(msg, type=TASK_COMPLETE, payload=dump(complete))

        manager = DelegationManager(
            orch.bus,
            DelegationConfig(task_timeout_s=5.0),
            spawn_callback=_make_redirected_spawn(sub.bus.sandbox_id),
        )
        registry = ToolRegistry()
        register_delegation_tool(
            registry,
            manager=manager,
            parent_sandbox_id="orchestrator",
            workspace_root=str(tmp_path / "ws"),
        )

        handler_task = asyncio.create_task(_capturing_handler())
        spec = registry.get("delegate_task")
        assert spec is not None
        await spec.handler(
            prompt="task",
            max_turns=42,
            model="azure/anthropic/claude-haiku-4",
        )
        await handler_task

        task: TaskAssignPayload = captured["task"]
        assert task.max_turns == 42
        assert task.model == "azure/anthropic/claude-haiku-4"


# ── Spawn-vs-connect readiness race ────────────────────────────────


class TestSpawnReadinessRetryEndToEnd:
    """End-to-end coverage of the readiness retry loop.

    The unit tests in ``tests/test_delegation.py::TestSpawnReadinessRetry``
    cover the manager's retry logic against a fake bus.  This test
    drives the same path through a real broker so we know the
    orchestrator + broker + sub-agent compose correctly when the
    sub-agent's NMB connection lags the spawn return.
    """

    async def test_late_connecting_subagent_completes_after_retry(
        self,
        two_sandbox_harness: IntegrationHarness,
        tmp_path: Path,
    ) -> None:
        """Sub-agent connects 300 ms after spawn — orchestrator retries and succeeds.

        Without the retry loop, the broker would reject the very first
        ``task.assign`` with ``TARGET_OFFLINE`` because the sub-agent
        is still mid-handshake.  This test exercises the retry path
        through a real broker: we close the sub-agent's connection
        immediately after the harness brings it up, then re-open it
        on a 300 ms delay, simulating the spawn-vs-handshake race.
        """
        orch = two_sandbox_harness["orchestrator"]
        sub = two_sandbox_harness["coding-1"]

        # Disconnect the sub-agent so the broker no longer knows it.
        # The orchestrator's first ``request`` will hit
        # ``TARGET_OFFLINE``.
        await sub.bus.close()

        # Schedule the sub-agent to come back online ~300 ms later
        # (analogous to a fresh subprocess finishing its NMB
        # handshake), then handle one task.assign.
        async def _delayed_reconnect_and_handle() -> None:
            await asyncio.sleep(0.3)
            await sub.bus.connect()
            await sub.start_collecting()
            await _run_sub_agent_handler(sub, create_coding_agent_config())

        reconnect_task = asyncio.create_task(_delayed_reconnect_and_handle())

        manager = DelegationManager(
            orch.bus,
            DelegationConfig(
                task_timeout_s=10.0,
                spawn_ready_timeout_s=5.0,
                spawn_ready_poll_interval_s=0.05,
            ),
            spawn_callback=_make_redirected_spawn(sub.bus.sandbox_id),
        )
        registry = ToolRegistry()
        register_delegation_tool(
            registry,
            manager=manager,
            parent_sandbox_id="orchestrator",
            workspace_root=str(tmp_path / "ws"),
        )

        spec = registry.get("delegate_task")
        assert spec is not None
        result = await spec.handler(prompt="late-connecting task")

        # The orchestrator retried on ``TARGET_OFFLINE`` until the
        # sub-agent's bus came back, then completed successfully.
        assert result == "Sub-agent finished the task."
        await reconnect_task
