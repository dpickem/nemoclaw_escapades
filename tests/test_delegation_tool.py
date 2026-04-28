"""Tests for the orchestrator-side ``delegate_task`` tool.

The tool wraps :class:`DelegationManager`; tests stub the manager
to record what payload it received and what the tool returned to
the model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.nmb.protocol import (
    TaskAssignPayload,
    TaskCompletePayload,
    TaskErrorPayload,
)
from nemoclaw_escapades.orchestrator.delegation import (
    DelegationError,
    DelegationResult,
)
from nemoclaw_escapades.tools.delegation import register_delegation_tool
from nemoclaw_escapades.tools.registry import ToolRegistry


class _FakeManager:
    """Records the most recent task; returns a scripted result."""

    def __init__(
        self,
        *,
        complete_summary: str = "did the work",
        raise_error: DelegationError | None = None,
    ) -> None:
        self.last_task: TaskAssignPayload | None = None
        self._summary = complete_summary
        self._raise = raise_error

    async def delegate(self, task: TaskAssignPayload) -> DelegationResult:
        self.last_task = task
        if self._raise:
            raise self._raise
        return DelegationResult(
            complete=TaskCompletePayload(
                workflow_id=task.workflow_id,
                summary=self._summary,
            ),
            sub_agent_sandbox_id=task.agent_id,
        )


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

        # Tool returned the sub-agent's summary as a string for the model.
        assert result == "did the work"

        # Manager received a fully-built, validated TaskAssignPayload.
        assert manager.last_task is not None
        task = manager.last_task
        assert task.prompt == "add a /api/health endpoint"
        assert task.parent_sandbox_id == "orchestrator-pinned"
        assert task.workflow_id.startswith("wf-")
        assert task.agent_id.startswith("coding-")
        # Workspace per agent: <root>/agent-<hex>
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
        """``default_max_turns`` / ``default_model`` from registration kick in."""
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
        # is_shallow defaulted to True (from the WorkspaceBaseline model).
        assert manager.last_task.workspace_baseline.is_shallow is True


# ── Error handling ─────────────────────────────────────────────────


class TestDelegateTaskErrorHandling:
    @pytest.mark.asyncio
    async def test_delegation_failure_returns_error_string_not_raises(self) -> None:
        """The model gets a string failure description, not an exception.

        Tools that raise are surfaced as a hard error in the loop;
        delegation failures are recoverable signals the model should
        consider in its plan, so returning a structured-text message
        is the better fit at this layer.  Phase 3b's finalisation
        flow will replace this with a typed structured result.
        """
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
        """Error payload from the sub-agent flows through to the message."""
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
    """Schema + flags surfaced through the registry."""

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
        # Delegation spawns a process; running two in the same loop
        # would defeat the orchestrator-side semaphore.
        assert spec.is_concurrency_safe is False

    def test_is_not_read_only(self, spec: Any) -> None:
        # Delegation sends a task — definitely not read-only.
        assert spec.is_read_only is False

    def test_input_schema_advertises_optional_overrides(self, spec: Any) -> None:
        schema = spec.input_schema
        properties = schema["properties"]
        assert "prompt" in schema["required"]
        assert "max_turns" in properties
        assert "model" in properties
        assert "workspace_baseline" in properties
        # Only prompt is required; the rest are optional.
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
    async def test_successful_delegation_logs_started_then_complete(
        self,
        audit_db: AuditDB,
    ) -> None:
        registry = ToolRegistry()
        manager = _FakeManager(complete_summary="ok done")
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
        assert row["status"] == "complete"
        assert row["completed_at"] is not None
        assert row["requested_model"] == "some-model"
        assert row["requested_max_turns"] == 10
        assert row["model_used"] is None  # _FakeManager doesn't set it
        assert row["summary"] == "ok done"

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
        # The tool works without audit (matches the existing
        # Orchestrator behaviour where audit DB is opt-in).
        registry = ToolRegistry()
        manager = _FakeManager()
        register_delegation_tool(
            registry,
            manager=manager,  # type: ignore[arg-type]
            parent_sandbox_id="orch",
            workspace_root="/ws",
            # No audit kwarg.
        )
        spec = registry.get("delegate_task")
        result = await spec.handler(prompt="task")  # type: ignore[arg-type]
        assert result == "did the work"
