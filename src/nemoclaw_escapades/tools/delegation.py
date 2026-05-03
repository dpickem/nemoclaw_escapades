"""``delegate_task`` — orchestrator-side tool for spawning a sub-agent.

The orchestrator's planning model calls this tool when the user asks
for coding work that benefits from a focused sub-agent (file edits,
test runs, multi-step refactors).  The tool is a thin wrapper around
:class:`DelegationManager`: it builds a ``TaskAssignPayload`` from
the model's arguments, runs the manager's
:meth:`DelegationManager.delegate`, and returns the sub-agent's
``summary`` (or a structured error) for the model to present.

In Phase 3a the tool's return value is a plain string — Phase 3b's
finalisation flow will wrap it in a structured result so the
finalisation model can branch on success vs ``recoverable`` failure.

The tool is deliberately **orchestrator-only** in M2b: a coding sub-
agent shouldn't be able to delegate further (the depth-1 cap in
:class:`DelegationConfig` enforces this).  Don't register
``delegate_task`` in ``create_coding_tool_registry``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from nemoclaw_escapades.nmb.protocol import (
    TaskAssignPayload,
    WorkspaceBaseline,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.orchestrator.delegation import DelegationError, DelegationManager
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB

logger = get_logger("tools.delegation")

_TOOLSET: str = "delegation"


def _make_delegate_task(
    manager: DelegationManager,
    *,
    parent_sandbox_id: str,
    workspace_root: str,
    default_max_turns: int | None = None,
    default_model: str | None = None,
    audit: AuditDB | None = None,
) -> ToolSpec:
    """Bind a ``delegate_task`` tool spec to a manager + workspace.

    Args:
        manager: The orchestrator's :class:`DelegationManager`
            instance.  Tests can pass a stub manager that records
            invocations without spawning real processes.
        parent_sandbox_id: Orchestrator's NMB sandbox identity.
            Stamped onto every spawned ``TaskAssignPayload`` so
            audit records can trace the delegation tree (and so
            future depth checks have something to inspect).
        workspace_root: Absolute path the sub-agent's workspace
            subdirectories will land under.  Each delegation gets
            its own ``<workspace_root>/agent-<id>`` subdir
            (matching the §4.2.1 isolation invariant).  This is
            registration-time runtime context, not a model argument;
            a future nested agent would register its own tool with
            its own parent identity and workspace root.
        default_max_turns: Per-shape default for ``max_turns`` (the
            §17 Q4 lookup table).  ``None`` means "inherit the
            sub-agent's config default."
        default_model: Per-shape default for ``model``.  Operationally
            a no-op in M2b's same-sandbox topology (§6.5.1) but
            recorded in the audit trail for M3 to pick up.

    Returns:
        A :class:`ToolSpec` ready to register.
    """

    @tool(
        "delegate_task",
        "Delegate a coding task to a sub-agent. Use for multi-step coding "
        "work, file edits, or test runs that benefit from a focused "
        "sandbox. Returns the sub-agent's summary on completion.",
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Natural-language description of the work the sub-agent should do."
                    ),
                },
                "max_turns": {
                    "type": "integer",
                    "description": (
                        "Optional cap on the sub-agent's tool-round budget for this "
                        "task.  Pick ~3 for one-shot lookups, ~15 for routine bug "
                        "fixes, ~30 for multi-file refactors.  Leave unset for the "
                        "default."
                    ),
                    "minimum": 1,
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model override for this delegation.  Operationally "
                        "a no-op in M2b but recorded in the audit DB."
                    ),
                },
                "workspace_baseline": {
                    "type": "object",
                    "description": (
                        "Optional git baseline the diff in task.complete should be "
                        "computed against.  Pin this when the orchestrator already "
                        "knows the repo + branch + base SHA the workflow targets."
                    ),
                    "properties": {
                        "repo_url": {"type": "string"},
                        "branch": {"type": "string"},
                        "base_sha": {"type": "string"},
                        "is_shallow": {"type": "boolean", "default": True},
                    },
                    "required": ["repo_url", "branch", "base_sha"],
                },
            },
            "required": ["prompt"],
        },
        display_name="Delegating to sub-agent",
        toolset=_TOOLSET,
        is_concurrency_safe=False,  # spawns a process; serialise per-call
        is_read_only=False,
    )
    async def delegate_task(
        prompt: str,
        max_turns: int | None = None,
        model: str | None = None,
        workspace_baseline: dict[str, object] | None = None,
    ) -> str:
        agent_id = f"coding-{uuid.uuid4().hex[:8]}"
        baseline = (
            WorkspaceBaseline.model_validate(workspace_baseline)
            if workspace_baseline is not None
            else None
        )
        task = TaskAssignPayload(
            prompt=prompt,
            workflow_id=f"wf-{uuid.uuid4().hex[:12]}",
            parent_sandbox_id=parent_sandbox_id,
            agent_id=agent_id,
            workspace_root=f"{workspace_root}/agent-{agent_id[len('coding-') :]}",
            max_turns=max_turns or default_max_turns,
            model=model or default_model,
            workspace_baseline=baseline,
        )
        logger.info(
            "Delegating task",
            extra={
                "workflow_id": task.workflow_id,
                "agent_id": task.agent_id,
                "max_turns": task.max_turns,
                "model": task.model,
            },
        )
        if audit is not None:
            await audit.log_delegation_started(
                workflow_id=task.workflow_id,
                parent_sandbox_id=task.parent_sandbox_id,
                agent_id=task.agent_id,
                workspace_root=task.workspace_root,
                prompt=task.prompt,
                requested_model=task.model,
                requested_max_turns=task.max_turns,
                base_sha=baseline.base_sha if baseline else None,
                base_repo_url=baseline.repo_url if baseline else None,
                base_branch=baseline.branch if baseline else None,
            )
        try:
            result = await manager.delegate(task)
        except DelegationError as exc:
            logger.error(
                "Delegation failed",
                extra={"workflow_id": task.workflow_id, "error": str(exc)},
            )
            if audit is not None:
                payload = exc.error_payload
                await audit.log_delegation_error(
                    workflow_id=task.workflow_id,
                    error_kind=payload.error_kind if payload else "other",
                    error_message=payload.error if payload else str(exc),
                    recoverable=payload.recoverable if payload else False,
                )
            return f"Delegation failed: {exc}"
        if audit is not None:
            await audit.log_delegation_complete(
                workflow_id=task.workflow_id,
                rounds_used=result.complete.rounds_used,
                tool_calls_made=result.complete.tool_calls_made,
                model_used=result.complete.model_used,
                summary=result.complete.summary,
                diff_size=len(result.complete.diff.encode()),
            )
        return result.complete.summary

    return delegate_task


def register_delegation_tool(
    registry: ToolRegistry,
    *,
    manager: DelegationManager,
    parent_sandbox_id: str,
    workspace_root: str,
    default_max_turns: int | None = None,
    default_model: str | None = None,
    audit: AuditDB | None = None,
) -> None:
    """Register the orchestrator's ``delegate_task`` tool.

    Call this from ``build_full_tool_registry`` (or directly from
    the orchestrator's startup code in ``main.py``) once the
    :class:`DelegationManager` is constructed.

    Args:
        registry: Tool registry to mutate.
        manager: Constructed :class:`DelegationManager`.
        parent_sandbox_id: Orchestrator's NMB sandbox identity.
        workspace_root: Base path for sub-agent workspaces.
        default_max_turns: Optional per-shape default.
        default_model: Optional per-shape default.
        audit: Optional :class:`AuditDB`.  When supplied, every
            delegation goes through ``log_delegation_started`` /
            ``log_delegation_complete`` / ``log_delegation_error``
            so the workflow trail is queryable post-hoc.
    """
    registry.register(
        _make_delegate_task(
            manager,
            parent_sandbox_id=parent_sandbox_id,
            workspace_root=workspace_root,
            default_max_turns=default_max_turns,
            default_model=default_model,
            audit=audit,
        ),
    )
