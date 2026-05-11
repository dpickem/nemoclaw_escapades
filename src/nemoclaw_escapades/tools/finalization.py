"""Model-callable finalization tools.

The finalization AgentLoop registers these tools after a coding sub-agent
returns ``task.complete``.  Tools do the side effect, update
:class:`FinalizationState`, and optionally ask the connector renderer to post
the result to the originating thread.

Tool inventory (per design §7.1):

- ``present_work_to_user`` — render the synthesised work to the user
  with action buttons (Push & PR, Iterate, Discard).
- ``push_branch`` / ``push_and_create_pr`` — git ops for landing the
  sub-agent's diff.
- ``discard_work`` — wipe the per-workflow workspace.
- ``re_delegate`` — fire a follow-up ``task.assign`` reusing the
  workflow's pinned baseline.
- ``destroy_sandbox`` — explicit M3-only sandbox teardown; an explicit
  no-op in M2b's same-sandbox topology.

The tools never block on inference themselves — they hand control
back to the finalisation ``AgentLoop`` which decides what to call
next.  Re-delegation in particular returns immediately after the
follow-up ``task.assign`` is sent; the second iteration's
finalisation runs as its own dispatcher-driven ``asyncio.Task``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from nemoclaw_escapades.agent.git_helpers import (
    GitCommandError,
    checkout_branch,
    commit_workspace,
)
from nemoclaw_escapades.agent.git_helpers import (
    push_branch as git_push_branch,
)
from nemoclaw_escapades.agent.github_helpers import GitHubCommandError, create_pull_request
from nemoclaw_escapades.nmb.protocol import TaskAssignPayload, TaskCompletePayload
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, tool

if TYPE_CHECKING:
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager
    from nemoclaw_escapades.orchestrator.workflow import WorkflowContext, WorkflowRenderer

logger = get_logger("tools.finalization")

# Logical toolset name used by the registry for grouping.
_TOOLSET: str = "finalization"

# Diff characters passed to the renderer/model as preview text.
_DIFF_PREVIEW_LIMIT: int = 8_000

# Default commit message when the finalisation model didn't supply one.
_DEFAULT_COMMIT_MESSAGE: str = "Finalize delegated work"

# Default remote for finalization git push operations.
_DEFAULT_REMOTE: str = "origin"

# Branch prefix used by button-driven finalization flows.
_FINALIZATION_BRANCH_PREFIX: str = "finalize"

# Iteration number increment for each re-delegation.
_ITERATION_INCREMENT: int = 1

# Minimum max_turns accepted by re_delegate's schema.
_MIN_MAX_TURNS: int = 1

# Prefix used to identify recoverable tool errors.
_ERROR_PREFIX: str = "Error:"


class FinalizationAction(StrEnum):
    """Names of model-callable finalization tools."""

    PRESENT_WORK_TO_USER = "present_work_to_user"
    PUSH_BRANCH = "push_branch"
    PUSH_AND_CREATE_PR = "push_and_create_pr"
    DISCARD_WORK = "discard_work"
    RE_DELEGATE = "re_delegate"
    DESTROY_SANDBOX = "destroy_sandbox"
    MODEL_RESPONSE = "model_response"


@dataclass
class FinalizationState:
    """Outcome captured by finalization tools for the coordinator's caller.

    The coordinator reads this after the tool call to decide what text to return
    and whether the dispatcher should deregister the workflow.

    Attributes:
        action: Tool name that ran, or ``None`` if no tool was called.
        message: User-facing tool result.
        is_terminal: Whether this action ends the workflow.
    """

    # Tool name that ran, or empty when no finalization tool was called.
    action: FinalizationAction | None = None
    # User-facing tool result text.
    message: str = ""
    # Whether this action ends the workflow lifecycle.
    is_terminal: bool = False


class FinalizationSession:
    """Per-workflow context bound to one finalisation ``AgentLoop`` run.

    A fresh session is created for each ``task.complete``.  Tool methods mutate
    ``state`` and use the optional renderer to publish user-facing results.

    Attributes:
        task: Original or current task assignment.
        complete: Validated sub-agent completion payload.
        context: Optional workflow metadata for rendering.
        delegation_manager: Optional manager for re-delegation.
        renderer: Optional connector renderer for user-facing results.
        state: Mutable outcome recorded by tool calls.
    """

    def __init__(
        self,
        *,
        task: TaskAssignPayload,
        complete: TaskCompletePayload,
        context: WorkflowContext | None = None,
        delegation_manager: DelegationManager | None = None,
        renderer: WorkflowRenderer | None = None,
    ) -> None:
        # Original/current task assignment for this workflow.
        self.task = task
        # Validated completion payload from the sub-agent.
        self.complete = complete
        # Optional workflow metadata for connector rendering.
        self.context = context
        # Optional manager used by re_delegate.
        self.delegation_manager = delegation_manager
        # Optional connector renderer for user-facing tool results.
        self.renderer = renderer
        # Mutable outcome recorded by tool calls.
        self.state = FinalizationState()

    async def present_work_to_user(
        self,
        summary: str | None = None,
        include_diff: bool = True,
    ) -> str:
        """Render the synthesised work to the originating channel.

        Uses the sub-agent summary unless *summary* overrides it.  When a
        renderer is wired, the user also receives action buttons.

        Args:
            summary: Optional replacement for the sub-agent summary.
            include_diff: Whether to include a truncated diff preview.

        Returns:
            The text shown to the user and returned to the finalization model.
        """
        rendered = summary or self.complete.summary
        diff_preview = (
            self.complete.diff[:_DIFF_PREVIEW_LIMIT] if include_diff and self.complete.diff else ""
        )
        if self.renderer is not None and self.context is not None:
            await self.renderer.render_present_work(
                context=self.context,
                summary=rendered,
                diff=diff_preview,
            )
        self.state.action = FinalizationAction.PRESENT_WORK_TO_USER
        self.state.message = rendered
        return rendered

    async def push_branch(
        self,
        branch_name: str,
        commit_message: str = _DEFAULT_COMMIT_MESSAGE,
        remote: str = _DEFAULT_REMOTE,
    ) -> str:
        """Commit the workspace changes and push *branch_name* to *remote*.

        Sets ``state.is_terminal`` only when checkout, commit, and push all
        succeed.  Recoverable git failures keep the workflow registered.

        Args:
            branch_name: Branch to create/reset and push.
            commit_message: Commit message for staged delegated work.
            remote: Git remote name.

        Returns:
            Git push output or an ``Error: ...`` message.
        """
        message = await self._do_push_branch(branch_name, commit_message, remote)
        await self._render_action_result(FinalizationAction.PUSH_BRANCH, message)
        self.state.action = FinalizationAction.PUSH_BRANCH
        self.state.message = message
        self.state.is_terminal = not message.startswith(_ERROR_PREFIX)
        return message

    async def _do_push_branch(
        self,
        branch_name: str,
        commit_message: str,
        remote: str,
    ) -> str:
        """Run checkout/commit/push without rendering or mutating state.

        Args:
            branch_name: Branch to create/reset and push.
            commit_message: Commit message for staged delegated work.
            remote: Git remote name.

        Returns:
            Git push output or an ``Error: ...`` message.
        """
        workspace = self.task.workspace_root
        try:
            await checkout_branch(workspace, branch_name)
            await commit_workspace(workspace, commit_message)
            return await git_push_branch(workspace, branch_name, remote=remote)
        except GitCommandError as exc:
            return f"{_ERROR_PREFIX} {exc}"

    async def push_and_create_pr(
        self,
        branch_name: str,
        title: str,
        body: str = "",
        remote: str = _DEFAULT_REMOTE,
    ) -> str:
        """Commit, push, and open a GitHub PR via ``gh``.

        Renders one combined result for push plus PR creation.  Sets
        ``state.is_terminal`` only when both steps succeed.

        Args:
            branch_name: Branch to create/reset and push.
            title: Pull request title.
            body: Pull request body; defaults to the sub-agent summary.
            remote: Git remote name.

        Returns:
            Pull request URL/output or an ``Error: ...`` message.
        """
        push_output = await self._do_push_branch(branch_name, title, remote)
        if push_output.startswith(_ERROR_PREFIX):
            message = push_output
        else:
            try:
                message = await create_pull_request(
                    self.task.workspace_root,
                    title=title,
                    body=body or self.complete.summary,
                )
            except GitHubCommandError as exc:
                message = f"{_ERROR_PREFIX} {exc}"

        await self._render_action_result(FinalizationAction.PUSH_AND_CREATE_PR, message)
        self.state.action = FinalizationAction.PUSH_AND_CREATE_PR
        self.state.message = message
        self.state.is_terminal = not message.startswith(_ERROR_PREFIX)
        return message

    async def discard_work(self, reason: str = "") -> str:
        """Discard the per-workflow workspace.

        Refuses paths that do not look like per-agent workspaces.  Successful
        deletion marks the workflow terminal.

        Args:
            reason: Optional acknowledgement text.

        Returns:
            Discard acknowledgement or an ``Error: ...`` message.
        """
        path = Path(self.task.workspace_root).expanduser().resolve()
        if not _looks_like_per_agent_workspace(path, self.task.agent_id):
            error_text = f"{_ERROR_PREFIX} refusing to discard non-agent path {path}"
            await self._render_action_result(FinalizationAction.DISCARD_WORK, error_text)
            self.state.action = FinalizationAction.DISCARD_WORK
            self.state.message = error_text
            return error_text

        if path.exists():
            shutil.rmtree(path)

        message = reason or f"Discarded delegated workspace at {path}."
        await self._render_action_result(FinalizationAction.DISCARD_WORK, message)
        self.state.action = FinalizationAction.DISCARD_WORK
        self.state.message = message
        self.state.is_terminal = True
        return message

    async def re_delegate(
        self,
        prompt: str,
        max_turns: int | None = None,
        model: str | None = None,
    ) -> str:
        """Fire a follow-up ``task.assign`` reusing the pinned baseline.

        Updates the registered workflow context before sending so the next
        ``task.complete`` finalizes the new iteration.

        Args:
            prompt: Updated instructions for the coding sub-agent.
            max_turns: Optional per-iteration tool-round cap.
            model: Optional per-iteration model override.

        Returns:
            Re-delegation acknowledgement or an error message.
        """
        if self.delegation_manager is None:
            return f"{_ERROR_PREFIX} delegation manager unavailable"

        followup = TaskAssignPayload(
            prompt=prompt,
            workflow_id=self.task.workflow_id,
            parent_sandbox_id=self.task.parent_sandbox_id,
            agent_id=self.task.agent_id,
            workspace_root=self.task.workspace_root,
            max_turns=max_turns or self.task.max_turns,
            model=model or self.task.model,
            tool_surface=self.task.tool_surface,
            context_files=self.task.context_files,
            workspace_baseline=self.task.workspace_baseline,
            is_iteration=True,
            iteration_number=self.task.iteration_number + _ITERATION_INCREMENT,
        )

        # Keep dispatcher-held context in sync before sending task.assign.
        if self.context is not None:
            self.context.task = followup

        self.task = followup
        await self.delegation_manager.delegate(followup)

        message = (
            f"Re-delegated workflow {followup.workflow_id} (iteration {followup.iteration_number})."
        )
        await self._render_action_result(FinalizationAction.RE_DELEGATE, message)
        self.state.action = FinalizationAction.RE_DELEGATE
        self.state.message = message
        return message

    async def destroy_sandbox(self) -> str:
        """No-op in M2b; real sandbox teardown lands with M3.

        Returns:
            Acknowledgement text explaining that no sandbox exists to destroy.
        """
        message = "Sub-agent process is already single-shot; no sandbox to destroy."
        await self._render_action_result(FinalizationAction.DESTROY_SANDBOX, message)
        self.state.action = FinalizationAction.DESTROY_SANDBOX
        self.state.message = message
        self.state.is_terminal = True
        return message

    async def _render_action_result(self, action: FinalizationAction, result: str) -> None:
        """Render a finalisation action result through the connector.

        Args:
            action: Finalization tool name.
            result: User-facing tool result text.
        """
        if self.renderer is None or self.context is None:
            return
        try:
            await self.renderer.render_finalization_action(
                context=self.context,
                action=action.value,
                result=result,
            )
        except Exception:  # noqa: BLE001 — connector surface is broad
            logger.warning(
                "Renderer raised on finalization action %s",
                action.value,
                exc_info=True,
            )


def _looks_like_per_agent_workspace(path: Path, agent_id: str) -> bool:
    """Return ``True`` when *path* looks like a per-agent workspace.

    Args:
        path: Candidate workspace path.
        agent_id: Coding sub-agent id.

    Returns:
        Whether the path shape is safe for discard.
    """
    name = path.name
    if name.startswith("agent-"):
        return True

    if agent_id and name.endswith(agent_id):
        return True

    return False


def create_finalization_tool_registry(session: FinalizationSession) -> ToolRegistry:
    """Build the single-use finalization tool registry for *session*.

    Args:
        session: Per-workflow finalization session.

    Returns:
        Tool registry bound to that session.
    """
    registry = ToolRegistry()

    @tool(
        FinalizationAction.PRESENT_WORK_TO_USER.value,
        "Present synthesized sub-agent work to the user with review actions.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "include_diff": {"type": "boolean", "default": True},
            },
        },
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def present_work_to_user(summary: str | None = None, include_diff: bool = True) -> str:
        """Present delegated work to the user."""
        return await session.present_work_to_user(summary, include_diff)

    @tool(
        FinalizationAction.PUSH_BRANCH.value,
        "Commit delegated work and push a branch without creating a PR.",
        {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string"},
                "commit_message": {"type": "string", "default": _DEFAULT_COMMIT_MESSAGE},
                "remote": {"type": "string", "default": _DEFAULT_REMOTE},
            },
            "required": ["branch_name"],
        },
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def push_branch(
        branch_name: str,
        commit_message: str = _DEFAULT_COMMIT_MESSAGE,
        remote: str = _DEFAULT_REMOTE,
    ) -> str:
        """Push delegated work to a branch."""
        return await session.push_branch(branch_name, commit_message, remote)

    @tool(
        FinalizationAction.PUSH_AND_CREATE_PR.value,
        "Commit delegated work, push a branch, and create a GitHub PR.",
        {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string", "default": ""},
                "remote": {"type": "string", "default": _DEFAULT_REMOTE},
            },
            "required": ["branch_name", "title"],
        },
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def push_and_create_pr(
        branch_name: str,
        title: str,
        body: str = "",
        remote: str = _DEFAULT_REMOTE,
    ) -> str:
        """Push delegated work and create a pull request."""
        return await session.push_and_create_pr(branch_name, title, body, remote)

    @tool(
        FinalizationAction.DISCARD_WORK.value,
        "Discard delegated workspace changes.",
        {"type": "object", "properties": {"reason": {"type": "string", "default": ""}}},
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def discard_work(reason: str = "") -> str:
        """Discard delegated workspace changes."""
        return await session.discard_work(reason)

    @tool(
        FinalizationAction.RE_DELEGATE.value,
        "Send updated instructions to the same sub-agent baseline.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "max_turns": {"type": "integer", "minimum": _MIN_MAX_TURNS},
                "model": {"type": "string"},
            },
            "required": ["prompt"],
        },
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def re_delegate(
        prompt: str,
        max_turns: int | None = None,
        model: str | None = None,
    ) -> str:
        """Send follow-up instructions to the coding sub-agent."""
        return await session.re_delegate(prompt, max_turns, model)

    @tool(
        FinalizationAction.DESTROY_SANDBOX.value,
        "Tear down the sub-agent sandbox or process.",
        {"type": "object", "properties": {}},
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def destroy_sandbox() -> str:
        """Tear down the sub-agent sandbox or process."""
        return await session.destroy_sandbox()

    for spec in (
        present_work_to_user,
        push_branch,
        push_and_create_pr,
        discard_work,
        re_delegate,
        destroy_sandbox,
    ):
        registry.register(spec)

    return registry
