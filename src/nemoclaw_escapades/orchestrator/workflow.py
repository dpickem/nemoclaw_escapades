"""Workflow-level types shared by the orchestrator's delegation flow.

A workflow is everything tied to one ``workflow_id``: the current
``TaskAssignPayload``, the originating channel/thread, and iteration state.  The
dispatcher uses this metadata to route asynchronous ``task.*`` messages and to
render finalisation results back to the right user thread.

This module stays lightweight so connector renderers can implement
``WorkflowRenderer`` without importing the whole orchestrator stack.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from nemoclaw_escapades.nmb.protocol import (
    TaskAssignPayload,
    TaskCompletePayload,
    TaskErrorPayload,
    TaskProgressPayload,
)


@dataclass
class WorkflowContext:
    """All workflow-scoped state the dispatcher needs to route arrivals.

    Re-delegation mutates ``task`` in place so the dispatcher sees the
    latest prompt and iteration number for the next ``task.complete``.
    """

    # UUID stamped on all NMB messages for this workflow.
    workflow_id: str
    # Latest task assignment; updated in place by re-delegation.
    task: TaskAssignPayload
    # Connector channel id, e.g. Slack channel; None for headless tests/CLI.
    channel_id: str | None = None
    # Connector thread timestamp or parent id inside channel_id.
    thread_ts: str | None = None
    # Original user request id for logs and trace correlation.
    request_id: str = ""
    # Workflow registration time; not reset by later iterations.
    started_at: float = field(default_factory=time.time)


@runtime_checkable
class WorkflowRenderer(Protocol):
    """Connector-side surface the dispatcher / finalizer push results through.

    Slack implements this today; future connectors can provide the same async
    methods so dispatcher/finalization code remains platform-neutral.
    """

    async def render_present_work(
        self,
        *,
        context: WorkflowContext,
        summary: str,
        diff: str,
    ) -> None:
        """Post synthesized work and action buttons to the originating thread.

        ``summary`` is already produced by the finalisation model. ``diff`` may
        be empty when the finalisation tool chose to omit it.
        """
        ...

    async def render_finalization_action(
        self,
        *,
        context: WorkflowContext,
        action: str,
        result: str,
    ) -> None:
        """Post the outcome of a finalisation tool back to the thread.

        ``action`` is the tool name and ``result`` is the tool's textual output
        such as a PR URL or discard message.
        """
        ...

    async def render_workflow_progress(
        self,
        *,
        context: WorkflowContext,
        progress: TaskProgressPayload,
    ) -> None:
        """Surface a sub-agent ``task.progress`` update to the thread.

        Implementations usually translate this into a concise thinking
        indicator or status line.
        """
        ...

    async def render_workflow_error(
        self,
        *,
        context: WorkflowContext,
        error: TaskErrorPayload,
    ) -> None:
        """Tell the user the sub-agent failed.

        Called when the dispatcher receives ``task.error``.  Finalisation does
        not run for failed sub-agent tasks.
        """
        ...

    async def render_workflow_completion_failure(
        self,
        *,
        context: WorkflowContext,
        complete: TaskCompletePayload,
        error: str,
    ) -> None:
        """Surface a finalisation-side failure back to the user.

        This is distinct from ``task.error``: the sub-agent completed, but the
        orchestrator failed while checking or presenting the result.
        """
        ...
