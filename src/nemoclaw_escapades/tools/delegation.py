"""``delegate_task`` — orchestrator-side fire-and-forget tool.

The orchestrator model calls this when coding work should run in a focused
sub-agent.  The tool registers workflow context, sends ``task.assign``, and
returns immediately; later ``task.complete`` / ``task.error`` arrivals are
handled by the dispatcher.

This tool is orchestrator-only in M2b.  Coding sub-agents do not receive it, and
the delegation manager enforces the one-level spawn shape until M3.
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
from nemoclaw_escapades.orchestrator.request_context import current_request_context
from nemoclaw_escapades.orchestrator.workflow import WorkflowContext
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB
    from nemoclaw_escapades.orchestrator.dispatcher import WorkflowDispatcher

logger = get_logger("tools.delegation")

# Logical toolset name used by the registry for grouping.
_TOOLSET: str = "delegation"

# Hex characters appended to generated coding-agent ids.
_AGENT_ID_HEX_CHARS: int = 8

# Hex characters appended to generated workflow ids.
_WORKFLOW_ID_HEX_CHARS: int = 12

# Minimum accepted max_turns value in the tool schema.
_MIN_MAX_TURNS: int = 1

# Suggested max_turns for a one-shot lookup.
_BUDGET_HINT_LOOKUP_TURNS: int = 3

# Suggested max_turns for a routine bug fix.
_BUDGET_HINT_BUGFIX_TURNS: int = 15

# Suggested max_turns for a multi-file refactor.
_BUDGET_HINT_REFACTOR_TURNS: int = 30

# Prefix used in generated coding-agent ids.
_CODING_AGENT_PREFIX: str = "coding-"


def _make_delegate_task(
    manager: DelegationManager,
    *,
    parent_sandbox_id: str,
    workspace_root: str,
    dispatcher: WorkflowDispatcher | None = None,
    default_max_turns: int | None = None,
    default_model: str | None = None,
    audit: AuditDB | None = None,
) -> ToolSpec:
    """Bind a ``delegate_task`` tool spec to a manager + workspace.

    The returned tool validates optional baseline input, registers workflow
    context with the dispatcher, records audit start/error rows, and delegates
    the actual send to :class:`DelegationManager`.

    Args:
        manager: The orchestrator's :class:`DelegationManager`.
        parent_sandbox_id: Orchestrator's NMB sandbox identity.
        workspace_root: Absolute path the sub-agent's per-task
            subdirectories will land under.
        dispatcher: Workflow dispatcher to register the new workflow
            with.  Required for the dispatcher-driven finalisation
            path; ``None`` runs in legacy "no finalisation" mode
            (used by tests that exercise the manager directly).
        default_max_turns: Per-shape default for ``max_turns``.
        default_model: Per-shape default for ``model``.
        audit: Optional :class:`AuditDB` for ``log_delegation_*``
            writes.

    Returns:
        A :class:`ToolSpec` ready to register.
    """

    @tool(
        "delegate_task",
        "Delegate a coding task to a sub-agent. Use for multi-step coding "
        "work, file edits, or test runs that benefit from a focused "
        "sandbox. Returns immediately; the sub-agent's work appears as a "
        "separate message in the thread when ready.",
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
                        f"task. Pick ~{_BUDGET_HINT_LOOKUP_TURNS} for one-shot "
                        f"lookups, ~{_BUDGET_HINT_BUGFIX_TURNS} for routine bug "
                        f"fixes, ~{_BUDGET_HINT_REFACTOR_TURNS} for multi-file "
                        "refactors. Leave unset for the default."
                    ),
                    "minimum": _MIN_MAX_TURNS,
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
        """Create a workflow, send it to a coding sub-agent, and return an ack."""
        agent_id = f"{_CODING_AGENT_PREFIX}{uuid.uuid4().hex[:_AGENT_ID_HEX_CHARS]}"
        baseline = (
            WorkspaceBaseline.model_validate(workspace_baseline)
            if workspace_baseline is not None
            else None
        )
        task = TaskAssignPayload(
            prompt=prompt,
            workflow_id=f"wf-{uuid.uuid4().hex[:_WORKFLOW_ID_HEX_CHARS]}",
            parent_sandbox_id=parent_sandbox_id,
            agent_id=agent_id,
            workspace_root=f"{workspace_root}/agent-{agent_id[len(_CODING_AGENT_PREFIX) :]}",
            max_turns=max_turns or default_max_turns,
            model=model or default_model,
            workspace_baseline=baseline,
        )
        request_ctx = current_request_context()
        workflow_ctx = WorkflowContext(
            workflow_id=task.workflow_id,
            task=task,
            channel_id=request_ctx.channel_id if request_ctx else None,
            thread_ts=request_ctx.thread_ts if request_ctx else None,
            request_id=request_ctx.request_id if request_ctx else "",
        )
        # Register before send so a fast task.complete cannot race setup.
        if dispatcher is not None:
            dispatcher.register_workflow(workflow_ctx)

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
            if dispatcher is not None:
                # Cleans dispatcher state; manager cleanup is idempotent.
                await dispatcher.deregister_workflow(task.workflow_id)

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

        return (
            f"Delegated. Workflow {result.workflow_id} is running in sub-agent "
            f"{result.sub_agent_sandbox_id}; I'll post the results in this thread "
            "when it finishes."
        )

    return delegate_task


def register_delegation_tool(
    registry: ToolRegistry,
    *,
    manager: DelegationManager,
    parent_sandbox_id: str,
    workspace_root: str,
    dispatcher: WorkflowDispatcher | None = None,
    default_max_turns: int | None = None,
    default_model: str | None = None,
    audit: AuditDB | None = None,
) -> None:
    """Register the orchestrator's ``delegate_task`` tool.

    Call this from ``build_full_tool_registry`` (or directly from
    the orchestrator's startup code in ``main.py``) once the
    :class:`DelegationManager`, :class:`WorkflowDispatcher`, and
    audit DB are constructed.

    Args:
        registry: Tool registry to mutate.
        manager: Constructed :class:`DelegationManager`.
        parent_sandbox_id: Orchestrator's NMB sandbox identity.
        workspace_root: Base path for sub-agent workspaces.
        dispatcher: Workflow dispatcher; required for the
            dispatcher-driven finalisation flow.
        default_max_turns: Optional per-shape default.
        default_model: Optional per-shape default.
        audit: Optional :class:`AuditDB` for ``log_delegation_*``.
    """
    registry.register(
        _make_delegate_task(
            manager,
            parent_sandbox_id=parent_sandbox_id,
            workspace_root=workspace_root,
            dispatcher=dispatcher,
            default_max_turns=default_max_turns,
            default_model=default_model,
            audit=audit,
        ),
    )
