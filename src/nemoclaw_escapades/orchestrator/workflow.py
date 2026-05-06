"""Workflow-level types shared by the orchestrator's delegation flow.

A *workflow* is everything tied to one ``workflow_id`` — the original
``TaskAssignPayload``, the originating channel and thread, and any
iteration state.  The orchestrator keeps this bag of metadata on the
side so the dispatcher (``orchestrator/dispatcher.py``) can look it up
when ``task.complete`` / ``task.error`` / ``task.progress`` arrive
asynchronously, and so the finalisation flow can render results back
to the right Slack thread.

This module is dependency-free w.r.t. the rest of the package so that
``WorkflowRenderer`` can be implemented by connectors
(``connectors/slack/finalization.py``) without dragging the
orchestrator's whole import graph into a connector unit test.
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

    Created by the orchestrator's ``delegate_task`` tool when it
    sends ``task.assign`` and registered with the
    :class:`WorkflowDispatcher` keyed by ``workflow_id``.

    Attributes:
        workflow_id: UUID stamped on every NMB message for this run.
        task: The most recent ``TaskAssignPayload`` for this workflow.
            Mutated in place by
            :meth:`tools.finalization.FinalizationSession.re_delegate`
            so iteration N+1's :class:`FinalizationSession` reads the
            new prompt / ``iteration_number`` instead of the original.
        channel_id: Connector channel identity (e.g. Slack channel ID)
            — the renderer uses this to address user-facing posts.
            ``None`` means "no connector context" (CLI invocations,
            unit tests).
        thread_ts: Thread / parent timestamp inside *channel_id*.
            ``None`` posts to channel root.
        request_id: Original orchestrator request ID — useful for
            structured logging that needs to correlate the user's
            Slack request with the eventual finalisation post.
        started_at: Unix epoch seconds when the workflow was registered.
            Used for stale-workflow GC and operator-facing dashboards.
            **Not** updated on re-delegation — it marks workflow
            start, not iteration start.
    """

    workflow_id: str
    task: TaskAssignPayload
    channel_id: str | None = None
    thread_ts: str | None = None
    request_id: str = ""
    started_at: float = field(default_factory=time.time)


@runtime_checkable
class WorkflowRenderer(Protocol):
    """Connector-side surface the dispatcher / finalizer push results through.

    Each connector (Slack today, more later) implements this Protocol
    so the orchestrator stays platform-neutral.  The renderer never
    raises into the dispatcher — implementations swallow connector
    errors and log internally.

    All methods are async because the typical implementation hits
    the platform's async SDK (``slack-bolt``'s ``AsyncWebClient``).
    """

    async def render_present_work(
        self,
        *,
        context: WorkflowContext,
        summary: str,
        diff: str,
    ) -> None:
        """Post the synthesised work + action buttons to the originating thread.

        Args:
            context: Workflow context (channel / thread).
            summary: User-facing summary (already synthesised by the
                finalisation model).
            diff: Optional pre-truncated diff body.  Empty when the
                finalisation tool elected to omit the diff.
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

        ``action`` is the tool name (``"push_and_create_pr"``,
        ``"discard_work"``, etc.); the renderer uses it to pick a
        cosmetic icon / formatter.  ``result`` is the tool's textual
        output (PR URL, "Discarded …", etc.).
        """
        ...

    async def render_workflow_progress(
        self,
        *,
        context: WorkflowContext,
        progress: TaskProgressPayload,
    ) -> None:
        """Surface a sub-agent ``task.progress`` update to the thread.

        Best-effort: the dispatcher swallows renderer errors here so
        a transient connector glitch can't kill the dispatcher.
        Implementations typically translate the typed progress into
        a single thinking-indicator update; verbose progress posting
        is intentionally not part of the renderer contract.
        """
        ...

    async def render_workflow_error(
        self,
        *,
        context: WorkflowContext,
        error: TaskErrorPayload,
    ) -> None:
        """Tell the user the sub-agent failed.

        Called when the dispatcher receives ``task.error``.  The
        finalisation flow does *not* run for errors — the user sees
        the structured failure directly so they can decide whether
        to retry, file a bug, or rephrase.
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

        Called when the dispatcher caught ``task.complete`` but the
        finalisation flow itself raised (baseline drift, finalisation
        ``AgentLoop`` blew up, etc.).  Distinct from
        :meth:`render_workflow_error` because the *sub-agent*
        succeeded; the failure is on the orchestrator's side and
        the user may still want to inspect the diff.
        """
        ...
