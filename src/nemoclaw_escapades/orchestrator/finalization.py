"""Model-driven finalization for completed delegated work.

After each ``task.complete``, the dispatcher runs a second
:class:`AgentLoop` with a small finalization tool registry.  That
model decides whether to present work, push a branch/PR, iterate,
discard work, or tear down the sandbox.

- ``present_work_to_user`` — render the work + action buttons to the
  originating thread (default path).
- ``push_branch`` / ``push_and_create_pr`` — git ops.
- ``re_delegate`` — fire a follow-up assignment with iteration feedback.
- ``discard_work`` — wipe the workspace.
- ``destroy_sandbox`` — explicit teardown (no-op in M2b).

The dispatcher invokes :meth:`FinalizationCoordinator.finalize` as
an independent ``asyncio.Task`` so concurrent finalisations run in
parallel and the user-facing chat thread never blocks on sub-agent
work (§8.2).

Baseline drift detection runs before the finalization model so the orchestrator
never performs git side effects against the wrong base.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nemoclaw_escapades.agent.git_helpers import GitDiffError, diff_against_baseline
from nemoclaw_escapades.agent.loop import AgentLoop
from nemoclaw_escapades.config import AgentLoopConfig
from nemoclaw_escapades.models.types import MessageRole
from nemoclaw_escapades.nmb.protocol import (
    TaskAssignPayload,
    TaskCompletePayload,
    WorkspaceBaseline,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.finalization import (
    FinalizationAction,
    FinalizationSession,
    create_finalization_tool_registry,
)

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB
    from nemoclaw_escapades.backends.base import BackendBase
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager
    from nemoclaw_escapades.orchestrator.workflow import WorkflowContext, WorkflowRenderer

logger = get_logger("orchestrator.finalization")

# ── Constants ──────────────────────────────────────────────────────

# Maximum notes-file characters fed into the finalisation prompt
# before truncation.
_NOTES_TRUNCATION_CHARS: int = 20_000

# Prefix added when notes content is truncated.
_TRUNCATED_NOTES_PREFIX: str = "\n... (truncated, "

# Suffix added when notes content is truncated.
_TRUNCATED_NOTES_SUFFIX: str = " chars omitted)"

# Default location of the finalisation system prompt (relative to the
# repository root.
_FINALIZATION_PROMPT_FILE: str = "prompts/finalization_agent.md"


class BaselineDriftError(RuntimeError):
    """Raised when a sub-agent reports a different baseline than assigned.

    Finalization aborts before any git side effects when this is raised.
    """


@dataclass
class FinalizationResult:
    """Outcome of one finalisation run.

    The dispatcher uses ``is_terminal`` to decide whether to deregister the
    workflow after the selected tool completes.
    """

    # Finalized workflow id.
    workflow_id: str
    # Finalization tool/action selected by the model.
    action: FinalizationAction
    # User-facing text returned by the tool or model.
    message: str
    # Whether the workflow should be deregistered after this action.
    is_terminal: bool = True


def verify_baseline(task: TaskAssignPayload, complete: TaskCompletePayload) -> None:
    """Verify that the completion echoes the assigned workspace baseline.

    Both baselines may be ``None`` for non-diff-producing tasks.  Otherwise
    they must match structurally.

    Raises:
        BaselineDriftError: When the baselines disagree.
    """
    expected = task.workspace_baseline
    actual = complete.workspace_baseline
    if expected is None and actual is None:
        return
    if expected is None or actual is None or _baseline_key(expected) != _baseline_key(actual):
        raise BaselineDriftError(
            "workspace baseline drift: "
            f"assigned={expected.model_dump() if expected else None} "
            f"completed={actual.model_dump() if actual else None}"
        )


def _baseline_key(baseline: WorkspaceBaseline) -> tuple[str, str, str, bool]:
    """Return the fields that must echo-match across assign/complete."""
    return (baseline.repo_url, baseline.branch, baseline.base_sha, baseline.is_shallow)


def build_finalization_prompt(task: TaskAssignPayload, complete: TaskCompletePayload) -> str:
    """Render the typed completion payload as the finalisation user prompt.

    This is mechanical formatting.  The model receives already-typed fields,
    not a free-form sub-agent transcript to parse.
    """
    notes = _read_notes(task.workspace_root, complete.notes_path)
    return "\n".join(
        [
            "A coding sub-agent completed a delegated task.",
            "",
            f"Workflow: {task.workflow_id}",
            f"Agent: {task.agent_id}",
            f"Original prompt: {task.prompt}",
            f"Summary: {complete.summary}",
            f"Rounds used: {complete.rounds_used}",
            f"Tool calls made: {complete.tool_calls_made}",
            f"Model used: {complete.model_used or '(unknown)'}",
            f"Suggested next step: {complete.suggested_next_step or '(none)'}",
            "",
            "Files changed:",
            "\n".join(f"- {path}" for path in complete.files_changed) or "(not provided)",
            "",
            "Notes:",
            notes or "(none)",
            "",
            "Diff:",
            complete.diff or "(empty)",
            "",
            "Choose exactly one finalization tool. Prefer present_work_to_user unless "
            "the result is clearly incomplete and needs re_delegate, or unsafe "
            "and needs discard_work.",
        ]
    )


def _read_notes(
    workspace_root: str,
    notes_path: str | None,
    limit: int = _NOTES_TRUNCATION_CHARS,
) -> str:
    """Read a notes file under the workspace root, truncating if needed."""
    if not notes_path:
        return ""

    root = Path(workspace_root).expanduser().resolve()
    path = (root / notes_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "(notes path escaped workspace)"

    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")

    if len(text) > limit:
        omitted = len(text) - limit
        return text[:limit] + f"{_TRUNCATED_NOTES_PREFIX}{omitted}{_TRUNCATED_NOTES_SUFFIX}"

    return text


class FinalizationCoordinator:
    """Per-orchestrator finalisation driver.

    The dispatcher calls :meth:`finalize` once per ``task.complete`` arrival.
    Verification and the second :class:`AgentLoop` live here so dispatcher
    handlers stay small.
    """

    def __init__(
        self,
        *,
        backend: BackendBase,
        config: AgentLoopConfig,
        delegation_manager: DelegationManager | None = None,
        audit: AuditDB | None = None,
        renderer: WorkflowRenderer | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Wire the coordinator to its collaborators.

        Args:
            backend: Inference provider for the finalization loop.
            config: Runtime config for the finalization loop.
            delegation_manager: Enables ``re_delegate``.
            audit: Central audit DB, if configured.
            renderer: Connector-side push surface.
            system_prompt: Optional prompt override.
        """
        # Inference backend used by the finalization AgentLoop.
        self._backend = backend
        # AgentLoop runtime config for the finalization model.
        self._config = config
        # Optional manager used by finalization tools for re-delegation.
        self._delegation_manager = delegation_manager
        # Optional audit DB for tool calls and completion rows.
        self._audit = audit
        # Optional connector renderer for user-facing finalization actions.
        self._renderer = renderer
        # System prompt used by the finalization AgentLoop.
        self._system_prompt = system_prompt or _load_system_prompt()

    async def finalize(
        self,
        context: WorkflowContext,
        complete: TaskCompletePayload,
    ) -> FinalizationResult:
        """Run finalisation for one ``task.complete`` arrival.

        Performs baseline checks, completion audit, and one finalization
        ``AgentLoop`` run.

        Args:
            context: Registered workflow context.
            complete: Validated completion payload.

        Returns:
            The action selected by the finalization model.

        Raises:
            BaselineDriftError: When echoed baseline disagrees with assignment.
        """
        verify_baseline(context.task, complete)
        await self._verify_diff(context.task, complete)
        await self._record_completion(context, complete)

        session = FinalizationSession(
            task=context.task,
            complete=complete,
            context=context,
            delegation_manager=self._delegation_manager,
            renderer=self._renderer,
        )
        tools = create_finalization_tool_registry(session)
        loop = AgentLoop(
            backend=self._backend,
            tools=tools,
            config=self._config,
            audit=self._audit,
        )
        messages = [
            {"role": MessageRole.SYSTEM, "content": self._system_prompt},
            {
                "role": MessageRole.USER,
                "content": build_finalization_prompt(context.task, complete),
            },
        ]
        result = await loop.run(messages, request_id=f"finalize-{context.workflow_id}")
        action = session.state.action or FinalizationAction.MODEL_RESPONSE
        message = session.state.message or result.content

        # No tool call means there is no follow-up button/action to wait on.
        is_terminal = session.state.is_terminal if session.state.action else True
        return FinalizationResult(
            workflow_id=context.workflow_id,
            action=action,
            message=message,
            is_terminal=is_terminal,
        )

    async def finalize_to_text(
        self,
        task: TaskAssignPayload,
        complete: TaskCompletePayload,
    ) -> str:
        """Headless finalisation entry point (used by tests).

        Builds an ad-hoc :class:`WorkflowContext` so tests can run without a
        connector or dispatcher.
        """
        from nemoclaw_escapades.orchestrator.workflow import WorkflowContext as _Ctx

        ctx = _Ctx(workflow_id=task.workflow_id, task=task)
        result = await self.finalize(ctx, complete)
        return result.message

    async def _record_completion(
        self,
        context: WorkflowContext,
        complete: TaskCompletePayload,
    ) -> None:
        """Update the delegation audit row with the typed completion result.

        Best-effort: audit failures are logged but do not abort finalization.
        """
        if self._audit is None:
            return

        try:
            await self._audit.log_delegation_complete(
                workflow_id=context.workflow_id,
                rounds_used=complete.rounds_used,
                tool_calls_made=complete.tool_calls_made,
                model_used=complete.model_used,
                summary=complete.summary,
                diff_size=len(complete.diff.encode()),
            )
        except Exception:  # noqa: BLE001 — DB surface is broad
            logger.warning(
                "log_delegation_complete failed",
                extra={"workflow_id": context.workflow_id},
                exc_info=True,
            )

    async def _verify_diff(
        self,
        task: TaskAssignPayload,
        complete: TaskCompletePayload,
    ) -> None:
        """Re-derive the workspace diff and warn if it disagrees.

        Best-effort by design.  The echoed baseline check is the load-bearing
        safety gate; this catches mismatched reported diffs for diagnostics.
        """
        if task.workspace_baseline is None:
            return

        try:
            local_diff = await diff_against_baseline(
                task.workspace_root,
                task.workspace_baseline.base_sha,
            )
        except GitDiffError:
            logger.warning(
                "Diff re-derivation failed; skipping §6.6.3 cross-check",
                extra={
                    "workflow_id": task.workflow_id,
                    "base_sha": task.workspace_baseline.base_sha,
                },
                exc_info=True,
            )
            return
        except Exception:  # noqa: BLE001 — broad git surface
            logger.warning(
                "Unexpected error during diff re-derivation",
                extra={"workflow_id": task.workflow_id},
                exc_info=True,
            )
            return

        if local_diff.strip() != complete.diff.strip():
            logger.warning(
                "Sub-agent diff disagrees with orchestrator-derived diff",
                extra={
                    "workflow_id": task.workflow_id,
                    "reported_bytes": len(complete.diff.encode()),
                    "rederived_bytes": len(local_diff.encode()),
                },
            )


def _load_system_prompt() -> str:
    """Read the finalisation system prompt from disk.

    Missing prompt files are deployment errors and should fail startup rather
    than silently changing finalization behavior.
    """
    path = Path(__file__).resolve().parent.parent.parent.parent / _FINALIZATION_PROMPT_FILE
    return path.read_text(encoding="utf-8")
