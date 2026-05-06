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

Per-workflow state lives in a registry of :class:`WorkflowContext`
objects, keyed by ``workflow_id``.  ``delegate_task`` registers a
context before sending ``task.assign``; the dispatcher deregisters
on terminal arrival (``task.complete`` after finalisation, or
``task.error``).

This module replaces the per-delegation ``bus.request()`` /
"reply on the same future" pattern that was the Phase 3a interim:
``DelegationManager.delegate`` now sends ``task.assign`` and returns
immediately.  The dispatcher is the single owner of ``bus.listen()``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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
    from nemoclaw_escapades.orchestrator.finalization import FinalizationCoordinator
    from nemoclaw_escapades.orchestrator.workflow import (
        WorkflowContext,
        WorkflowRenderer,
    )

logger = get_logger("orchestrator.dispatcher")


class WorkflowDispatcher:
    """Single owner of the orchestrator's NMB listen queue.

    Lifecycle:

    1. ``await dispatcher.start()`` after the bus is connected.
    2. Each delegation calls
       :meth:`register_workflow` *before* it sends ``task.assign``
       (so a fast sub-agent can't race the registration).
    3. Inbound NMB messages arrive on the dispatcher's loop and are
       routed by ``type`` to the matching ``_handle_*`` method.
       Handlers run inline if they're cheap (audit ingest, error
       render); finalisation is forked into an independent
       ``asyncio.Task`` so a slow finalisation can't head-of-line-
       block ``audit.flush`` ingest for peer workflows.
    4. ``await dispatcher.close()`` cancels the loop on shutdown.
    """

    def __init__(
        self,
        bus: MessageBus,
        *,
        audit: AuditDB | None = None,
        finalizer: FinalizationCoordinator | None = None,
        renderer: WorkflowRenderer | None = None,
    ) -> None:
        """Wire the dispatcher to its collaborators.

        Args:
            bus: Connected NMB ``MessageBus``; the dispatcher owns
                ``listen()`` so no other code in the orchestrator
                process should consume from it.
            audit: Optional audit DB.  When ``None``, ``audit.flush``
                arrivals are logged and skipped.
            finalizer: Optional :class:`FinalizationCoordinator`.
                When ``None``, ``task.complete`` arrivals are logged
                and the renderer (if any) is asked to surface the
                payload directly.
            renderer: Optional connector-side push surface.  When
                ``None``, the dispatcher runs in headless mode —
                useful for tests; the orchestrator's ``main.py``
                always wires one in production.
        """
        self._bus = bus
        self._audit = audit
        self._finalizer = finalizer
        self._renderer = renderer

        self._workflows: dict[str, WorkflowContext] = {}
        # Background finalisation tasks.  Indexed by workflow_id so
        # we can cancel them on ``close()`` and so a duplicate
        # ``task.complete`` (NMB at-least-once replay; Phase 4)
        # doesn't fork two finalisations of the same workflow.
        self._finalization_tasks: dict[str, asyncio.Task[None]] = {}

        self._loop_task: asyncio.Task[None] | None = None
        self._loop_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the dispatch loop if it isn't already running.

        Idempotent.  Concurrent ``start()`` calls are guarded by
        :data:`_loop_lock` so two delegations racing through
        ``main.py`` setup can't both create a loop task.
        """
        async with self._loop_lock:
            if self._loop_task is None or self._loop_task.done():
                self._loop_task = asyncio.create_task(
                    self._dispatch_loop(),
                    name="workflow-dispatcher",
                )

    async def close(self) -> None:
        """Cancel the dispatch loop and any running finalisations.

        Safe to call multiple times.  Pending finalisation tasks are
        cancelled and awaited so the asyncio loop can shut down
        cleanly without ``Task was destroyed but it is pending!``
        warnings.
        """
        finalizers = list(self._finalization_tasks.values())
        for task in finalizers:
            task.cancel()
        for task in finalizers:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — defensive; finalisation surface is broad
                logger.warning("Finalisation task raised during close", exc_info=True)
        self._finalization_tasks.clear()

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

        Replaces an existing registration of the same id.  Note: the
        re-delegation path does **not** call this method — it mutates
        the registered :class:`WorkflowContext` in place
        (see :meth:`tools.finalization.FinalizationSession.re_delegate`)
        so the same object the dispatcher holds picks up the new
        iteration's task without an extra registry round-trip.  This
        method is reserved for genuinely new workflows (the original
        ``delegate_task`` invocation).
        """
        self._workflows[context.workflow_id] = context

    def deregister_workflow(self, workflow_id: str) -> None:
        """Remove the workflow registration after a terminal outcome.

        Idempotent: missing workflows are silently ignored so
        late-arriving duplicate frames don't error.
        """
        self._workflows.pop(workflow_id, None)

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
        """Validate ``task.complete`` and fork a finalisation task."""
        try:
            payload = load(TaskCompletePayload, TASK_COMPLETE, msg.payload)
        except PayloadValidationError:
            logger.warning(
                "Discarding malformed task.complete",
                extra={"from": msg.from_sandbox},
                exc_info=True,
            )
            return
        ctx = self._workflows.get(payload.workflow_id)
        if ctx is None:
            logger.warning(
                "task.complete for unknown workflow; dropping",
                extra={"workflow_id": payload.workflow_id, "from": msg.from_sandbox},
            )
            return
        # Forking the finalisation lets concurrent workflows finalise
        # in parallel (§8.2) and prevents a slow finalisation from
        # head-of-line-blocking peer workflows' ``audit.flush`` ingest.
        existing = self._finalization_tasks.get(payload.workflow_id)
        if existing is not None and not existing.done():
            logger.info(
                "Ignoring duplicate task.complete; finalisation already in flight",
                extra={"workflow_id": payload.workflow_id},
            )
            return
        self._finalization_tasks[payload.workflow_id] = asyncio.create_task(
            self._finalize(ctx, payload),
            name=f"finalize-{payload.workflow_id}",
        )

    async def _finalize(
        self,
        ctx: WorkflowContext,
        complete: TaskCompletePayload,
    ) -> None:
        """Run finalisation for one workflow.

        Conditional deregistration:

        - **On exception** the workflow is deregistered — the user
          saw the failure rendering and there's nothing left to act
          on.
        - **On success** the chosen tool's ``is_terminal`` flag
          decides:

          * ``present_work_to_user`` keeps the workflow registered
            because the user still has Push / Iterate / Discard
            buttons in their thread; clicking them needs to find
            the live :class:`WorkflowContext`.
          * ``re_delegate`` keeps the workflow registered because
            iteration 2's ``task.complete`` arrives on the same
            ``workflow_id`` and would otherwise be dropped as
            "unknown workflow".
          * ``push_branch`` / ``push_and_create_pr`` /
            ``discard_work`` / ``destroy_sandbox`` are terminal —
            the workflow is done and the registration is freed.

        The completed :class:`asyncio.Task` is **left** in
        :data:`_finalization_tasks`.  ``_handle_task_complete``
        checks ``existing.done()`` before forking a peer, so an
        iteration-2 ``task.complete`` for the same workflow id
        proceeds normally.  Memory is bounded by ``close()``.
        """
        try:
            if self._finalizer is None:
                # No finalisation wired (e.g. tests); render the raw
                # payload directly so the user still sees something.
                # No registered workflow to deregister either.
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
            self.deregister_workflow(ctx.workflow_id)
            return
        if result.is_terminal:
            self.deregister_workflow(ctx.workflow_id)

    async def _handle_task_error(self, msg: NMBMessage) -> None:
        """Validate ``task.error`` and surface it to the user."""
        try:
            payload = load(TaskErrorPayload, TASK_ERROR, msg.payload)
        except PayloadValidationError:
            logger.warning(
                "Discarding malformed task.error",
                extra={"from": msg.from_sandbox},
                exc_info=True,
            )
            return
        ctx = self._workflows.get(payload.workflow_id)
        if ctx is None:
            logger.warning(
                "task.error for unknown workflow; dropping",
                extra={"workflow_id": payload.workflow_id, "from": msg.from_sandbox},
            )
            return
        if self._renderer is not None:
            try:
                await self._renderer.render_workflow_error(context=ctx, error=payload)
            except Exception:  # noqa: BLE001 — connector surface is broad
                logger.warning(
                    "Renderer raised on task.error",
                    extra={"workflow_id": payload.workflow_id},
                    exc_info=True,
                )
        self.deregister_workflow(payload.workflow_id)

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
        timeout: float = 5.0,
    ) -> bool:
        """Block until *workflow_id*'s finalisation task finishes.

        Polls :data:`_finalization_tasks` for the workflow's task to
        appear (the dispatcher's loop forks it asynchronously after
        ``task.complete`` arrives), then awaits the task.

        Returns ``True`` if the task is already done or finishes
        within *timeout*; ``False`` if no task ever appeared.  Used
        by tests to synchronise without sleeping.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        task = self._finalization_tasks.get(workflow_id)
        while task is None:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)
            task = self._finalization_tasks.get(workflow_id)
        if task.done():
            return True
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except (TimeoutError, asyncio.CancelledError):
            return False
        return True

    @property
    def in_flight_finalizations(self) -> int:
        """Number of finalisation tasks currently running (not yet done)."""
        return sum(1 for t in self._finalization_tasks.values() if not t.done())


__all__ = ["WorkflowDispatcher"]
