"""Tests for the orchestrator's centralised :class:`WorkflowDispatcher`.

The dispatcher owns ``MessageBus.listen()`` and routes inbound NMB
messages to per-type handlers.  These tests stub the bus with a
scripted message stream and assert:

- ``audit.flush`` arrivals land in the audit DB exactly once
  (idempotent under replay).
- Concurrent workflows' flushes don't drop each other (regression
  for the silent-message-drop bug Phase 3a/early-3b had).
- ``task.complete`` for a registered workflow forks a finalisation
  task; ``task.complete`` for an unknown workflow is dropped.
- ``task.error`` and ``task.progress`` arrive at the renderer
  (best-effort) and the dispatcher stays alive across renderer
  exceptions.
- ``close()`` cancels in-flight finalisation tasks and the listen
  loop cleanly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.nmb.models import NMBMessage, Op
from nemoclaw_escapades.nmb.protocol import (
    AUDIT_FLUSH,
    TASK_COMPLETE,
    TASK_ERROR,
    TASK_PROGRESS,
    AuditFlushPayload,
    AuditToolCallPayload,
    TaskAssignPayload,
    TaskCompletePayload,
    TaskErrorPayload,
    TaskProgressPayload,
    dump,
)
from nemoclaw_escapades.orchestrator.dispatcher import WorkflowDispatcher
from nemoclaw_escapades.orchestrator.workflow import WorkflowContext

# ── Fakes ──────────────────────────────────────────────────────────


class _ScriptedBus:
    """Fake :class:`MessageBus` that yields a scripted message stream."""

    def __init__(self, messages: list[NMBMessage] | None = None) -> None:
        self._queue: asyncio.Queue[NMBMessage] = asyncio.Queue()
        for m in messages or []:
            self._queue.put_nowait(m)
        self._closed = asyncio.Event()

    async def push(self, msg: NMBMessage) -> None:
        await self._queue.put(msg)

    def close(self) -> None:
        self._closed.set()

    async def listen(self) -> AsyncIterator[NMBMessage]:
        while not self._closed.is_set():
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield msg


class _RecordingRenderer:
    """Records every render call so tests can assert on them."""

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.present: list[tuple[str, str, str]] = []
        self.actions: list[tuple[str, str, str]] = []
        self.progress: list[tuple[str, TaskProgressPayload]] = []
        self.errors: list[tuple[str, TaskErrorPayload]] = []
        self.completion_failures: list[tuple[str, str]] = []
        self._raise_on = raise_on or set()

    async def render_present_work(
        self, *, context: WorkflowContext, summary: str, diff: str
    ) -> None:
        if "present_work" in self._raise_on:
            raise RuntimeError("renderer boom")
        self.present.append((context.workflow_id, summary, diff))

    async def render_finalization_action(
        self, *, context: WorkflowContext, action: str, result: str
    ) -> None:
        self.actions.append((context.workflow_id, action, result))

    async def render_workflow_progress(
        self, *, context: WorkflowContext, progress: TaskProgressPayload
    ) -> None:
        if "progress" in self._raise_on:
            raise RuntimeError("renderer boom")
        self.progress.append((context.workflow_id, progress))

    async def render_workflow_error(
        self, *, context: WorkflowContext, error: TaskErrorPayload
    ) -> None:
        self.errors.append((context.workflow_id, error))

    async def render_workflow_completion_failure(
        self, *, context: WorkflowContext, complete: TaskCompletePayload, error: str
    ) -> None:
        self.completion_failures.append((context.workflow_id, error))


def _ctx(workflow_id: str) -> WorkflowContext:
    return WorkflowContext(
        workflow_id=workflow_id,
        task=TaskAssignPayload(
            prompt="x",
            workflow_id=workflow_id,
            parent_sandbox_id="orchestrator",
            agent_id=f"coding-{workflow_id[-4:]}",
            workspace_root="/tmp/wf",
        ),
        channel_id="C1",
        thread_ts="T1",
    )


def _audit_flush(workflow_id: str, *, row_id: str) -> NMBMessage:
    payload = AuditFlushPayload(
        workflow_id=workflow_id,
        parent_sandbox_id="orchestrator",
        agent_id=f"coding-{workflow_id[-4:]}",
        tool_calls=[
            AuditToolCallPayload(
                id=row_id,
                command="bash",
                operation_type="READ",
                duration_ms=1.0,
                success=True,
            )
        ],
    )
    return NMBMessage(
        op=Op.DELIVER,
        from_sandbox=f"coding-{workflow_id[-4:]}",
        type=AUDIT_FLUSH,
        payload=dump(payload),
    )


def _task_complete(workflow_id: str) -> NMBMessage:
    payload = TaskCompletePayload(
        workflow_id=workflow_id,
        summary="done",
        diff="",
    )
    return NMBMessage(
        op=Op.DELIVER,
        from_sandbox=f"coding-{workflow_id[-4:]}",
        type=TASK_COMPLETE,
        payload=dump(payload),
    )


def _task_error(workflow_id: str) -> NMBMessage:
    payload = TaskErrorPayload(
        workflow_id=workflow_id,
        error="kaboom",
        error_kind="other",
    )
    return NMBMessage(
        op=Op.DELIVER,
        from_sandbox=f"coding-{workflow_id[-4:]}",
        type=TASK_ERROR,
        payload=dump(payload),
    )


def _task_progress(workflow_id: str) -> NMBMessage:
    payload = TaskProgressPayload(
        workflow_id=workflow_id,
        status="writing_code",
        note="Created src/api/health.py",
    )
    return NMBMessage(
        op=Op.DELIVER,
        from_sandbox=f"coding-{workflow_id[-4:]}",
        type=TASK_PROGRESS,
        payload=dump(payload),
    )


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
async def audit_db(tmp_path: Path) -> AuditDB:
    db = AuditDB(str(tmp_path / "dispatcher.db"))
    await db.open()
    yield db  # type: ignore[misc]
    await db.close()


# ── Audit flush ───────────────────────────────────────────────────


class TestAuditFlushIngest:
    @pytest.mark.asyncio
    async def test_concurrent_workflows_flush_lands_for_both(
        self,
        audit_db: AuditDB,
    ) -> None:
        bus = _ScriptedBus(
            [
                _audit_flush("wf-alpha", row_id="row-alpha"),
                _audit_flush("wf-beta", row_id="row-beta"),
            ]
        )
        dispatcher = WorkflowDispatcher(bus, audit=audit_db)  # type: ignore[arg-type]
        try:
            await dispatcher.start()
            await _wait_until(
                lambda: len(  # noqa: PLR2004
                    asyncio.run_coroutine_threadsafe.__name__,
                )
                or True,
                timeout=0.0,
            )
            # Poll the DB until both rows arrive.
            async def _have_two() -> bool:
                rows = await audit_db.query("SELECT id FROM tool_calls")
                return len(rows) >= 2

            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if await _have_two():
                    break
                await asyncio.sleep(0.02)
            rows = await audit_db.query("SELECT id, workflow_id FROM tool_calls ORDER BY id")
            assert sorted(r["id"] for r in rows) == ["row-alpha", "row-beta"]
            workflows = {r["id"]: r["workflow_id"] for r in rows}
            assert workflows["row-alpha"] == "wf-alpha"
            assert workflows["row-beta"] == "wf-beta"
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_replay_is_idempotent(self, audit_db: AuditDB) -> None:
        msg = _audit_flush("wf-d", row_id="row-d")
        bus = _ScriptedBus([msg, msg])
        dispatcher = WorkflowDispatcher(bus, audit=audit_db)  # type: ignore[arg-type]
        try:
            await dispatcher.start()
            await asyncio.sleep(0.2)
            rows = await audit_db.query("SELECT id FROM tool_calls")
            assert [r["id"] for r in rows] == ["row-d"]
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_malformed_flush_does_not_kill_dispatcher(
        self,
        audit_db: AuditDB,
    ) -> None:
        malformed = NMBMessage(
            op=Op.DELIVER,
            from_sandbox="coding-bad",
            type=AUDIT_FLUSH,
            payload={"workflow_id": ""},  # missing required fields
        )
        bus = _ScriptedBus([malformed, _audit_flush("wf-e", row_id="row-e")])
        dispatcher = WorkflowDispatcher(bus, audit=audit_db)  # type: ignore[arg-type]
        try:
            await dispatcher.start()
            await asyncio.sleep(0.2)
            rows = await audit_db.query("SELECT id FROM tool_calls")
            assert [r["id"] for r in rows] == ["row-e"]
        finally:
            bus.close()
            await dispatcher.close()


# ── task.complete → finalisation fork ─────────────────────────────


class TestTaskCompleteRouting:
    @pytest.mark.asyncio
    async def test_complete_for_known_workflow_forks_finalisation(self) -> None:
        from nemoclaw_escapades.orchestrator.finalization import FinalizationResult

        finalised: list[str] = []

        class _StubFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> FinalizationResult:
                finalised.append(ctx.workflow_id)
                # Default to terminal so the workflow is deregistered;
                # the explicit "stays registered" cases are tested in
                # ``TestConditionalDeregistration``.
                return FinalizationResult(
                    workflow_id=ctx.workflow_id,
                    action="push_branch",
                    message="ok",
                    is_terminal=True,
                )

        bus = _ScriptedBus([_task_complete("wf-x")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_StubFinalizer(),  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-x"))
        try:
            await dispatcher.start()
            assert await dispatcher.wait_for_finalization("wf-x", timeout=2.0)
            assert finalised == ["wf-x"]
            # Workflow deregistered after a *terminal* finalisation.
            assert dispatcher.get_workflow("wf-x") is None
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_complete_for_unknown_workflow_dropped(self) -> None:
        from nemoclaw_escapades.orchestrator.finalization import FinalizationResult

        called: list[str] = []

        class _StubFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> FinalizationResult:
                called.append(ctx.workflow_id)
                return FinalizationResult(
                    workflow_id=ctx.workflow_id,
                    action="present_work_to_user",
                    message="ok",
                    is_terminal=True,
                )

        bus = _ScriptedBus([_task_complete("wf-unknown")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_StubFinalizer(),  # type: ignore[arg-type]
        )
        try:
            await dispatcher.start()
            await asyncio.sleep(0.2)
            assert called == []
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_finalisation_failure_routes_to_renderer(self) -> None:
        class _BrokenFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> Any:
                raise RuntimeError("baseline drift simulated")

        renderer = _RecordingRenderer()
        bus = _ScriptedBus([_task_complete("wf-y")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_BrokenFinalizer(),  # type: ignore[arg-type]
            renderer=renderer,  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-y"))
        try:
            await dispatcher.start()
            assert await dispatcher.wait_for_finalization("wf-y", timeout=2.0)
            assert len(renderer.completion_failures) == 1
            assert renderer.completion_failures[0][0] == "wf-y"
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_concurrent_finalisations_run_in_parallel(self) -> None:
        from nemoclaw_escapades.orchestrator.finalization import FinalizationResult

        running: list[str] = []
        gates: dict[str, asyncio.Event] = {}

        class _SlowFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> FinalizationResult:
                running.append(ctx.workflow_id)
                gate = gates.setdefault(ctx.workflow_id, asyncio.Event())
                await gate.wait()
                return FinalizationResult(
                    workflow_id=ctx.workflow_id,
                    action="push_branch",
                    message="ok",
                    is_terminal=True,
                )

        bus = _ScriptedBus([_task_complete("wf-1"), _task_complete("wf-2")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_SlowFinalizer(),  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-1"))
        dispatcher.register_workflow(_ctx("wf-2"))
        try:
            await dispatcher.start()
            await _wait_until(lambda: dispatcher.in_flight_finalizations >= 2)
            assert dispatcher.in_flight_finalizations == 2
            for wf in ("wf-1", "wf-2"):
                gates[wf] = gates.get(wf) or asyncio.Event()
                gates[wf].set()
            assert await dispatcher.wait_for_finalization("wf-1", timeout=2.0)
            assert await dispatcher.wait_for_finalization("wf-2", timeout=2.0)
        finally:
            bus.close()
            await dispatcher.close()


# ── task.error / task.progress ─────────────────────────────────────


class TestErrorAndProgressRouting:
    @pytest.mark.asyncio
    async def test_task_error_renders_and_deregisters(self) -> None:
        renderer = _RecordingRenderer()
        bus = _ScriptedBus([_task_error("wf-z")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            renderer=renderer,  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-z"))
        try:
            await dispatcher.start()
            await _wait_until(lambda: len(renderer.errors) >= 1)
            assert renderer.errors[0][0] == "wf-z"
            assert dispatcher.get_workflow("wf-z") is None
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_task_progress_routes_to_renderer(self) -> None:
        renderer = _RecordingRenderer()
        bus = _ScriptedBus([_task_progress("wf-p")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            renderer=renderer,  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-p"))
        try:
            await dispatcher.start()
            await _wait_until(lambda: len(renderer.progress) >= 1)
            assert renderer.progress[0][0] == "wf-p"
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_renderer_exception_does_not_kill_loop(self) -> None:
        """A renderer that raises on one delivery must not stop the loop."""
        # First progress raises (per ``raise_on``); second arrives as
        # normal — the dispatcher's per-message try/except catches the
        # raise and continues consuming.
        renderer = _RecordingRenderer(raise_on={"progress-once"})

        class _RaiseOnceRenderer(_RecordingRenderer):
            def __init__(self) -> None:
                super().__init__()
                self._raised = False

            async def render_workflow_progress(
                self, *, context: WorkflowContext, progress: TaskProgressPayload
            ) -> None:
                if not self._raised:
                    self._raised = True
                    raise RuntimeError("renderer boom")
                self.progress.append((context.workflow_id, progress))

        rec = _RaiseOnceRenderer()
        bus = _ScriptedBus([_task_progress("wf-p"), _task_progress("wf-p")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            renderer=rec,  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-p"))
        try:
            await dispatcher.start()
            await _wait_until(lambda: len(rec.progress) >= 1)
            assert len(rec.progress) == 1
        finally:
            bus.close()
            await dispatcher.close()
        # Reference unused-but-imported renderer to silence ruff.
        _ = renderer


# ── close() ────────────────────────────────────────────────────────


class TestConditionalDeregistration:
    """Regression: deregister only when the chosen action is terminal.

    Previously :meth:`WorkflowDispatcher._finalize` deregistered
    unconditionally in a ``finally`` block, so:

    - ``present_work_to_user`` posted action buttons but the
      workflow was deregistered before the user could click; every
      click failed with "Workflow is no longer active".
    - ``re_delegate`` reuses the same ``workflow_id`` for iteration
      2; deregistering after iteration 1 caused iteration 2's
      ``task.complete`` to be silently dropped as "unknown
      workflow".

    The fix inspects the :class:`FinalizationResult.is_terminal`
    flag and keeps non-terminal workflows registered.
    """

    @pytest.mark.asyncio
    async def test_present_work_keeps_workflow_registered(self) -> None:
        from nemoclaw_escapades.orchestrator.finalization import FinalizationResult

        class _PresentFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> FinalizationResult:
                # Mimic ``present_work_to_user``: non-terminal.
                return FinalizationResult(
                    workflow_id=ctx.workflow_id,
                    action="present_work_to_user",
                    message="rendered",
                    is_terminal=False,
                )

        bus = _ScriptedBus([_task_complete("wf-pw")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_PresentFinalizer(),  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-pw"))
        try:
            await dispatcher.start()
            assert await dispatcher.wait_for_finalization("wf-pw", timeout=2.0)
            # Workflow is still registered — buttons can fire later.
            assert dispatcher.get_workflow("wf-pw") is not None
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_terminal_action_deregisters(self) -> None:
        from nemoclaw_escapades.orchestrator.finalization import FinalizationResult

        class _PushFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> FinalizationResult:
                return FinalizationResult(
                    workflow_id=ctx.workflow_id,
                    action="push_and_create_pr",
                    message="https://github.com/.../pull/1",
                    is_terminal=True,
                )

        bus = _ScriptedBus([_task_complete("wf-push")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_PushFinalizer(),  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-push"))
        try:
            await dispatcher.start()
            assert await dispatcher.wait_for_finalization("wf-push", timeout=2.0)
            assert dispatcher.get_workflow("wf-push") is None
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_re_delegate_keeps_workflow_registered(self) -> None:
        """``re_delegate`` reuses the workflow id; iteration 2 needs the registration."""
        from nemoclaw_escapades.orchestrator.finalization import FinalizationResult

        class _ReDelegateFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> FinalizationResult:
                return FinalizationResult(
                    workflow_id=ctx.workflow_id,
                    action="re_delegate",
                    message="Re-delegated",
                    is_terminal=False,
                )

        bus = _ScriptedBus([_task_complete("wf-iter")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_ReDelegateFinalizer(),  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-iter"))
        try:
            await dispatcher.start()
            assert await dispatcher.wait_for_finalization("wf-iter", timeout=2.0)
            # Iteration 2's task.complete (same workflow_id) must
            # still find the registered context.
            assert dispatcher.get_workflow("wf-iter") is not None
        finally:
            bus.close()
            await dispatcher.close()

    @pytest.mark.asyncio
    async def test_finalisation_failure_deregisters(self) -> None:
        """Exception path always deregisters — nothing for the user to act on."""
        renderer = _RecordingRenderer()

        class _BrokenFinalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> Any:
                raise RuntimeError("boom")

        bus = _ScriptedBus([_task_complete("wf-broken")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_BrokenFinalizer(),  # type: ignore[arg-type]
            renderer=renderer,  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-broken"))
        try:
            await dispatcher.start()
            assert await dispatcher.wait_for_finalization("wf-broken", timeout=2.0)
            assert dispatcher.get_workflow("wf-broken") is None
            assert len(renderer.completion_failures) == 1
        finally:
            bus.close()
            await dispatcher.close()


class TestDispatcherClose:
    @pytest.mark.asyncio
    async def test_close_cancels_in_flight_finalisation(self) -> None:
        gate = asyncio.Event()

        class _Finalizer:
            async def finalize(
                self,
                ctx: WorkflowContext,
                complete: TaskCompletePayload,
            ) -> Any:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    raise

        bus = _ScriptedBus([_task_complete("wf-c")])
        dispatcher = WorkflowDispatcher(
            bus,  # type: ignore[arg-type]
            finalizer=_Finalizer(),  # type: ignore[arg-type]
        )
        dispatcher.register_workflow(_ctx("wf-c"))
        try:
            await dispatcher.start()
            await _wait_until(lambda: dispatcher.in_flight_finalizations >= 1)
            await dispatcher.close()
            assert dispatcher.in_flight_finalizations == 0
        finally:
            bus.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        bus = _ScriptedBus()
        dispatcher = WorkflowDispatcher(bus)  # type: ignore[arg-type]
        await dispatcher.start()
        await dispatcher.close()
        await dispatcher.close()  # must not raise
        bus.close()
