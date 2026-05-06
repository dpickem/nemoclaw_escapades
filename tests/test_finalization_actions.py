"""Tests for :class:`FinalizationActionHandler`.

The handler routes Slack action-button clicks (Push & PR, Iterate,
Discard) to the matching :class:`FinalizationSession` method and
manages workflow registration in the dispatcher.

Critical contract: the handler must read ``session.state.is_terminal``
to decide whether to deregister the workflow after a tool call.  A
recoverable failure (network blip on push, safety-check refusal on
discard) leaves the workflow registered so the user can retry from
the same buttons.  Deregistering on every call would dead-lock the
buttons after one failure.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from nemoclaw_escapades.models.types import (
    FINALIZATION_ACTION_DISCARD,
    FINALIZATION_ACTION_ITERATE,
    FINALIZATION_ACTION_PUSH_PR,
    ActionPayload,
    NormalizedRequest,
)
from nemoclaw_escapades.nmb.protocol import (
    TaskAssignPayload,
    TaskCompletePayload,
)
from nemoclaw_escapades.orchestrator.finalization_actions import (
    FinalizationActionHandler,
    is_finalization_action,
)
from nemoclaw_escapades.orchestrator.workflow import WorkflowContext
from nemoclaw_escapades.tools.finalization import FinalizationSession


def _task() -> TaskAssignPayload:
    return TaskAssignPayload(
        prompt="fix things",
        workflow_id="wf-1",
        parent_sandbox_id="orchestrator",
        agent_id="coding-abcdef02",
        workspace_root="/tmp/wf",
    )


def _request(action_id: str) -> NormalizedRequest:
    return NormalizedRequest(
        text="",
        user_id="U1",
        channel_id="C1",
        timestamp=0.0,
        source="slack",
        thread_ts="T1",
        action=ActionPayload(action_id=action_id, value="wf-1"),
    )


class _FakeDispatcher:
    """Minimal dispatcher stub recording register / deregister calls."""

    def __init__(self, ctx: WorkflowContext | None = None) -> None:
        self._ctx = ctx
        self.deregistered: list[str] = []

    def get_workflow(self, workflow_id: str) -> WorkflowContext | None:
        if self._ctx is not None and self._ctx.workflow_id == workflow_id:
            return self._ctx
        return None

    def deregister_workflow(self, workflow_id: str) -> None:
        self.deregistered.append(workflow_id)

    def register_workflow(self, ctx: WorkflowContext) -> None:
        self._ctx = ctx


def _session_factory_returning(
    state_is_terminal: bool,
    state_message: str = "ok",
) -> Callable[[WorkflowContext, TaskCompletePayload], FinalizationSession]:
    """Build a session factory whose tools immediately stamp ``state``.

    The handler reads ``session.state.is_terminal`` after the tool
    method returns, so we stub the tool methods to set the terminal
    flag we want to exercise.
    """

    def _factory(
        ctx: WorkflowContext,
        complete: TaskCompletePayload,
    ) -> FinalizationSession:
        session = FinalizationSession(task=ctx.task, complete=complete, context=ctx)

        async def _push_and_create_pr(*_args: Any, **_kwargs: Any) -> str:
            session.state.action = "push_and_create_pr"
            session.state.message = state_message
            session.state.is_terminal = state_is_terminal
            return state_message

        async def _discard_work(*_args: Any, **_kwargs: Any) -> str:
            session.state.action = "discard_work"
            session.state.message = state_message
            session.state.is_terminal = state_is_terminal
            return state_message

        # Override the bound methods on the instance.
        session.push_and_create_pr = _push_and_create_pr  # type: ignore[method-assign]
        session.discard_work = _discard_work  # type: ignore[method-assign]
        return session

    return _factory


# ── is_finalization_action ─────────────────────────────────────────


class TestIsFinalizationAction:
    @pytest.mark.parametrize(
        "action_id",
        [
            FINALIZATION_ACTION_PUSH_PR,
            FINALIZATION_ACTION_ITERATE,
            FINALIZATION_ACTION_DISCARD,
        ],
    )
    def test_recognised_action_ids(self, action_id: str) -> None:
        assert is_finalization_action(_request(action_id)) is True

    def test_other_action_id_rejected(self) -> None:
        assert is_finalization_action(_request("approve_write")) is False

    def test_request_without_action_rejected(self) -> None:
        request = NormalizedRequest(
            text="hi",
            user_id="U1",
            channel_id="C1",
            timestamp=0.0,
            source="slack",
        )
        assert is_finalization_action(request) is False


# ── Push & PR ─────────────────────────────────────────────────────


class TestHandlePushPr:
    @pytest.mark.asyncio
    async def test_terminal_success_deregisters(self) -> None:
        ctx = WorkflowContext(workflow_id="wf-1", task=_task())
        dispatcher = _FakeDispatcher(ctx=ctx)
        handler = FinalizationActionHandler(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            session_factory=_session_factory_returning(
                state_is_terminal=True,
                state_message="https://github.com/example/pr/1",
            ),
        )
        response = await handler.handle(_request(FINALIZATION_ACTION_PUSH_PR))
        # Deregistered after success — no stale buttons should fire.
        assert dispatcher.deregistered == ["wf-1"]
        assert "https://github.com/example/pr/1" in str(response.blocks[0])

    @pytest.mark.asyncio
    async def test_recoverable_failure_keeps_workflow_alive(self) -> None:
        """Regression: a non-terminal tool result must NOT deregister.

        Before the fix, the handler unconditionally deregistered after
        any successful return — including the tool's own ``Error: ...``
        sentinel for recoverable failures.  Subsequent button clicks
        then dead-locked with "Workflow is no longer active".
        """
        ctx = WorkflowContext(workflow_id="wf-1", task=_task())
        dispatcher = _FakeDispatcher(ctx=ctx)
        handler = FinalizationActionHandler(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            session_factory=_session_factory_returning(
                state_is_terminal=False,
                state_message="Error: network blip",
            ),
        )
        await handler.handle(_request(FINALIZATION_ACTION_PUSH_PR))
        assert dispatcher.deregistered == [], (
            "recoverable Push & PR failure must keep the workflow "
            "registered so the user can retry the click"
        )
        # And the workflow context is still resolvable for the
        # next click.
        assert dispatcher.get_workflow("wf-1") is ctx

    @pytest.mark.asyncio
    async def test_unknown_workflow_returns_friendly_message(self) -> None:
        dispatcher = _FakeDispatcher(ctx=None)
        handler = FinalizationActionHandler(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            session_factory=_session_factory_returning(state_is_terminal=True),
        )
        response = await handler.handle(_request(FINALIZATION_ACTION_PUSH_PR))
        assert dispatcher.deregistered == []
        body = str(response.blocks[0])
        assert "no longer active" in body


# ── Discard ───────────────────────────────────────────────────────


class TestHandleDiscard:
    @pytest.mark.asyncio
    async def test_terminal_success_deregisters(self) -> None:
        ctx = WorkflowContext(workflow_id="wf-1", task=_task())
        dispatcher = _FakeDispatcher(ctx=ctx)
        handler = FinalizationActionHandler(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            session_factory=_session_factory_returning(
                state_is_terminal=True,
                state_message="Discarded delegated workspace at /tmp/wf.",
            ),
        )
        await handler.handle(_request(FINALIZATION_ACTION_DISCARD))
        assert dispatcher.deregistered == ["wf-1"]

    @pytest.mark.asyncio
    async def test_safety_refusal_keeps_workflow_alive(self) -> None:
        """Regression: ``discard_work`` safety refusal must not deregister.

        The tool intentionally leaves ``is_terminal=False`` when the
        workspace path doesn't look like an agent path; the handler
        must respect that.
        """
        ctx = WorkflowContext(workflow_id="wf-1", task=_task())
        dispatcher = _FakeDispatcher(ctx=ctx)
        handler = FinalizationActionHandler(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            session_factory=_session_factory_returning(
                state_is_terminal=False,
                state_message="Error: refusing to discard non-agent path /weird",
            ),
        )
        await handler.handle(_request(FINALIZATION_ACTION_DISCARD))
        assert dispatcher.deregistered == []
        assert dispatcher.get_workflow("wf-1") is ctx


# ── Iterate ───────────────────────────────────────────────────────


class TestHandleIterate:
    @pytest.mark.asyncio
    async def test_iterate_primes_pending_state_only(self) -> None:
        ctx = WorkflowContext(workflow_id="wf-1", task=_task())
        dispatcher = _FakeDispatcher(ctx=ctx)
        handler = FinalizationActionHandler(
            dispatcher=dispatcher,  # type: ignore[arg-type]
            session_factory=_session_factory_returning(state_is_terminal=False),
        )
        request = _request(FINALIZATION_ACTION_ITERATE)
        await handler.handle(request)
        # No deregistration: iterate is non-terminal and waits on
        # the user's next text message.
        assert dispatcher.deregistered == []
        thread_key = request.thread_ts or request.request_id
        assert handler.is_pending_iteration(thread_key) is True
