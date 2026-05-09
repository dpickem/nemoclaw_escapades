"""Model-callable finalization tools.

The orchestrator's finalisation ``AgentLoop`` (see
``orchestrator/finalization.py``) registers these tools and then runs
them against a typed ``TaskCompletePayload``.  Each tool's job is to
*do* the work and let the renderer (a connector-side hook) push the
user-facing result to the originating channel.

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

import asyncio
import shutil
from dataclasses import dataclass
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
from nemoclaw_escapades.nmb.protocol import TaskAssignPayload, TaskCompletePayload
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, tool

if TYPE_CHECKING:
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager
    from nemoclaw_escapades.orchestrator.workflow import WorkflowContext, WorkflowRenderer

logger = get_logger("tools.finalization")

# Logical toolset name used by the registry for grouping
_TOOLSET: str = "finalization"

# Sanity cap on the diff body the tool passes to its platform-specific
# renderer.  This is **not** a per-message Slack constraint —
# splitting the rendered text into Slack-safe section blocks is the
# renderer's job (see ``connectors/slack/finalization.py``).  This cap
# is a transport bound that keeps the tool's textual return value
# digestible for the model's next inference round and prevents
# pathological diffs from blowing out the renderer's input.  Diffs
# larger than this are truncated; users can still inspect the full
# diff in the workspace directly.
_DIFF_PREVIEW_LIMIT: int = 8_000

# Default commit message when the finalisation model didn't supply one.
_DEFAULT_COMMIT_MESSAGE: str = "Finalize delegated work"


@dataclass
class FinalizationState:
    """Outcome captured by finalization tools for the coordinator's caller.

    Attributes:
        action: Tool name that ran (``"present_work_to_user"`` etc.).
        message: User-facing text from the tool — already rendered or
            ready to render.  The coordinator returns this verbatim
            from :meth:`FinalizationCoordinator.finalize_to_text`.
        is_terminal: ``True`` when the chosen action ends the
            workflow's lifecycle (push, discard, sandbox-destroy)
            so the dispatcher can deregister the
            :class:`WorkflowContext`.  ``False`` for actions that
            keep the workflow alive (``present_work_to_user``
            waits on a button click; ``re_delegate`` carries the
            same ``workflow_id`` into iteration 2).
    """

    action: str = ""
    message: str = ""
    is_terminal: bool = False


class FinalizationSession:
    """Per-workflow context bound to one finalisation ``AgentLoop`` run.

    The orchestrator's finalisation flow constructs one session per
    incoming ``task.complete``, then runs an ``AgentLoop`` whose tool
    registry is built from this session.  Tools mutate
    ``self.state`` and (for user-facing rendering) call into
    ``self.renderer`` so the connector pushes the result to the
    originating thread.

    Attributes:
        task: The original ``TaskAssignPayload`` (carries
            ``workspace_root``, ``workspace_baseline``, etc.).
        complete: The validated ``TaskCompletePayload`` from the
            sub-agent.
        context: Workflow-level metadata (channel, thread,
            workflow_id) the renderer needs to address Slack.
        delegation_manager: Required to fire ``re_delegate`` follow-
            ups; ``None`` disables that tool path.
        renderer: Connector-side push surface.  ``None`` runs in
            "headless" mode (e.g. tests) where ``present_work_to_user``
            simply records text in ``state.message``.
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
        self.task = task
        self.complete = complete
        self.context = context
        self.delegation_manager = delegation_manager
        self.renderer = renderer
        self.state = FinalizationState()

    async def present_work_to_user(
        self,
        summary: str | None = None,
        include_diff: bool = True,
    ) -> str:
        """Render the synthesised work to the originating channel.

        Args:
            summary: Override for the user-facing summary.  When
                ``None``, the sub-agent's verbatim ``summary`` is used.
            include_diff: Inline a truncated diff preview below the
                summary; ignored when the diff is empty.

        Returns:
            The rendered text (or, when no renderer is wired, the
            same text the renderer would have posted).  The
            finalisation ``AgentLoop`` consumes this verbatim so the
            tool result the model sees matches what the user sees.
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
        self.state.action = "present_work_to_user"
        self.state.message = rendered
        return rendered

    async def push_branch(
        self,
        branch_name: str,
        commit_message: str = _DEFAULT_COMMIT_MESSAGE,
        remote: str = "origin",
    ) -> str:
        """Commit the workspace changes and push *branch_name* to *remote*.

        Operates on the local workspace the sub-agent left behind;
        delegates the actual git invocations to public helpers in
        ``agent/git_helpers.py`` so the structured-error / timeout /
        TLS-bundle behaviour matches the model-callable git tools.

        Args:
            branch_name: Local branch the work lands on.  Created
                with ``-B`` semantics so an existing branch is reset
                to HEAD.
            commit_message: Commit subject; falls back to
                ``"Finalize delegated work"``.
            remote: Remote name (default ``origin``).

        Returns:
            Combined push output on success, or a structured error
            string if any sub-step (checkout / commit / push) fails.
            Errors are also rendered to the originating channel via
            ``renderer.render_finalization_action`` so the user sees
            them.

        State: ``is_terminal`` is set to ``True`` only when the push
        actually succeeded.  A recoverable git failure (network blip,
        auth refresh, divergence) leaves the workflow registered so
        the user can retry from the same Push & PR / Iterate /
        Discard buttons in their thread.
        """
        message = await self._do_push_branch(branch_name, commit_message, remote)
        await self._render_action_result("push_branch", message)
        self.state.action = "push_branch"
        self.state.message = message
        self.state.is_terminal = not message.startswith("Error:")
        return message

    async def _do_push_branch(
        self,
        branch_name: str,
        commit_message: str,
        remote: str,
    ) -> str:
        """Inner git-ops only — no rendering, no state mutation.

        Shared by :meth:`push_branch` (which adds the single render +
        state mutation) and :meth:`push_and_create_pr` (which composes
        push with ``gh pr create`` and renders once for the whole
        operation).  Splitting the rendering off here is the fix for
        the double-render bug where ``push_and_create_pr`` used to
        post one Slack message for the push and a second for the PR
        creation, when the user expects a single atomic outcome.
        """
        workspace = self.task.workspace_root
        try:
            await checkout_branch(workspace, branch_name)
            await commit_workspace(workspace, commit_message)
            return await git_push_branch(workspace, branch_name, remote=remote)
        except GitCommandError as exc:
            return f"Error: {exc}"

    async def push_and_create_pr(
        self,
        branch_name: str,
        title: str,
        body: str = "",
        remote: str = "origin",
    ) -> str:
        """Commit, push, and open a GitHub PR via ``gh``.

        Composed from the same inner git ops as :meth:`push_branch`
        plus a ``gh pr create`` invocation, but renders **exactly
        once** for the combined operation: the user sees a single
        Slack message describing the final outcome rather than one
        for the push and another for the PR.

        Args:
            branch_name: Branch to push.
            title: Pull-request title.
            body: PR body; defaults to the sub-agent's ``summary``.
            remote: Remote name (default ``origin``).

        Returns:
            ``gh`` stdout on success (typically the PR URL) or an
            ``Error: …`` string on failure.  Always rendered through
            the renderer so the user sees the outcome.

        State: ``is_terminal`` is set to ``True`` only when the push
        AND ``gh pr create`` both succeeded.  Either failure leaves
        the workflow registered so the user can retry, iterate, or
        discard from the same buttons.
        """
        push_output = await self._do_push_branch(branch_name, title, remote)
        if push_output.startswith("Error:"):
            message = push_output
        else:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "pr",
                "create",
                "--title",
                title,
                "--body",
                body or self.complete.summary,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.task.workspace_root,
            )
            stdout, stderr = await proc.communicate()
            out = (stdout or stderr).decode(errors="replace").strip()
            message = out if proc.returncode == 0 else f"Error: gh pr create failed: {out}"
        await self._render_action_result("push_and_create_pr", message)
        self.state.action = "push_and_create_pr"
        self.state.message = message
        self.state.is_terminal = not message.startswith("Error:")
        return message

    async def discard_work(self, reason: str = "") -> str:
        """Discard the per-workflow workspace.

        The workspace path is verified to be a real directory under
        the configured per-agent subdirectory (``agent-…``) before
        ``shutil.rmtree``.  The check is defence-in-depth against a
        misconfigured ``TaskAssignPayload.workspace_root`` — the
        intended path is always
        ``<config.coding.workspace_root>/agent-<agent_id>``, so any
        other shape fails closed.

        Args:
            reason: Optional human-readable note rendered with the
                acknowledgement.

        Returns:
            Acknowledgement text.
        """
        path = Path(self.task.workspace_root).expanduser().resolve()
        if not _looks_like_per_agent_workspace(path, self.task.agent_id):
            error_text = f"Error: refusing to discard non-agent path {path}"
            await self._render_action_result("discard_work", error_text)
            self.state.action = "discard_work"
            self.state.message = error_text
            return error_text
        if path.exists():
            shutil.rmtree(path)
        message = reason or f"Discarded delegated workspace at {path}."
        await self._render_action_result("discard_work", message)
        self.state.action = "discard_work"
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

        Returns immediately after the follow-up is sent — the second
        iteration's finalisation runs as its own dispatcher-driven
        ``asyncio.Task`` (design §8.2), so this call does not block
        the orchestrator's main loop.

        **Workflow-context mutation.**  Before the follow-up is sent,
        the workflow's :class:`WorkflowContext` is updated in place so
        ``context.task`` reflects the new iteration's payload.  The
        context is the *same object* the dispatcher holds in its
        ``_workflows`` registry — the mutation propagates to every
        future ``task.complete`` arrival on this ``workflow_id``.
        Without this update, the dispatcher would forever see the
        iteration-0 task on the registered context and every cascading
        re-delegation would compute ``iteration_number = 0 + 1 = 1``
        no matter how many follow-ups had already happened.
        ``self.task`` is also rebound so a model that calls
        ``re_delegate`` twice within the same finalisation increments
        correctly on the second call too.

        Args:
            prompt: Updated instructions for the sub-agent.
            max_turns: Optional cap override; defaults to the
                original task's value.
            model: Optional model override.

        Returns:
            Acknowledgement string suitable for the finalisation
            ``AgentLoop`` to read as the tool's textual result.
        """
        if self.delegation_manager is None:
            return "Error: delegation manager unavailable"
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
            iteration_number=self.task.iteration_number + 1,
        )
        # Update the dispatcher's registered workflow BEFORE firing the
        # new ``task.assign`` so a fast sub-agent can't race the
        # mutation: the dispatcher's ``_handle_task_complete`` reads
        # ``ctx.task`` to seed the next iteration's
        # :class:`FinalizationSession`, and we need that to see the
        # new ``iteration_number`` / ``prompt``.  Mutating in place
        # is intentional — :class:`WorkflowContext` is a regular
        # dataclass and the dispatcher holds the same object.
        if self.context is not None:
            self.context.task = followup
        self.task = followup
        await self.delegation_manager.delegate(followup, context=self.context)
        message = (
            f"Re-delegated workflow {followup.workflow_id} (iteration {followup.iteration_number})."
        )
        await self._render_action_result("re_delegate", message)
        self.state.action = "re_delegate"
        self.state.message = message
        return message

    async def destroy_sandbox(self) -> str:
        """No-op in M2b: the sub-agent process is single-shot.

        Recorded as a tool call so audit trails distinguish "model
        chose not to push" (this) from "model never picked any tool".
        Real teardown lands in M3 alongside ``openshell sandbox delete``.
        """
        message = "Sub-agent process is already single-shot; no sandbox to destroy."
        await self._render_action_result("destroy_sandbox", message)
        self.state.action = "destroy_sandbox"
        self.state.message = message
        self.state.is_terminal = True
        return message

    async def _render_action_result(self, action: str, result: str) -> None:
        """Render a finalisation action result through the connector.

        No-op when no renderer is wired (headless tests).  Errors are
        swallowed so a connector failure can't poison the
        finalisation loop's tool-call output.
        """
        if self.renderer is None or self.context is None:
            return
        try:
            await self.renderer.render_finalization_action(
                context=self.context,
                action=action,
                result=result,
            )
        except Exception:  # noqa: BLE001 — connector surface is broad
            logger.warning(
                "Renderer raised on finalization action %s",
                action,
                exc_info=True,
            )


def _looks_like_per_agent_workspace(path: Path, agent_id: str) -> bool:
    """Return ``True`` when *path* matches the expected per-agent shape.

    The orchestrator's ``delegate_task`` tool builds workspace roots
    as ``<config.coding.workspace_root>/agent-<short-id>``; the
    sub-agent's ``agent_id`` is ``coding-<short-id>``.  We accept
    either form so the check is robust to future spawn-id schemes
    while still refusing arbitrary paths the model could pass through.
    """
    name = path.name
    if name.startswith("agent-"):
        return True
    if agent_id and name.endswith(agent_id):
        return True
    return False


def create_finalization_tool_registry(session: FinalizationSession) -> ToolRegistry:
    """Build the finalization tool registry bound to one *session*.

    Returned registry is single-use: each ``task.complete`` constructs
    a new session and a new registry.  No surfacing or ``tool_search``
    indirection — the finalisation model sees every tool up front
    because the surface is small and bounded.
    """
    registry = ToolRegistry()

    @tool(
        "present_work_to_user",
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
        return await session.present_work_to_user(summary, include_diff)

    @tool(
        "push_branch",
        "Commit delegated work and push a branch without creating a PR.",
        {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string"},
                "commit_message": {"type": "string", "default": _DEFAULT_COMMIT_MESSAGE},
                "remote": {"type": "string", "default": "origin"},
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
        remote: str = "origin",
    ) -> str:
        return await session.push_branch(branch_name, commit_message, remote)

    @tool(
        "push_and_create_pr",
        "Commit delegated work, push a branch, and create a GitHub PR.",
        {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string", "default": ""},
                "remote": {"type": "string", "default": "origin"},
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
        remote: str = "origin",
    ) -> str:
        return await session.push_and_create_pr(branch_name, title, body, remote)

    @tool(
        "discard_work",
        "Discard delegated workspace changes.",
        {"type": "object", "properties": {"reason": {"type": "string", "default": ""}}},
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def discard_work(reason: str = "") -> str:
        return await session.discard_work(reason)

    @tool(
        "re_delegate",
        "Send updated instructions to the same sub-agent baseline.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "max_turns": {"type": "integer", "minimum": 1},
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
        return await session.re_delegate(prompt, max_turns, model)

    @tool(
        "destroy_sandbox",
        "Tear down the sub-agent sandbox or process.",
        {"type": "object", "properties": {}},
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def destroy_sandbox() -> str:
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
