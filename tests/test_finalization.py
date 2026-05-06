"""Tests for Phase 3b finalization.

Covers:

- Baseline echo-match (§6.6.3 part 1) — ``verify_baseline``.
- Diff verification (§6.6.3 part 2) — ``FinalizationCoordinator._verify_diff``.
- JSONL fallback ingest (§13.2) — ``FinalizationCoordinator._ingest_jsonl_fallback``.
- The full finalisation registry — every tool registered, baselines plumbed
  through ``re_delegate``, ``discard_work`` safety check, ``push_branch`` end-to-end.
- Slack rendering for ``present_work_to_user`` (action-button shape).

The ``FinalizationCoordinator.finalize`` end-to-end path is tested
against a stub backend that scripts the model's tool choice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import AgentLoopConfig
from nemoclaw_escapades.connectors.slack.finalization import build_present_work_response
from nemoclaw_escapades.models.types import (
    FINALIZATION_ACTION_DISCARD,
    FINALIZATION_ACTION_ITERATE,
    FINALIZATION_ACTION_PUSH_PR,
    InferenceRequest,
    InferenceResponse,
    TokenUsage,
    ToolCall,
)
from nemoclaw_escapades.nmb.protocol import (
    TaskAssignPayload,
    TaskCompletePayload,
    WorkspaceBaseline,
)
from nemoclaw_escapades.orchestrator.finalization import (
    BaselineDriftError,
    FinalizationCoordinator,
    build_finalization_prompt,
    verify_baseline,
)
from nemoclaw_escapades.orchestrator.workflow import WorkflowContext
from nemoclaw_escapades.tools.finalization import (
    FinalizationSession,
    create_finalization_tool_registry,
)


class ToolCallingBackend(BackendBase):
    """Backend that scripts a single tool call then a final reply."""

    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls = 0

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        self.calls += 1
        if self.calls == 1:
            return InferenceResponse(
                content="",
                model="mock",
                usage=TokenUsage(),
                latency_ms=1,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name=self.tool_name,
                        arguments=json.dumps(self.arguments),
                    )
                ],
            )
        return InferenceResponse(
            content="finalized",
            model="mock",
            usage=TokenUsage(),
            latency_ms=1,
            finish_reason="stop",
        )


def _baseline(sha: str = "a" * 40) -> WorkspaceBaseline:
    return WorkspaceBaseline(
        repo_url="https://example.com/repo.git",
        branch="main",
        base_sha=sha,
    )


def _task(workspace_root: Path, *, agent_id: str = "coding-abcdef02") -> TaskAssignPayload:
    return TaskAssignPayload(
        prompt="fix bug",
        workflow_id="wf-1",
        parent_sandbox_id="orchestrator",
        agent_id=agent_id,
        workspace_root=str(workspace_root),
        workspace_baseline=_baseline(),
    )


def _complete() -> TaskCompletePayload:
    return TaskCompletePayload(
        workflow_id="wf-1",
        summary="fixed bug",
        diff="diff --git a/x b/x\n",
        workspace_baseline=_baseline(),
        files_changed=["x"],
    )


# ── Per-agent workspace fixtures ───────────────────────────────────


@pytest.fixture
def per_agent_workspace(tmp_path: Path) -> Path:
    """Create a `<tmp>/agent-<id>/` workspace shaped like real delegations."""
    workspace = tmp_path / "agent-abcdef02"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


# ── Baseline verification ──────────────────────────────────────────


class TestBaselineVerification:
    def test_matching_baseline_passes(self, per_agent_workspace: Path) -> None:
        verify_baseline(_task(per_agent_workspace), _complete())

    def test_mismatched_baseline_raises(self, per_agent_workspace: Path) -> None:
        complete = _complete()
        complete.workspace_baseline = _baseline(sha="b" * 40)
        with pytest.raises(BaselineDriftError):
            verify_baseline(_task(per_agent_workspace), complete)

    def test_both_none_passes(self, tmp_path: Path) -> None:
        task = TaskAssignPayload(
            prompt="x",
            workflow_id="w",
            parent_sandbox_id="o",
            agent_id="a",
            workspace_root=str(tmp_path),
        )
        complete = TaskCompletePayload(workflow_id="w", summary="ok")
        verify_baseline(task, complete)

    def test_one_none_one_set_raises(self, tmp_path: Path) -> None:
        task = _task(tmp_path)
        complete = TaskCompletePayload(workflow_id="wf-1", summary="ok")
        with pytest.raises(BaselineDriftError):
            verify_baseline(task, complete)


# ── Finalisation prompt ────────────────────────────────────────────


class TestFinalizationPrompt:
    def test_prompt_uses_typed_fields_and_notes(self, per_agent_workspace: Path) -> None:
        notes = per_agent_workspace / "notes.md"
        notes.write_text("tested manually\n")
        complete = _complete()
        complete.notes_path = "notes.md"
        prompt = build_finalization_prompt(_task(per_agent_workspace), complete)
        assert "fixed bug" in prompt
        assert "diff --git" in prompt
        assert "tested manually" in prompt


# ── Finalisation tools ────────────────────────────────────────────


class TestFinalizationToolRegistry:
    @pytest.mark.asyncio
    async def test_registry_exposes_all_tools(self, per_agent_workspace: Path) -> None:
        session = FinalizationSession(task=_task(per_agent_workspace), complete=_complete())
        registry = create_finalization_tool_registry(session)
        assert {
            "present_work_to_user",
            "push_and_create_pr",
            "push_branch",
            "discard_work",
            "re_delegate",
            "destroy_sandbox",
        }.issubset(set(registry.names))

    @pytest.mark.asyncio
    async def test_re_delegate_reuses_pinned_baseline(self, per_agent_workspace: Path) -> None:
        captured: dict[str, TaskAssignPayload] = {}

        class Manager:
            async def delegate(
                self,
                task: TaskAssignPayload,
                *,
                context: object | None = None,
            ) -> Any:
                del context
                captured["task"] = task
                return type("Result", (), {"workflow_id": task.workflow_id})()

        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
            delegation_manager=Manager(),  # type: ignore[arg-type]
        )
        result = await session.re_delegate("try again", max_turns=9)
        assert "Re-delegated" in result
        assert captured["task"].workspace_baseline == _baseline()
        assert captured["task"].is_iteration is True
        assert captured["task"].iteration_number == 1

    @pytest.mark.asyncio
    async def test_re_delegate_updates_workflow_context(
        self,
        per_agent_workspace: Path,
    ) -> None:
        """Regression: cascading ``re_delegate`` produces monotonic iteration numbers.

        Previously :class:`WorkflowContext.task` was never updated
        after :meth:`re_delegate` — :meth:`DelegationManager.delegate`
        explicitly discards the context and nothing else re-registered
        it.  On every subsequent iteration, the dispatcher's
        registered context still pointed at the *original* task and
        ``iteration_number = original.iteration_number + 1`` always
        produced 1, no matter how many follow-ups had already been
        sent.  The fix mutates ``context.task`` in place so the
        dispatcher sees the latest iteration on the next
        ``task.complete`` arrival.
        """
        captured_tasks: list[TaskAssignPayload] = []

        class Manager:
            async def delegate(
                self,
                task: TaskAssignPayload,
                *,
                context: object | None = None,
            ) -> Any:
                del context
                captured_tasks.append(task)
                return type("Result", (), {"workflow_id": task.workflow_id})()

        manager = Manager()
        original = _task(per_agent_workspace)
        ctx = WorkflowContext(workflow_id="wf-1", task=original)

        # ── Iteration 0 → 1 ────────────────────────────────────────
        session = FinalizationSession(
            task=ctx.task,
            complete=_complete(),
            context=ctx,
            delegation_manager=manager,  # type: ignore[arg-type]
        )
        await session.re_delegate("fix tests")
        assert captured_tasks[-1].iteration_number == 1
        assert captured_tasks[-1].prompt == "fix tests"
        # The context's task — the same object the dispatcher holds —
        # now reflects iteration 1.
        assert ctx.task.iteration_number == 1
        assert ctx.task.prompt == "fix tests"

        # ── Iteration 1 → 2 (the dispatcher rebuilds the session
        # from ``ctx.task`` when iteration 1's task.complete arrives) ─
        session = FinalizationSession(
            task=ctx.task,
            complete=_complete(),
            context=ctx,
            delegation_manager=manager,  # type: ignore[arg-type]
        )
        await session.re_delegate("more fixes")
        assert captured_tasks[-1].iteration_number == 2, (
            "cascading re_delegate must increment monotonically; "
            f"got {captured_tasks[-1].iteration_number}"
        )
        assert ctx.task.iteration_number == 2
        assert ctx.task.prompt == "more fixes"

        # ── Iteration 2 → 3 (one more for good measure) ────────────
        session = FinalizationSession(
            task=ctx.task,
            complete=_complete(),
            context=ctx,
            delegation_manager=manager,  # type: ignore[arg-type]
        )
        await session.re_delegate("third try")
        assert captured_tasks[-1].iteration_number == 3
        assert ctx.task.iteration_number == 3

    @pytest.mark.asyncio
    async def test_discard_work_refuses_non_agent_path(self, tmp_path: Path) -> None:
        # tmp_path is not under an ``agent-…`` subdirectory; the
        # safety check must refuse the discard.
        bad_workspace = tmp_path  # no ``agent-`` prefix
        canary = bad_workspace / "important.txt"
        canary.write_text("don't delete me")
        session = FinalizationSession(
            task=TaskAssignPayload(
                prompt="x",
                workflow_id="w",
                parent_sandbox_id="o",
                agent_id="a",
                workspace_root=str(bad_workspace),
            ),
            complete=TaskCompletePayload(workflow_id="w", summary="ok"),
        )
        result = await session.discard_work("test")
        assert result.startswith("Error:")
        assert canary.exists(), "discard_work must not delete non-agent paths"
        # Regression: the safety-check refusal must NOT mark the
        # workflow terminal, otherwise the action handler would
        # deregister it and every other button click would die with
        # "Workflow is no longer active."
        assert session.state.is_terminal is False

    @pytest.mark.asyncio
    async def test_discard_work_removes_per_agent_workspace(
        self,
        per_agent_workspace: Path,
    ) -> None:
        canary = per_agent_workspace / "leftover.txt"
        canary.write_text("should be deleted")
        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
        )
        result = await session.discard_work("test")
        assert "Discarded" in result or "test" in result
        assert not per_agent_workspace.exists()

    @pytest.mark.asyncio
    async def test_destroy_sandbox_is_no_op(self, per_agent_workspace: Path) -> None:
        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
        )
        result = await session.destroy_sandbox()
        assert "single-shot" in result


# ── Behavioural git-tool orchestration ─────────────────────────────


class TestPushBranch:
    @pytest.mark.asyncio
    async def test_push_branch_calls_public_git_helpers(
        self,
        per_agent_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``push_branch`` orchestrates checkout → commit → push in order."""
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        async def _checkout(*args: Any, **kwargs: Any) -> str:
            calls.append(("checkout", args, kwargs))
            return "checked out"

        async def _commit(*args: Any, **kwargs: Any) -> str:
            calls.append(("commit", args, kwargs))
            return "committed"

        async def _push(*args: Any, **kwargs: Any) -> str:
            calls.append(("push", args, kwargs))
            return "pushed feature/test"

        import nemoclaw_escapades.tools.finalization as finalization_mod

        monkeypatch.setattr(finalization_mod, "checkout_branch", _checkout)
        monkeypatch.setattr(finalization_mod, "commit_workspace", _commit)
        monkeypatch.setattr(finalization_mod, "git_push_branch", _push)

        task = _task(per_agent_workspace)
        session = FinalizationSession(task=task, complete=_complete())
        result = await session.push_branch("feature/test", "Add feature")
        assert result == "pushed feature/test"
        assert [name for name, _, _ in calls] == ["checkout", "commit", "push"]
        assert session.state.is_terminal is True

    @pytest.mark.asyncio
    async def test_push_branch_handles_nothing_to_commit(
        self,
        per_agent_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A no-op commit helper result is fine as long as push succeeds."""
        async def _checkout(*_args: Any, **_kwargs: Any) -> str:
            return "checked out"

        async def _commit(*_args: Any, **_kwargs: Any) -> str:
            return "nothing to commit, working tree clean"

        async def _push(*_args: Any, **_kwargs: Any) -> str:
            return "pushed empty branch"

        import nemoclaw_escapades.tools.finalization as finalization_mod

        monkeypatch.setattr(finalization_mod, "checkout_branch", _checkout)
        monkeypatch.setattr(finalization_mod, "commit_workspace", _commit)
        monkeypatch.setattr(finalization_mod, "git_push_branch", _push)

        task = _task(per_agent_workspace)
        session = FinalizationSession(task=task, complete=_complete())
        result = await session.push_branch("feature/empty")
        assert result == "pushed empty branch"
        assert session.state.is_terminal is True


class TestPushAndCreatePrSingleRender:
    """Regression: ``push_and_create_pr`` must render exactly once.

    Previously it called ``self.push_branch(...)`` internally, which
    rendered a ``"push_branch"`` message via the renderer; then the
    outer method rendered again as ``"push_and_create_pr"``.  The
    user saw two Slack messages for what is logically one atomic
    operation.  The fix routes both methods through a private
    ``_do_push_branch`` that performs git ops only — no rendering,
    no state mutation.
    """

    class _RecordingRenderer:
        def __init__(self) -> None:
            self.actions: list[tuple[str, str]] = []

        async def render_present_work(self, **_: Any) -> None:
            return None

        async def render_finalization_action(
            self, *, context: Any, action: str, result: str
        ) -> None:
            del context
            self.actions.append((action, result))

        async def render_workflow_progress(self, **_: Any) -> None:
            return None

        async def render_workflow_error(self, **_: Any) -> None:
            return None

        async def render_workflow_completion_failure(self, **_: Any) -> None:
            return None

    @pytest.mark.asyncio
    async def test_push_and_create_pr_renders_once_on_push_failure(
        self,
        per_agent_workspace: Path,
    ) -> None:
        """A bad branch (no git repo) should render exactly one ``push_and_create_pr`` line."""
        renderer = self._RecordingRenderer()
        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
            renderer=renderer,  # type: ignore[arg-type]
            context=WorkflowContext(
                workflow_id="wf-1",
                task=_task(per_agent_workspace),
            ),
        )
        result = await session.push_and_create_pr("feature/x", "Title")
        assert result.startswith("Error:"), "push should fail (no git repo)"
        # Exactly one render — no spurious push_branch message.
        actions = [a for a, _ in renderer.actions]
        assert actions == ["push_and_create_pr"], f"got renders: {renderer.actions!r}"


class TestPushBranchRendersOnce:
    """``push_branch`` keeps its own single-render contract intact."""

    class _RecordingRenderer(TestPushAndCreatePrSingleRender._RecordingRenderer):
        pass

    @pytest.mark.asyncio
    async def test_push_branch_renders_once_on_failure(
        self,
        per_agent_workspace: Path,
    ) -> None:
        renderer = self._RecordingRenderer()
        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
            renderer=renderer,  # type: ignore[arg-type]
            context=WorkflowContext(
                workflow_id="wf-1",
                task=_task(per_agent_workspace),
            ),
        )
        await session.push_branch("feature/x")
        actions = [a for a, _ in renderer.actions]
        assert actions == ["push_branch"], f"got renders: {renderer.actions!r}"


class TestTerminalFlagOnFailure:
    """Regression: a failed terminal action leaves ``is_terminal=False``.

    Previously every git op set ``state.is_terminal = True`` regardless
    of the outcome, so a transient push failure caused the action
    handler's deregistration to fire and the user lost access to the
    Push & PR / Iterate / Discard buttons in their thread.  The fix
    only sets the flag when the operation actually succeeded, so
    recoverable failures keep the workflow alive.
    """

    @pytest.mark.asyncio
    async def test_push_branch_failure_leaves_workflow_alive(
        self,
        per_agent_workspace: Path,
    ) -> None:
        # No git repo at the workspace path → ``push_branch`` returns
        # an ``Error: ...`` string without raising.
        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
        )
        result = await session.push_branch("feature/x")
        assert result.startswith("Error:")
        assert session.state.is_terminal is False, (
            "push_branch failure must keep the workflow registered "
            "so the user can retry, iterate, or discard"
        )

    @pytest.mark.asyncio
    async def test_push_and_create_pr_failure_leaves_workflow_alive(
        self,
        per_agent_workspace: Path,
    ) -> None:
        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
        )
        result = await session.push_and_create_pr("feature/x", "title")
        assert result.startswith("Error:"), "push should fail (no git repo)"
        assert session.state.is_terminal is False

    @pytest.mark.asyncio
    async def test_push_branch_success_marks_terminal(
        self,
        per_agent_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Conversely, a successful helper result sets the terminal flag."""
        async def _checkout(*_args: Any, **_kwargs: Any) -> str:
            return "checked out"

        async def _commit(*_args: Any, **_kwargs: Any) -> str:
            return "committed"

        async def _push(*_args: Any, **_kwargs: Any) -> str:
            return "pushed"

        import nemoclaw_escapades.tools.finalization as finalization_mod

        monkeypatch.setattr(finalization_mod, "checkout_branch", _checkout)
        monkeypatch.setattr(finalization_mod, "commit_workspace", _commit)
        monkeypatch.setattr(finalization_mod, "git_push_branch", _push)

        session = FinalizationSession(
            task=_task(per_agent_workspace),
            complete=_complete(),
        )
        result = await session.push_branch("feature/test")
        assert not result.startswith("Error:"), result
        assert session.state.is_terminal is True


# ── JSONL fallback ingest ─────────────────────────────────────────


class TestJsonlFallbackIngest:
    @pytest.mark.asyncio
    async def test_corrupted_jsonl_does_not_abort_finalize(
        self,
        tmp_path: Path,
    ) -> None:
        """Regression: a truncated / malformed JSONL must not raise.

        Previously :meth:`_ingest_jsonl_fallback` called
        ``json.loads(line)`` and ``model_validate`` without exception
        handling, so a sub-agent that crashed mid-write would corrupt
        the audit fallback file and the orchestrator would abort
        finalisation — the user's actual sub-agent work would never
        be presented.
        """
        workspace = tmp_path / "agent-abcdef02"
        workspace.mkdir()
        fallback_dir = workspace / ".nemoclaw"
        fallback_dir.mkdir()
        fallback = fallback_dir / "audit-wf-1.jsonl"
        # Mix valid + truncated + missing-field rows.
        valid = {
            "workflow_id": "wf-1",
            "parent_sandbox_id": "orchestrator",
            "agent_id": "coding-abcdef02",
            "agent_role": "coding",
            "tool_call": {
                "id": "row-good",
                "service": "files",
                "command": "read_file",
                "args": "{}",
                "operation_type": "READ",
                "duration_ms": 1.0,
                "success": True,
                "response_payload": "ok",
            },
        }
        fallback.write_text(
            json.dumps(valid) + "\n"
            + '{"workflow_id":"wf-1","truncated\n'  # JSON parse error
            + '{"workflow_id":"wf-1"}\n'  # missing tool_call key
            + json.dumps(valid).replace("row-good", "row-good-2") + "\n"
        )
        db_path = tmp_path / "audit.db"
        db = AuditDB(str(db_path))
        await db.open()
        try:
            from nemoclaw_escapades.backends.base import BackendBase as _Backend

            class _StubBackend(_Backend):
                async def complete(self, request: InferenceRequest) -> InferenceResponse:
                    raise NotImplementedError

            coord = FinalizationCoordinator(
                backend=_StubBackend(),
                config=AgentLoopConfig(),
                audit=db,
            )
            count = await coord._ingest_jsonl_fallback(_task(workspace))
            # Only the valid rows were ingested; bad lines were
            # logged-and-skipped, the coordinator did NOT raise.
            assert count == 2
            rows = await db.query("SELECT id FROM tool_calls ORDER BY id")
            assert sorted(r["id"] for r in rows) == ["row-good", "row-good-2"]
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_ingest_picks_up_disk_rows(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent-abcdef02"
        workspace.mkdir()
        fallback_dir = workspace / ".nemoclaw"
        fallback_dir.mkdir()
        fallback = fallback_dir / "audit-wf-1.jsonl"
        # Mirror the AuditBuffer.write_jsonl_fallback shape.
        row = {
            "workflow_id": "wf-1",
            "parent_sandbox_id": "orchestrator",
            "agent_id": "coding-abcdef02",
            "agent_role": "coding",
            "tool_call": {
                "id": "row-disk-1",
                "service": "files",
                "command": "read_file",
                "args": "{}",
                "operation_type": "READ",
                "duration_ms": 1.0,
                "success": True,
                "response_payload": "ok",
            },
        }
        fallback.write_text(json.dumps(row) + "\n")
        db_path = tmp_path / "audit.db"
        db = AuditDB(str(db_path))
        await db.open()
        try:
            from nemoclaw_escapades.backends.base import BackendBase as _Backend

            class _StubBackend(_Backend):
                async def complete(self, request: InferenceRequest) -> InferenceResponse:
                    raise NotImplementedError

            coord = FinalizationCoordinator(
                backend=_StubBackend(),
                config=AgentLoopConfig(),
                audit=db,
            )
            task = _task(workspace)
            count = await coord._ingest_jsonl_fallback(task)
            assert count == 1
            rows = await db.query("SELECT * FROM tool_calls WHERE id = 'row-disk-1'")
            assert len(rows) == 1
            assert rows[0]["workflow_id"] == "wf-1"
            assert rows[0]["agent_id"] == "coding-abcdef02"
        finally:
            await db.close()


# ── End-to-end coordinator ─────────────────────────────────────────


class TestFinalizationCoordinator:
    @pytest.mark.asyncio
    async def test_finalize_runs_model_and_present_tool(
        self,
        per_agent_workspace: Path,
    ) -> None:
        backend = ToolCallingBackend("present_work_to_user", {"summary": "show this"})
        coordinator = FinalizationCoordinator(
            backend=backend,
            config=AgentLoopConfig(max_tool_rounds=3),
        )
        ctx = WorkflowContext(
            workflow_id="wf-1",
            task=_task(per_agent_workspace),
        )
        result = await coordinator.finalize(ctx, _complete())
        assert result.action == "present_work_to_user"
        assert result.message.startswith("show this")

    @pytest.mark.asyncio
    async def test_finalize_aborts_on_baseline_drift(
        self,
        per_agent_workspace: Path,
    ) -> None:
        backend = ToolCallingBackend("present_work_to_user", {})
        coordinator = FinalizationCoordinator(
            backend=backend,
            config=AgentLoopConfig(max_tool_rounds=3),
        )
        ctx = WorkflowContext(
            workflow_id="wf-1",
            task=_task(per_agent_workspace),
        )
        complete = _complete()
        complete.workspace_baseline = _baseline(sha="b" * 40)  # drift!
        with pytest.raises(BaselineDriftError):
            await coordinator.finalize(ctx, complete)
        # Backend was never called — drift caught before the AgentLoop.
        assert backend.calls == 0


# ── Slack rendering ────────────────────────────────────────────────


class TestSlackFinalizationRendering:
    def test_present_work_response_has_expected_buttons(self) -> None:
        response = build_present_work_response(
            channel_id="C1",
            thread_ts="T1",
            workflow_id="wf-1",
            summary="done",
        )
        actions = response.blocks[1].actions  # type: ignore[attr-defined]
        assert [button.action_id for button in actions] == [
            FINALIZATION_ACTION_PUSH_PR,
            FINALIZATION_ACTION_ITERATE,
            FINALIZATION_ACTION_DISCARD,
        ]


class TestSlackRendererSectionLimit:
    """Regression: every section block must fit Slack's 3000-char text cap.

    Previously the renderer truncated diffs / errors / action results
    to 4000 chars and crammed them into a single section block.
    Slack rejects any block whose ``section.text.text`` exceeds 3000
    chars with ``invalid_blocks``; the broad ``except`` in ``_post``
    swallowed the rejection and the user saw nothing.

    The fix routes every long body through
    :func:`connectors.slack.connector._split_text_for_slack` (with
    headroom for the surrounding code-fence delimiters).  These tests
    record every block the renderer would post and assert each one
    stays under :data:`_SLACK_SECTION_TEXT_LIMIT`.
    """

    @pytest.fixture
    def captured(self) -> dict[str, list[dict[str, Any]]]:
        return {"blocks": []}

    @pytest.fixture
    def renderer(
        self,
        captured: dict[str, list[dict[str, Any]]],
    ) -> Any:
        from nemoclaw_escapades.connectors.slack.finalization import (
            SlackFinalizationRenderer,
        )

        class _CapturingClient:
            # Mirrors Slack's ``AsyncWebClient.chat_postMessage`` exactly
            # so the renderer's call binds the same way as in production.
            async def chat_postMessage(  # noqa: N802 — Slack API name
                self, **kwargs: Any
            ) -> dict[str, Any]:
                blocks = kwargs.get("blocks") or []
                captured["blocks"].extend(blocks)
                return {"ok": True}

        return SlackFinalizationRenderer(_CapturingClient())

    def _section_texts(
        self,
        blocks: list[dict[str, Any]],
    ) -> list[str]:
        return [
            b["text"]["text"]
            for b in blocks
            if b.get("type") == "section" and "text" in b.get("text", {})
        ]

    @pytest.mark.asyncio
    async def test_long_diff_splits_into_safe_blocks(
        self,
        captured: dict[str, list[dict[str, Any]]],
        renderer: Any,
    ) -> None:
        from nemoclaw_escapades.connectors.slack.connector import (
            _SLACK_SECTION_TEXT_LIMIT,
        )

        big_diff = "diff --git a/x b/x\n" + ("a" * 50) + "\n"
        big_diff = big_diff * 400  # ~28 KB, well over a single section
        ctx = WorkflowContext(
            workflow_id="wf-big",
            task=TaskAssignPayload(
                prompt="x",
                workflow_id="wf-big",
                parent_sandbox_id="o",
                agent_id="a",
                workspace_root="/tmp/wf",
            ),
            channel_id="C1",
            thread_ts="T1",
        )
        await renderer.render_present_work(
            context=ctx,
            summary="ok",
            diff=big_diff,
        )
        section_texts = self._section_texts(captured["blocks"])
        assert section_texts, "expected at least one section block"
        for text in section_texts:
            assert len(text) <= _SLACK_SECTION_TEXT_LIMIT, (
                f"section text {len(text)} chars exceeds Slack cap "
                f"{_SLACK_SECTION_TEXT_LIMIT}"
            )
        # Every diff section is independently fenced — splitting a
        # single fence across blocks would render as raw text in
        # Slack.
        diff_sections = [t for t in section_texts if t.startswith("```diff")]
        assert diff_sections, "expected at least one fenced diff block"
        for section in diff_sections:
            assert section.startswith("```diff"), section[:20]
            assert section.endswith("```"), section[-20:]

    @pytest.mark.asyncio
    async def test_long_error_splits_into_safe_blocks(
        self,
        captured: dict[str, list[dict[str, Any]]],
        renderer: Any,
    ) -> None:
        from nemoclaw_escapades.connectors.slack.connector import (
            _SLACK_SECTION_TEXT_LIMIT,
        )
        from nemoclaw_escapades.nmb.protocol import TaskErrorPayload

        ctx = WorkflowContext(
            workflow_id="wf-err",
            task=TaskAssignPayload(
                prompt="x",
                workflow_id="wf-err",
                parent_sandbox_id="o",
                agent_id="a",
                workspace_root="/tmp/wf",
            ),
            channel_id="C1",
            thread_ts="T1",
        )
        long_traceback = ("Traceback line " + "x" * 80 + "\n") * 100
        await renderer.render_workflow_error(
            context=ctx,
            error=TaskErrorPayload(
                workflow_id="wf-err",
                error=long_traceback,
                error_kind="other",
                recoverable=False,
            ),
        )
        section_texts = self._section_texts(captured["blocks"])
        for text in section_texts:
            assert len(text) <= _SLACK_SECTION_TEXT_LIMIT

    @pytest.mark.asyncio
    async def test_long_action_result_splits_into_safe_blocks(
        self,
        captured: dict[str, list[dict[str, Any]]],
        renderer: Any,
    ) -> None:
        from nemoclaw_escapades.connectors.slack.connector import (
            _SLACK_SECTION_TEXT_LIMIT,
        )

        ctx = WorkflowContext(
            workflow_id="wf-act",
            task=TaskAssignPayload(
                prompt="x",
                workflow_id="wf-act",
                parent_sandbox_id="o",
                agent_id="a",
                workspace_root="/tmp/wf",
            ),
            channel_id="C1",
            thread_ts="T1",
        )
        big_output = "git push output line " * 500
        await renderer.render_finalization_action(
            context=ctx,
            action="push_branch",
            result=big_output,
        )
        section_texts = self._section_texts(captured["blocks"])
        for text in section_texts:
            assert len(text) <= _SLACK_SECTION_TEXT_LIMIT
