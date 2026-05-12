"""Platform-neutral routing for finalisation button clicks.

Connectors emit :class:`NormalizedRequest` actions for Push & PR, Iterate, and
Discard buttons.  This module looks up the live workflow, builds a
:class:`FinalizationSession`, and invokes the matching finalisation tool.

Iteration is a two-step flow: the button click records pending state, and the
user's next text reply in the same thread becomes the ``re_delegate`` prompt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from nemoclaw_escapades.models.types import (
    FINALIZATION_ACTION_DISCARD,
    FINALIZATION_ACTION_ITERATE,
    FINALIZATION_ACTION_PUSH_PR,
    NormalizedRequest,
    RichResponse,
    TextBlock,
)
from nemoclaw_escapades.nmb.protocol import TaskCompletePayload
from nemoclaw_escapades.observability.logging import get_logger

if TYPE_CHECKING:
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager
    from nemoclaw_escapades.orchestrator.dispatcher import WorkflowDispatcher
    from nemoclaw_escapades.orchestrator.workflow import WorkflowContext, WorkflowRenderer
    from nemoclaw_escapades.tools.finalization import FinalizationSession

logger = get_logger("orchestrator.finalization_actions")

# Characters of task prompt copied into synthetic completion summaries.
_COMPLETION_SUMMARY_PROMPT_LIMIT: int = 120

# Characters of task prompt used as the generated PR title.
_PR_TITLE_PROMPT_LIMIT: int = 80

# Builds a finalization session for one workflow/completion pair.
SessionFactory = Callable[
    ["WorkflowContext", "TaskCompletePayload"],
    "FinalizationSession",
]


def is_finalization_action(request: NormalizedRequest) -> bool:
    """Return ``True`` when *request* carries a finalisation button click.

    The orchestrator uses this to gate routing without re-checking
    each ``action_id`` individually.
    """
    if request.action is None:
        return False
    return request.action.action_id in {
        FINALIZATION_ACTION_PUSH_PR,
        FINALIZATION_ACTION_ITERATE,
        FINALIZATION_ACTION_DISCARD,
    }


class FinalizationActionHandler:
    """Routes finalisation button clicks to the right finalisation tool.

    The handler is connector-neutral: Slack supplies the normalized request
    today, but the routing only depends on workflow id and action id.
    """

    def __init__(
        self,
        *,
        dispatcher: WorkflowDispatcher,
        session_factory: SessionFactory | None = None,
        delegation_manager: DelegationManager | None = None,
        renderer: WorkflowRenderer | None = None,
    ) -> None:
        """Wire dispatcher, session factory, and optional tool collaborators.

        Args:
            dispatcher: Live workflow lookup and deregistration surface.
            session_factory: Optional override for tests.
            delegation_manager: Used by ``re_delegate``.
            renderer: Connector-side renderer for tool outcomes.
        """
        # Live workflow registry and cleanup owner.
        self._dispatcher = dispatcher
        # Optional manager used by sessions for re-delegation.
        self._delegation_manager = delegation_manager
        # Optional renderer used by sessions to publish action results.
        self._renderer = renderer
        # Factory for per-click FinalizationSession instances.
        self._session_factory: SessionFactory = (
            session_factory or self._build_default_session_factory()
        )
        # Thread key -> workflow id for pending Iterate prompts.
        self._pending_iteration: dict[str, str] = {}

    def _build_default_session_factory(self) -> SessionFactory:
        """Bind ``delegation_manager`` and ``renderer`` into the factory."""
        delegation_manager = self._delegation_manager
        renderer = self._renderer

        def _factory(
            ctx: WorkflowContext,
            complete: TaskCompletePayload,
        ) -> FinalizationSession:
            from nemoclaw_escapades.tools.finalization import FinalizationSession

            return FinalizationSession(
                task=ctx.task,
                complete=complete,
                context=ctx,
                delegation_manager=delegation_manager,
                renderer=renderer,
            )

        return _factory

    async def handle(self, request: NormalizedRequest) -> RichResponse:
        """Route a finalisation action click to the matching tool.

        Unknown or stale workflow ids return a friendly thread reply instead
        of raising into the connector.
        """
        action = request.action
        if action is None:
            return _text_reply(request, "No finalisation action found.")

        # Look up the workflow context for this action.
        workflow_id = action.value
        ctx = self._dispatcher.get_workflow(workflow_id)
        if ctx is None:
            return _text_reply(
                request,
                f"Workflow `{workflow_id}` is no longer active "
                "(probably already finalised or aged out).",
            )

        # Route the action to the matching tool.
        if action.action_id == FINALIZATION_ACTION_PUSH_PR:
            return await self._handle_push_pr(request, ctx)
        if action.action_id == FINALIZATION_ACTION_DISCARD:
            return await self._handle_discard(request, ctx)
        if action.action_id == FINALIZATION_ACTION_ITERATE:
            return self._handle_iterate(request, ctx)

        return _text_reply(request, f"Unknown finalisation action `{action.action_id}`.")

    def is_pending_iteration(self, thread_key: str) -> bool:
        """Return whether *thread_key* is waiting on iteration feedback."""
        return thread_key in self._pending_iteration

    async def consume_iteration_feedback(
        self,
        request: NormalizedRequest,
        thread_key: str,
    ) -> RichResponse:
        """Use the user's text reply as the ``re_delegate`` prompt.

        Looks up the pending workflow id, fires ``re_delegate``, and clears the
        pending entry before running the tool.
        """
        workflow_id = self._pending_iteration.pop(thread_key, None)
        if workflow_id is None:
            return _text_reply(
                request,
                "No pending iteration; treating this as a fresh request.",
            )

        # Look up the workflow context for this iteration.
        ctx = self._dispatcher.get_workflow(workflow_id)
        if ctx is None:
            return _text_reply(
                request,
                f"Workflow `{workflow_id}` is no longer active.",
            )

        complete_stub = _completion_stub(
            ctx,
            f"Iterating on workflow {ctx.workflow_id} per user feedback.",
        )
        session = self._session_factory(ctx, complete_stub)

        # Fire the iteration follow-up.
        try:
            result = await session.re_delegate(request.text)
        except Exception as exc:  # noqa: BLE001 — surface to user, don't crash the connector
            logger.warning("re_delegate failed", exc_info=True)
            return _text_reply(request, f"Re-delegation failed: {exc}")

        return _text_reply(request, result)

    async def _handle_push_pr(
        self,
        request: NormalizedRequest,
        ctx: WorkflowContext,
    ) -> RichResponse:
        """Run Push & PR for a clicked workflow and deregister on success."""
        complete_stub = _completion_stub(
            ctx,
            f"Finalize {_truncate(ctx.task.prompt, _COMPLETION_SUMMARY_PROMPT_LIMIT)}",
        )
        session = self._session_factory(ctx, complete_stub)
        branch = f"finalize/{ctx.workflow_id}"
        title = _truncate(ctx.task.prompt, _PR_TITLE_PROMPT_LIMIT)

        # Fire the Push & PR tool.
        try:
            result = await asyncio.shield(
                session.push_and_create_pr(branch_name=branch, title=title)
            )
        except Exception as exc:  # noqa: BLE001 — surface to user, don't crash the connector
            logger.warning("push_and_create_pr failed", exc_info=True)
            return _text_reply(request, f"Push & PR failed: {exc}")

        # Deregister the workflow if the action is terminal.
        if session.state.is_terminal:
            await self._dispatcher.deregister_workflow(ctx.workflow_id)

        return _text_reply(request, result)

    async def _handle_discard(
        self,
        request: NormalizedRequest,
        ctx: WorkflowContext,
    ) -> RichResponse:
        """Run Discard for a clicked workflow and deregister on success."""
        complete_stub = _completion_stub(ctx, "Discarding workflow per user request.")
        session = self._session_factory(ctx, complete_stub)

        try:
            result = await session.discard_work("Discarded by user")
        except Exception as exc:  # noqa: BLE001 — surface to user, don't crash the connector
            logger.warning("discard_work failed", exc_info=True)
            return _text_reply(request, f"Discard failed: {exc}")

        if session.state.is_terminal:
            await self._dispatcher.deregister_workflow(ctx.workflow_id)

        return _text_reply(request, result)

    def _handle_iterate(
        self,
        request: NormalizedRequest,
        ctx: WorkflowContext,
    ) -> RichResponse:
        """Record that the next message in this thread is iteration feedback.

        The click itself does not contact the coding agent.  The next text
        message with the same thread key is intercepted by
        :meth:`consume_iteration_feedback`, which calls ``re_delegate``.
        """
        thread_key = request.thread_ts or request.request_id
        self._pending_iteration[thread_key] = ctx.workflow_id
        return _text_reply(
            request,
            "OK — please reply in this thread with the changes you'd like the "
            "sub-agent to make. Your next message becomes the iteration prompt.",
        )


def _text_reply(request: NormalizedRequest, text: str) -> RichResponse:
    """Build a plain-text :class:`RichResponse` addressed to *request*'s thread."""
    return RichResponse(
        channel_id=request.channel_id,
        thread_ts=request.thread_ts,
        blocks=[TextBlock(text=text)],
    )


def _completion_stub(ctx: WorkflowContext, summary: str) -> TaskCompletePayload:
    """Build the minimal completion payload needed by a click-created session."""
    return TaskCompletePayload(
        workflow_id=ctx.workflow_id,
        summary=summary,
        workspace_baseline=ctx.task.workspace_baseline,
    )


def _truncate(text: str, limit: int) -> str:
    """Return *text* capped at *limit* characters."""
    return text[:limit]
