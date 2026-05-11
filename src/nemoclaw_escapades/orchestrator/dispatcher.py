"""Centralised NMB event-loop dispatcher (design §8.2).

The orchestrator listens for inbound NMB messages on a single
background ``asyncio.Task`` owned by :class:`WorkflowDispatcher`.
The dispatcher routes each message to a per-type handler based on
the message ``type`` field:

- ``task.complete`` → kick off finalisation as an independent task
  (so concurrent workflows finalise concurrently, design §8.2).
- ``task.error`` → render the failure to the originating thread.
- ``task.progress`` → forward to the connector for thinking-indicator
  updates.
- ``audit.flush`` → ingest into the central audit DB.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from nemoclaw_escapades.nmb.protocol import (
    AUDIT_FLUSH,
    TASK_COMPLETE,
    TASK_ERROR,
    TASK_PROGRESS,
    AuditFlushPayload,
    PayloadValidationError,
    TaskCompletePayload,
    TaskErrorPayload,
    TaskProgressPayload,
    load,
)
from nemoclaw_escapades.observability.logging import get_logger

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB
    from nemoclaw_escapades.nmb.client import MessageBus
    from nemoclaw_escapades.nmb.models import NMBMessage
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager
    from nemoclaw_escapades.orchestrator.finalization import FinalizationCoordinator
    from nemoclaw_escapades.orchestrator.workflow import (
        WorkflowContext,
        WorkflowRenderer,
    )

logger = get_logger("orchestrator.dispatcher")

# Default seconds tests wait for a finalization task to appear and finish.
_DEFAULT_FINALIZATION_WAIT_TIMEOUT_S: float = 5.0

# Seconds between test-helper polls for finalization task completion.
_FINALIZATION_WAIT_POLL_S: float = 0.02


class _FinalizationNotReadyError(Exception):
    """Internal retry sentinel for ``wait_for_finalization``."""


class WorkflowDispatcher:
    """Single owner of the orchestrator's NMB listen queue.

    The dispatcher keeps per-workflow context, forwards progress and
    errors to the renderer, and ingests audit flushes.  Finalisation
    runs in background tasks so slow workflows do not block other
    inbound NMB traffic.
    """

    def __init__(
        self,
        bus: MessageBus,
        *,
        audit: AuditDB | None = None,
        finalizer: FinalizationCoordinator | None = None,
        renderer: WorkflowRenderer | None = None,
        delegation_manager: DelegationManager | None = None,
    ) -> None:
        """Wire the dispatcher to its collaborators.

        Args:
            bus: Connected NMB bus; this class owns ``listen()``.
            audit: Optional central audit DB.
            finalizer: Optional model-driven finalization coordinator.
            renderer: Optional connector-side push surface.
            delegation_manager: Optional spawned-agent lifecycle manager.
        """
        # Connected NMB client; this dispatcher is the sole listen() consumer.
        self._bus = bus
        # Central audit DB for audit.flush and delegation terminal status.
        self._audit = audit
        # Model-driven finalization runner for task.complete payloads.
        self._finalizer = finalizer
        # Connector-facing renderer for Slack/headless workflow updates.
        self._renderer = renderer
        # Delegation lifecycle manager used to terminate completed workflows.
        self._delegation_manager = delegation_manager

        # Per-workflow metadata keyed by workflow_id.
        self._workflows: dict[str, WorkflowContext] = {}
        # One finalisation task per workflow id; prevents duplicate
        # in-flight finalisations and makes shutdown cancellation easy.
        self._finalization_tasks: dict[str, asyncio.Task[None]] = {}

        # Background task running the bus listen loop.
        self._loop_task: asyncio.Task[None] | None = None
        # Guards start() so concurrent callers do not spawn multiple loops.
        self._loop_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the dispatch loop if it isn't already running.

        Idempotent under concurrent callers.  Once started, this
        instance owns the bus listen queue until :meth:`close`.
        """
        async with self._loop_lock:
            if self._loop_task is None or self._loop_task.done():
                self._loop_task = asyncio.create_task(
                    self._dispatch_loop(),
                    name="workflow-dispatcher",
                )

    async def close(self) -> None:
        """Cancel the dispatch loop and any running finalisations.

        Safe to call multiple times.  Finalisation tasks are awaited
        after cancellation so shutdown does not leave pending tasks.
        """
        finalizers = list(self._finalization_tasks.values())

        # Cancel all finalisation tasks; best-effort.
        for task in finalizers:
            task.cancel()

        # Await all finalisation tasks; cancellation is best-effort.
        for task in finalizers:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — defensive; finalisation surface is broad
                logger.warning("Finalisation task raised during close", exc_info=True)

        self._finalization_tasks.clear()

        # Cancel the bus listen loop; best-effort.
        loop_task = self._loop_task
        if loop_task is None or loop_task.done():
            return
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Workflow registry
    # ------------------------------------------------------------------

    def register_workflow(self, context: WorkflowContext) -> None:
        """Track *context* under ``context.workflow_id``.

        Re-delegation mutates the existing context in place; this is
        for new workflows only.  Callers should register before
        sending ``task.assign`` so fast completions do not race setup.
        """
        self._workflows[context.workflow_id] = context

    async def deregister_workflow(self, workflow_id: str) -> None:
        """Remove workflow state and terminate its spawned agent.

        Idempotent for unknown workflow ids.  The registry entry is
        removed before awaiting teardown so late frames see the
        workflow as closed immediately.
        """
        self._workflows.pop(workflow_id, None)
        if self._delegation_manager is not None:
            await self._delegation_manager.terminate(workflow_id)

    def get_workflow(self, workflow_id: str) -> WorkflowContext | None:
        """Look up a registered workflow."""
        return self._workflows.get(workflow_id)

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        """Consume the bus's listen queue and route each delivery."""
        try:
            async for msg in self._bus.listen():
                try:
                    await self._dispatch(msg)
                except Exception:  # noqa: BLE001 — never let one msg kill the loop
                    logger.warning(
                        "Dispatcher handler raised",
                        extra={"type": msg.type, "from": msg.from_sandbox},
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — keep the dispatcher resilient
            logger.error(
                "Dispatcher loop exited; inbound NMB messages will not be processed",
                exc_info=True,
            )

    async def _dispatch(self, msg: NMBMessage) -> None:
        """Route one delivery to its type-specific handler."""
        if msg.type == TASK_COMPLETE:
            await self._handle_task_complete(msg)
        elif msg.type == TASK_ERROR:
            await self._handle_task_error(msg)
        elif msg.type == TASK_PROGRESS:
            await self._handle_task_progress(msg)
        elif msg.type == AUDIT_FLUSH:
            await self._handle_audit_flush(msg)
        else:
            logger.debug(
                "Ignoring unknown message type",
                extra={"type": msg.type, "from": msg.from_sandbox},
            )

    # ------------------------------------------------------------------
    # Per-type handlers
    # ------------------------------------------------------------------

    async def _handle_task_complete(self, msg: NMBMessage) -> None:
        """Validate ``task.complete`` and fork a finalisation task.

        Duplicate completions are ignored while finalisation is still
        running for the same workflow.  Completed finalisation tasks
        may be replaced by later iteration results.
        """
        # Validate the payload.
        try:
            payload = load(TaskCompletePayload, TASK_COMPLETE, msg.payload)
        except PayloadValidationError:
            logger.warning(
                "Discarding malformed task.complete",
                extra={"from": msg.from_sandbox},
                exc_info=True,
            )
            return

        # Look up the workflow context for this completion.
        ctx = self._workflows.get(payload.workflow_id)
        if ctx is None:
            logger.warning(
                "task.complete for unknown workflow; dropping",
                extra={"workflow_id": payload.workflow_id, "from": msg.from_sandbox},
            )
            return

        # Finalisation may call tools or Slack; keep the listen loop free.
        existing = self._finalization_tasks.get(payload.workflow_id)
        if existing is not None and not existing.done():
            logger.info(
                "Ignoring duplicate task.complete; finalisation already in flight",
                extra={"workflow_id": payload.workflow_id},
            )
            return

        # Fork a finalisation task; it will call tools or Slack.
        self._finalization_tasks[payload.workflow_id] = asyncio.create_task(
            self._finalize(ctx, payload),
            name=f"finalize-{payload.workflow_id}",
        )

    async def _finalize(
        self,
        ctx: WorkflowContext,
        complete: TaskCompletePayload,
    ) -> None:
        """Run finalisation and deregister terminal workflows.

        Non-terminal actions such as presenting work or re-delegating
        keep the workflow registered for the next button click or
        iteration result.
        """
        try:
            if self._finalizer is None:
                # Headless/test fallback: surface the raw payload.
                if self._renderer is not None:
                    await self._renderer.render_present_work(
                        context=ctx,
                        summary=complete.summary,
                        diff=complete.diff,
                    )
                return
            result = await self._finalizer.finalize(ctx, complete)
        except Exception as exc:  # noqa: BLE001 — surface to user, don't crash the dispatcher
            logger.error(
                "Finalisation failed",
                extra={"workflow_id": ctx.workflow_id},
                exc_info=True,
            )
            if self._renderer is not None:
                try:
                    await self._renderer.render_workflow_completion_failure(
                        context=ctx,
                        complete=complete,
                        error=str(exc),
                    )
                except Exception:  # noqa: BLE001 — connector surface is broad
                    logger.warning(
                        "Renderer raised on finalisation failure",
                        exc_info=True,
                    )
            await self.deregister_workflow(ctx.workflow_id)
            return
        if result.is_terminal:
            await self.deregister_workflow(ctx.workflow_id)

    async def _handle_task_error(self, msg: NMBMessage) -> None:
        """Validate ``task.error``, render it, and stamp audit.

        Rendering and audit writes are best-effort.  The workflow is
        deregistered after both attempts so cleanup still runs.
        """
        try:
            payload = load(TaskErrorPayload, TASK_ERROR, msg.payload)
        except PayloadValidationError:
            logger.warning(
                "Discarding malformed task.error",
                extra={"from": msg.from_sandbox},
                exc_info=True,
            )
            return

        # Look up the workflow context for this error.
        ctx = self._workflows.get(payload.workflow_id)
        if ctx is None:
            logger.warning(
                "task.error for unknown workflow; dropping",
                extra={"workflow_id": payload.workflow_id, "from": msg.from_sandbox},
            )
            return

        # Render the error to the originating thread.
        if self._renderer is not None:
            try:
                await self._renderer.render_workflow_error(context=ctx, error=payload)
            except Exception:  # noqa: BLE001 — connector surface is broad
                logger.warning(
                    "Renderer raised on task.error",
                    extra={"workflow_id": payload.workflow_id},
                    exc_info=True,
                )

        # Write the error to the audit DB.
        if self._audit is not None:
            try:
                await self._audit.log_delegation_error(
                    workflow_id=payload.workflow_id,
                    error_kind=payload.error_kind,
                    error_message=payload.error,
                    recoverable=payload.recoverable,
                )
            except Exception:  # noqa: BLE001 — DB surface is broad
                logger.warning(
                    "log_delegation_error failed",
                    extra={"workflow_id": payload.workflow_id},
                    exc_info=True,
                )

        await self.deregister_workflow(payload.workflow_id)

    async def _handle_task_progress(self, msg: NMBMessage) -> None:
        """Forward ``task.progress`` to the renderer; best-effort."""
        try:
            payload = load(TaskProgressPayload, TASK_PROGRESS, msg.payload)
        except PayloadValidationError:
            logger.debug(
                "Discarding malformed task.progress",
                extra={"from": msg.from_sandbox},
            )
            return

        # Look up the workflow context for this progress.
        ctx = self._workflows.get(payload.workflow_id)
        if ctx is None or self._renderer is None:
            return
        try:
            await self._renderer.render_workflow_progress(context=ctx, progress=payload)
        except Exception:  # noqa: BLE001 — progress is best-effort
            logger.debug(
                "Renderer raised on task.progress",
                extra={"workflow_id": payload.workflow_id},
                exc_info=True,
            )

    async def _handle_audit_flush(self, msg: NMBMessage) -> None:
        """Ingest a sub-agent ``audit.flush`` batch into the central DB."""
        if self._audit is None:
            return

        try:
            payload = load(AuditFlushPayload, AUDIT_FLUSH, msg.payload)
        except PayloadValidationError:
            logger.warning(
                "Discarding malformed audit.flush",
                extra={"from": msg.from_sandbox},
                exc_info=True,
            )
            return

        try:
            count = await self._audit.ingest_audit_flush(payload)
        except Exception:  # noqa: BLE001 — DB surface is broad
            logger.warning(
                "Failed to ingest audit.flush",
                extra={"workflow_id": payload.workflow_id},
                exc_info=True,
            )
            return

        logger.info(
            "Ingested sub-agent audit flush",
            extra={"workflow_id": payload.workflow_id, "tool_calls": count},
        )

    # ------------------------------------------------------------------
    # Test introspection
    # ------------------------------------------------------------------

    async def wait_for_finalization(
        self,
        workflow_id: str,
        *,
        timeout: float = _DEFAULT_FINALIZATION_WAIT_TIMEOUT_S,
    ) -> bool:
        """Block until *workflow_id*'s finalisation task finishes.

        Test helper. Returns ``False`` if the task never appears or
        does not finish within *timeout*.  The finalization task is
        never awaited directly, so a helper timeout cannot cancel it.
        """
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_FinalizationNotReadyError),
                wait=wait_fixed(_FINALIZATION_WAIT_POLL_S),
                stop=stop_after_delay(timeout),
                reraise=True,
            ):
                with attempt:
                    task = self._finalization_tasks.get(workflow_id)
                    if task is None or not task.done():
                        raise _FinalizationNotReadyError
        except _FinalizationNotReadyError:
            return False
        return True

    @property
    def in_flight_finalizations(self) -> int:
        """Number of finalisation tasks currently running (not yet done)."""
        return len([t for t in self._finalization_tasks.values() if not t.done()])
