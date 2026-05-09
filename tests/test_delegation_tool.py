"""Tests for the orchestrator-side ``delegate_task`` tool.

The tool wraps :class:`DelegationManager`; tests stub the manager so
they don't actually spawn a sub-agent process or talk to NMB.  The
manager's contract under Phase 3b is fire-and-forget: ``delegate``
sends ``task.assign`` and returns immediately with a
:class:`DelegationResult` carrying the workflow id.

The ``log_delegation_complete`` audit row lands when the dispatcher
receives ``task.complete``; that flow is tested in
``test_finalization.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.nmb.protocol import (
    TaskAssignPayload,
    TaskErrorPayload,
)
from nemoclaw_escapades.orchestrator.delegation import (
    DelegationError,
    DelegationResult,
)
from nemoclaw_escapades.orchestrator.workflow import WorkflowContext
from nemoclaw_escapades.tools.delegation import register_delegation_tool
from nemoclaw_escapades.tools.registry import ToolRegistry


class _FakeManager:
    """Records the most recent task; returns the workflow ack or raises."""

    def __init__(
        self,
        *,
        raise_error: DelegationError | None = None,
    ) -> None:
        self.last_task: TaskAssignPayload | None = None
        self.last_context: WorkflowContext | None = None
        self._raise = raise_error

    async def delegate(
        self,
        task: TaskAssignPayload,
        *,
        context: WorkflowContext | None = None,
    ) -> DelegationResult:
        self.last_task = task
        self.last_context = context
        if self._raise:
            raise self._raise
        return DelegationResult(
            workflow_id=task.workflow_id,
            sub_agent_sandbox_id=task.agent_id,
        )


class _FakeDispatcher:
    """Records workflow registrations the tool performs."""

    def __init__(self) -> None:
        self.registered: list[WorkflowContext] = []
        self.deregistered: list[str] = []

    def register_workflow(self, ctx: WorkflowContext) -> None:
        self.registered.append(ctx)

    async def deregister_workflow(self, workflow_id: str) -> None:
        self.deregistered.append(workflow_id)


# ── Happy path ─────────────────────────────────────────────────────


class TestDelegateTaskHappyPath:
    @pytest.mark.asyncio
    async def test_minimal_prompt_builds_valid_assign_payload(self) -> None:
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orchestrator-pinned",
            workspace_root="/tmp/work",
        )
        spec = registry.get("delegate_task")
        result = await spec.handler(prompt="add a /api/health endpoint")  # type: ignore[arg-type]

        # Tool returns an immediate "Delegated …" ack to the model.
        assert "Delegated" in result
        assert manager.last_task is not None
        assert manager.last_task.workflow_id in result

        task = manager.last_task
        assert task.prompt == "add a /api/health endpoint"
        assert task.parent_sandbox_id == "orchestrator-pinned"
        assert task.workflow_id.startswith("wf-")
        assert task.agent_id.startswith("coding-")
        assert task.workspace_root.startswith("/tmp/work/agent-")

    @pytest.mark.asyncio
    async def test_max_turns_and_model_passed_through(self) -> None:
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
        )
        spec = registry.get("delegate_task")
        await spec.handler(  # type: ignore[arg-type]
            prompt="task",
            max_turns=20,
            model="azure/anthropic/claude-haiku-4",
        )
        assert manager.last_task is not None
        assert manager.last_task.max_turns == 20
        assert manager.last_task.model == "azure/anthropic/claude-haiku-4"

    @pytest.mark.asyncio
    async def test_per_shape_defaults_apply_when_args_missing(self) -> None:
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
            default_max_turns=15,
            default_model="azure/anthropic/claude-opus-4",
        )
        spec = registry.get("delegate_task")
        await spec.handler(prompt="task")  # type: ignore[arg-type]
        assert manager.last_task is not None
        assert manager.last_task.max_turns == 15
        assert manager.last_task.model == "azure/anthropic/claude-opus-4"

    @pytest.mark.asyncio
    async def test_explicit_args_override_per_shape_defaults(self) -> None:
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
            default_max_turns=15,
        )
        spec = registry.get("delegate_task")
        await spec.handler(prompt="task", max_turns=42)  # type: ignore[arg-type]
        assert manager.last_task is not None
        assert manager.last_task.max_turns == 42

    @pytest.mark.asyncio
    async def test_workspace_baseline_validated_into_pydantic(self) -> None:
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
        )
        spec = registry.get("delegate_task")
        await spec.handler(  # type: ignore[arg-type]
            prompt="task",
            workspace_baseline={
                "repo_url": "https://example.com/x.git",
                "branch": "main",
                "base_sha": "abcdef0123456789abcdef0123456789abcdef01",
            },
        )
        assert manager.last_task is not None
        assert manager.last_task.workspace_baseline is not None
        assert manager.last_task.workspace_baseline.branch == "main"
        assert manager.last_task.workspace_baseline.is_shallow is True


# ── Dispatcher integration ─────────────────────────────────────────


class TestDispatcherRegistration:
    @pytest.mark.asyncio
    async def test_workflow_registered_before_send(self) -> None:
        """Workflow registration must happen *before* ``manager.delegate``.

        Otherwise a fast sub-agent's ``task.complete`` could land on
        the dispatcher before the workflow is known and get dropped
        as "unknown workflow".
        """
        order: list[str] = []
        dispatcher = _FakeDispatcher()

        # Override the manager.delegate to record ordering.
        class _OrderManager(_FakeManager):
            async def delegate(self, task: TaskAssignPayload, *, context: Any = None) -> Any:
                order.append("delegate")
                return await super().delegate(task, context=context)

        # Wrap dispatcher.register so we observe the order.
        original_register = dispatcher.register_workflow

        def _register_recording(ctx: WorkflowContext) -> None:
            order.append("register")
            original_register(ctx)

        dispatcher.register_workflow = _register_recording  # type: ignore[method-assign]

        registry = ToolRegistry()
        register_delegation_tool(
            registry,
            manager=_OrderManager(),  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
            dispatcher=dispatcher,  # type: ignore[arg-type]
        )
        spec = registry.get("delegate_task")
        await spec.handler(prompt="task")  # type: ignore[arg-type]
        assert order == ["register", "delegate"]
        assert len(dispatcher.registered) == 1

    @pytest.mark.asyncio
    async def test_failed_delegation_deregisters_workflow(self) -> None:
        """A spawn / send failure must clean up the dispatcher registration."""
        dispatcher = _FakeDispatcher()
        registry = ToolRegistry()
        manager = _FakeManager(raise_error=DelegationError("broker offline"))
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
            dispatcher=dispatcher,  # type: ignore[arg-type]
        )
        spec = registry.get("delegate_task")
        await spec.handler(prompt="task")  # type: ignore[arg-type]
        assert len(dispatcher.registered) == 1
        # The dispatcher should have been told to drop the registration.
        assert len(dispatcher.deregistered) == 1
        assert dispatcher.deregistered[0] == dispatcher.registered[0].workflow_id


# ── Error handling ─────────────────────────────────────────────────


class TestDelegateTaskErrorHandling:
    @pytest.mark.asyncio
    async def test_delegation_failure_returns_error_string_not_raises(self) -> None:
        registry = ToolRegistry()
        manager = _FakeManager(raise_error=DelegationError("broker offline"))
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
        )
        spec = registry.get("delegate_task")
        result = await spec.handler(prompt="task")  # type: ignore[arg-type]
        assert "broker offline" in result
        assert "Delegation failed" in result

    @pytest.mark.asyncio
    async def test_delegation_failure_with_typed_error_payload(self) -> None:
        error_payload = TaskErrorPayload(
            workflow_id="wf-1",
            error="Tool round limit exceeded",
            error_kind="max_turns_exceeded",
            recoverable=True,
        )
        manager = _FakeManager(
            raise_error=DelegationError(
                "sub-agent returned task.error: Tool round limit exceeded",
                error_payload=error_payload,
            ),
        )
        registry = ToolRegistry()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
        )
        spec = registry.get("delegate_task")
        result = await spec.handler(prompt="task")  # type: ignore[arg-type]
        assert "Tool round limit exceeded" in result


# ── Tool registration metadata ─────────────────────────────────────


class TestDelegateTaskSpec:
    @pytest.fixture
    def spec(self) -> Any:
        registry = ToolRegistry()
        register_delegation_tool(
            registry,
            manager=_FakeManager(),  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
        )
        return registry.get("delegate_task")

    def test_is_not_concurrency_safe(self, spec: Any) -> None:
        assert spec.is_concurrency_safe is False

    def test_is_not_read_only(self, spec: Any) -> None:
        assert spec.is_read_only is False

    def test_input_schema_advertises_optional_overrides(self, spec: Any) -> None:
        schema = spec.input_schema
        properties = schema["properties"]
        assert "prompt" in schema["required"]
        assert "max_turns" in properties
        assert "model" in properties
        assert "workspace_baseline" in properties
        assert schema["required"] == ["prompt"]


# ── Audit DB integration ───────────────────────────────────────────


@pytest.fixture
async def audit_db(tmp_path: Path) -> AuditDB:
    db = AuditDB(str(tmp_path / "test_delegation_audit.db"))
    await db.open()
    yield db  # type: ignore[misc]
    await db.close()


class TestDelegationAuditTrail:
    @pytest.mark.asyncio
    async def test_successful_delegation_logs_started_row(
        self,
        audit_db: AuditDB,
    ) -> None:
        """Tool logs ``log_delegation_started``; ``complete`` happens later in the dispatcher."""
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
            audit=audit_db,
        )
        spec = registry.get("delegate_task")
        await spec.handler(  # type: ignore[arg-type]
            prompt="task",
            max_turns=10,
            model="some-model",
        )
        rows = await audit_db.query("SELECT * FROM delegations")
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "started"
        assert row["completed_at"] is None
        assert row["requested_model"] == "some-model"
        assert row["requested_max_turns"] == 10

    @pytest.mark.asyncio
    async def test_failed_delegation_logs_started_then_error(
        self,
        audit_db: AuditDB,
    ) -> None:
        registry = ToolRegistry()
        error = DelegationError(
            "sub-agent failed",
            error_payload=TaskErrorPayload(
                workflow_id="ignored-overwritten-by-row",
                error="ran out of rounds",
                error_kind="max_turns_exceeded",
                recoverable=True,
            ),
        )
        manager = _FakeManager(raise_error=error)
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
            audit=audit_db,
        )
        spec = registry.get("delegate_task")
        await spec.handler(prompt="task")  # type: ignore[arg-type]
        rows = await audit_db.query("SELECT * FROM delegations")
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "error"
        assert row["error_kind"] == "max_turns_exceeded"
        assert row["error_message"] == "ran out of rounds"
        assert row["recoverable"] == 1

    @pytest.mark.asyncio
    async def test_audit_argument_is_optional(self) -> None:
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
        )
        spec = registry.get("delegate_task")
        result = await spec.handler(prompt="task")  # type: ignore[arg-type]
        assert "Delegated" in result
