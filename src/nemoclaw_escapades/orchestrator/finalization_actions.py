"""Platform-neutral routing for finalisation button clicks.

The connector layer (Slack today, more later) emits a
:class:`NormalizedRequest` with ``action.action_id`` set to one of
:data:`FINALIZATION_ACTION_PUSH_PR` / ``_ITERATE`` / ``_DISCARD`` when
the user clicks a finalisation button.  The
:class:`Orchestrator.handle` method spots the click and forwards it
to :meth:`FinalizationActionHandler.handle`, which:

- looks up the live :class:`WorkflowContext` via the dispatcher,
- builds a per-click :class:`FinalizationSession`,
- runs the matching tool method directly (``push_and_create_pr``,
  ``discard_work``, or ``re_delegate``),
- returns a :class:`RichResponse` for the connector to render in
  the originating thread.

Iteration is special: a click only *primes* the handler — the
user's next text message in the thread becomes the
``re_delegate`` prompt.  See :meth:`is_pending_iteration` /
:meth:`consume_iteration_feedback`.
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
from nemoclaw_escapades.observability.logging import get_logger

if TYPE_CHECKING:
    from nemoclaw_escapades.nmb.protocol import TaskCompletePayload
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager
    from nemoclaw_escapades.orchestrator.dispatcher import WorkflowDispatcher
    from nemoclaw_escapades.orchestrator.workflow import WorkflowContext, WorkflowRenderer
    from nemoclaw_escapades.tools.finalization import FinalizationSession

logger = get_logger("orchestrator.finalization_actions")

#: Builds a :class:`FinalizationSession` for one workflow / completion
#: pair.  Defaulted in :meth:`FinalizationActionHandler.__init__`; the
#: orchestrator's ``main.py`` wires a custom factory if it wants to
#: bind a delegation manager / renderer onto each session.
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

    Constructed once in ``main.py`` (next to the dispatcher and
    finalization coordinator).  All methods are async because the
    underlying :class:`FinalizationSession` tools are async — git
    pushes, ``gh pr create``, file deletion all await.
    """

    def __init__(
        self,
        *,
        dispatcher: WorkflowDispatcher,
        session_factory: SessionFactory | None = None,
        delegation_manager: DelegationManager | None = None,
        renderer: WorkflowRenderer | None = None,
    ) -> None:
        """Wire the handler.

        Args:
            dispatcher: Used to look up the live
                :class:`WorkflowContext` for the clicked workflow.
            session_factory: Optional override for building a
                :class:`FinalizationSession`.  Defaults to a factory
                that constructs sessions bound to *delegation_manager*
                and *renderer*.  Tests inject a stub.
            delegation_manager: Required for the Iterate path
                (``re_delegate`` fires a follow-up ``task.assign``).
            renderer: Connector-side renderer used by
                :meth:`FinalizationSession.push_branch` etc. so the
                tool's structured output reaches the user's thread.
        """
        self._dispatcher = dispatcher
        self._delegation_manager = delegation_manager
        self._renderer = renderer
        self._session_factory: SessionFactory = (
            session_factory or self._build_default_session_factory()
        )
        # Threads awaiting the user's iteration feedback.  Keyed by
        # ``thread_key`` (``thread_ts`` if set, else ``request_id``)
        # so a message arriving in the same thread is routed to the
        # ``re_delegate`` path.  Maps thread key → workflow id.
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
        """Route an inbound finalisation-action click to the right tool."""
        action = request.action
        assert action is not None  # caller gates on is_finalization_action
        workflow_id = action.value
        ctx = self._dispatcher.get_workflow(workflow_id)
        if ctx is None:
            return _text_reply(
                request,
                f"Workflow `{workflow_id}` is no longer active "
                "(probably already finalised or aged out).",
            )
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

        Looks up the pending workflow id, fires ``re_delegate`` via
        the finalisation session, and clears the pending entry.
        """
        workflow_id = self._pending_iteration.pop(thread_key, None)
        if workflow_id is None:
            return _text_reply(
                request,
                "No pending iteration; treating this as a fresh request.",
            )
        ctx = self._dispatcher.get_workflow(workflow_id)
        if ctx is None:
            return _text_reply(
                request,
                f"Workflow `{workflow_id}` is no longer active.",
            )
        from nemoclaw_escapades.nmb.protocol import TaskCompletePayload

        complete_stub = TaskCompletePayload(
            workflow_id=ctx.workflow_id,
            summary=f"Iterating on workflow {ctx.workflow_id} per user feedback.",
            workspace_baseline=ctx.task.workspace_baseline,
        )
        session = self._session_factory(ctx, complete_stub)
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
        from nemoclaw_escapades.nmb.protocol import TaskCompletePayload

        complete_stub = TaskCompletePayload(
            workflow_id=ctx.workflow_id,
            summary=f"Finalize {ctx.task.prompt[:120]}",
            workspace_baseline=ctx.task.workspace_baseline,
        )
        session = self._session_factory(ctx, complete_stub)
        branch = f"finalize/{ctx.workflow_id}"
        title = ctx.task.prompt[:80]
        # Run the actual git ops inside an asyncio.shield so a Slack
        # click that times out doesn't leave a half-pushed branch.
        try:
            result = await asyncio.shield(
                session.push_and_create_pr(branch_name=branch, title=title)
            )
        except Exception as exc:  # noqa: BLE001 — surface to user, don't crash the connector
            logger.warning("push_and_create_pr failed", exc_info=True)
            return _text_reply(request, f"Push & PR failed: {exc}")
        # Only deregister when the tool reported a terminal outcome
        # (push + ``gh pr create`` both succeeded).  A recoverable
        # error leaves the workflow registered so the user can retry
        # the click, switch to Iterate / Discard, or wait out a
        # transient broker / git hiccup.
        if session.state.is_terminal:
            self._dispatcher.deregister_workflow(ctx.workflow_id)
        return _text_reply(request, result)

    async def _handle_discard(
        self,
        request: NormalizedRequest,
        ctx: WorkflowContext,
    ) -> RichResponse:
        from nemoclaw_escapades.nmb.protocol import TaskCompletePayload

        complete_stub = TaskCompletePayload(
            workflow_id=ctx.workflow_id,
            summary="Discarding workflow per user request.",
            workspace_baseline=ctx.task.workspace_baseline,
        )
        session = self._session_factory(ctx, complete_stub)
        try:
            result = await session.discard_work("Discarded by user")
        except Exception as exc:  # noqa: BLE001 — surface to user, don't crash the connector
            logger.warning("discard_work failed", exc_info=True)
            return _text_reply(request, f"Discard failed: {exc}")
        # Same logic as Push & PR: ``discard_work`` only sets
        # ``is_terminal`` when it actually deleted the workspace.
        # The safety-check refusal path (non-agent path) leaves the
        # workflow alive so the user can investigate or retry.
        if session.state.is_terminal:
            self._dispatcher.deregister_workflow(ctx.workflow_id)
        return _text_reply(request, result)

    def _handle_iterate(
        self,
        request: NormalizedRequest,
        ctx: WorkflowContext,
    ) -> RichResponse:
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


__all__ = [
    "FinalizationActionHandler",
    "SessionFactory",
    "is_finalization_action",
]
