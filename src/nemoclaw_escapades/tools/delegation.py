"""``delegate_task`` — orchestrator-side fire-and-forget tool.

The orchestrator's planning model calls this tool when the user asks
for coding work that benefits from a focused sub-agent.  The tool
spawns the sub-agent, sends ``task.assign``, registers the workflow
with the :class:`WorkflowDispatcher`, and **returns immediately**.

The sub-agent's eventual ``task.complete`` / ``task.error`` /
``audit.flush`` arrive on the dispatcher's event loop and are routed
to the per-workflow handler — the model's chat thread does **not**
block on sub-agent latency (design §7.1, §8.2).  The user sees:

1. An immediate "I've delegated this; results will appear in this
   thread" reply (composed by the orchestrator's main agent loop
   from this tool's return string).
2. A separate Slack message later, posted by the finalisation
   renderer, carrying the synthesised result and action buttons.

The tool is deliberately **orchestrator-only** in M2b: a coding
sub-agent shouldn't be able to delegate further (the depth-1 cap in
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
from nemoclaw_escapades.orchestrator.request_context import current_request_context
from nemoclaw_escapades.orchestrator.workflow import WorkflowContext
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB
    from nemoclaw_escapades.orchestrator.dispatcher import WorkflowDispatcher

logger = get_logger("tools.delegation")

# Logical toolset name used by the registry for grouping
_TOOLSET: str = "delegation"


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
        request_ctx = current_request_context()
        workflow_ctx = WorkflowContext(
            workflow_id=task.workflow_id,
            task=task,
            channel_id=request_ctx.channel_id if request_ctx else None,
            thread_ts=request_ctx.thread_ts if request_ctx else None,
            request_id=request_ctx.request_id if request_ctx else "",
        )
        # Register with the dispatcher BEFORE sending task.assign so a
        # fast sub-agent can't race the registration: the dispatcher
        # would then drop the resulting task.complete as "unknown
        # workflow" (see WorkflowDispatcher._handle_task_complete).
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
            result = await manager.delegate(task, context=workflow_ctx)
        except DelegationError as exc:
            if dispatcher is not None:
                dispatcher.deregister_workflow(task.workflow_id)
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
