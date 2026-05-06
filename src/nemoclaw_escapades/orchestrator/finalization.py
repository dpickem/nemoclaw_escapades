"""Model-driven finalization for completed delegated work.

Per design §7.1, the orchestrator runs a *second* :class:`AgentLoop`
after every ``task.complete`` arrival.  This second loop is bound to
a small, focused tool registry (``tools/finalization.py``) and a
prompt that hands the model the typed completion payload.  The model
synthesises the result and decides what to do next:

- ``present_work_to_user`` — render the work + action buttons to the
  originating thread (default path).
- ``push_branch`` / ``push_and_create_pr`` — git ops.
- ``re_delegate`` — fire a follow-up assignment with iteration feedback.
- ``discard_work`` — wipe the workspace.
- ``destroy_sandbox`` — explicit teardown (no-op in M2b).

The dispatcher invokes :meth:`FinalizationCoordinator.finalize` as
an independent ``asyncio.Task`` so concurrent finalisations run in
parallel and the user-facing chat thread never blocks on sub-agent
work (§8.2).

Two cross-cutting responsibilities live here:

- **Baseline drift detection (§6.6.3)** — compare echoed
  ``WorkspaceBaseline`` against the assigned one *and* re-derive
  ``git diff <base_sha>`` from the local workspace, logging the
  diff verbatim when it disagrees with the sub-agent's reported
  ``diff``.  Mismatch on the echoed baseline raises
  :class:`BaselineDriftError`; finalisation aborts before any git
  side-effects.
- **JSONL audit fallback (§13.2)** — if the sub-agent's
  ``audit.flush`` over NMB never made it (broker hiccup, crash),
  the orchestrator picks up the JSONL file the sub-agent wrote in
  its workspace and ingests it directly.  Idempotent against
  ``ingest_audit_flush`` so double-ingest produces no duplicates.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from nemoclaw_escapades.agent.git_helpers import GitDiffError, diff_against_baseline
from nemoclaw_escapades.agent.loop import AgentLoop
from nemoclaw_escapades.config import AgentLoopConfig
from nemoclaw_escapades.models.types import MessageRole
from nemoclaw_escapades.nmb.protocol import (
    AuditFlushPayload,
    AuditToolCallPayload,
    TaskAssignPayload,
    TaskCompletePayload,
    WorkspaceBaseline,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.finalization import (
    FinalizationSession,
    create_finalization_tool_registry,
)

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB
    from nemoclaw_escapades.backends.base import BackendBase
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager
    from nemoclaw_escapades.orchestrator.workflow import WorkflowContext, WorkflowRenderer

logger = get_logger("orchestrator.finalization")

# ── Constants ──────────────────────────────────────────────────────

# Maximum notes-file characters fed into the finalisation prompt
# before truncation.  Chosen large enough for typical scratchpad
# workflows but small enough to leave headroom for the diff and the
# system prompt within a single inference window.
_NOTES_TRUNCATION_CHARS: int = 20_000

# Default location of the finalisation system prompt (relative to the
# config-resolved ``prompts/`` directory).  Falls back to a short
# inline prompt if the file isn't present, so packaging mistakes
# don't crash finalisation.
_FINALIZATION_PROMPT_FILE: str = "prompts/finalization_agent.md"

# Inline fallback when the prompt file is missing.  Kept terse and
# matches the design's §7.1 expectations.
_FINALIZATION_FALLBACK_PROMPT: str = (
    "You are the orchestrator's finalization agent. A coding sub-agent "
    "has finished a delegated task. You receive the typed task.complete "
    "payload as context and must call exactly one finalization tool. "
    "Default to present_work_to_user; choose re_delegate when the work "
    "is incomplete and recoverable; choose discard_work for clearly "
    "unsafe results."
)


# Type alias for the public coordinator entry point used by the
# delegation tool's "fire-and-forget" path.  Takes a workflow context
# and returns a textual ack — used by tests and the legacy synchronous
# wiring; production goes through :meth:`finalize` via the dispatcher.
Finalizer = Callable[[TaskAssignPayload, TaskCompletePayload], Awaitable[str]]


class BaselineDriftError(RuntimeError):
    """Raised when a sub-agent reports a different baseline than assigned.

    Drives the §6.6.3 echo-match check.  The orchestrator fails
    finalisation early on drift rather than push a diff against the
    wrong base.
    """


@dataclass
class FinalizationResult:
    """Outcome of one finalisation run.

    Attributes:
        workflow_id: The finalised workflow.
        action: Tool the finalisation model chose
            (``"present_work_to_user"`` etc.).  Empty string when the
            model returned text without calling any tool.
        message: User-facing text from the chosen tool, or the
            model's text reply when no tool was called.
        is_terminal: ``True`` when the chosen action ends the
            workflow's lifecycle (push, discard, sandbox-destroy,
            or no-tool degenerate path).  ``False`` for actions
            that keep the workflow alive — ``present_work_to_user``
            (waiting on a user button) and ``re_delegate`` (carrying
            the same ``workflow_id`` into iteration 2).  The
            dispatcher reads this to decide whether to deregister
            the :class:`WorkflowContext`.
    """

    workflow_id: str
    action: str
    message: str
    is_terminal: bool = True


def verify_baseline(task: TaskAssignPayload, complete: TaskCompletePayload) -> None:
    """Echo-match check for ``WorkspaceBaseline`` (§6.6.3 part 1).

    Both ``None`` is fine (non-diff-producing task).  Otherwise the
    completion's baseline must equal the assigned one structurally
    (same repo URL, same branch, same SHA, same shallow flag).

    Raises:
        BaselineDriftError: When the baselines disagree.
    """
    expected = task.workspace_baseline
    actual = complete.workspace_baseline
    if expected is None and actual is None:
        return
    if expected is None or actual is None or _baseline_key(expected) != _baseline_key(actual):
        raise BaselineDriftError(
            "workspace baseline drift: "
            f"assigned={expected.model_dump() if expected else None} "
            f"completed={actual.model_dump() if actual else None}"
        )


def _baseline_key(baseline: WorkspaceBaseline) -> tuple[str, str, str, bool]:
    return (baseline.repo_url, baseline.branch, baseline.base_sha, baseline.is_shallow)


def build_finalization_prompt(task: TaskAssignPayload, complete: TaskCompletePayload) -> str:
    """Render the typed completion payload as the finalisation user prompt.

    Mechanical templating only — the design (§7.1) calls out that
    typed payloads make this step a deterministic format job rather
    than free-form LLM parsing.
    """
    notes = _read_notes(task.workspace_root, complete.notes_path)
    return "\n".join(
        [
            "A coding sub-agent completed a delegated task.",
            "",
            f"Workflow: {task.workflow_id}",
            f"Agent: {task.agent_id}",
            f"Original prompt: {task.prompt}",
            f"Summary: {complete.summary}",
            f"Rounds used: {complete.rounds_used}",
            f"Tool calls made: {complete.tool_calls_made}",
            f"Model used: {complete.model_used or '(unknown)'}",
            f"Suggested next step: {complete.suggested_next_step or '(none)'}",
            "",
            "Files changed:",
            "\n".join(f"- {path}" for path in complete.files_changed) or "(not provided)",
            "",
            "Notes:",
            notes or "(none)",
            "",
            "Diff:",
            complete.diff or "(empty)",
            "",
            "Choose exactly one finalization tool. Prefer present_work_to_user unless "
            "the result is clearly incomplete and needs re_delegate, or unsafe "
            "and needs discard_work.",
        ]
    )


def _read_notes(
    workspace_root: str,
    notes_path: str | None,
    limit: int = _NOTES_TRUNCATION_CHARS,
) -> str:
    if not notes_path:
        return ""
    root = Path(workspace_root).expanduser().resolve()
    path = (root / notes_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "(notes path escaped workspace)"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text) - limit} chars omitted)"
    return text


class FinalizationCoordinator:
    """Per-orchestrator finalisation driver.

    Constructed once at orchestrator startup; the dispatcher calls
    :meth:`finalize` once per ``task.complete`` arrival.  Verification,
    JSONL fallback ingest, and the second :class:`AgentLoop` live here
    rather than on the dispatcher to keep the dispatcher's per-message
    handlers small and to make finalisation testable in isolation.
    """

    def __init__(
        self,
        *,
        backend: BackendBase,
        config: AgentLoopConfig,
        delegation_manager: DelegationManager | None = None,
        audit: AuditDB | None = None,
        renderer: WorkflowRenderer | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Wire the coordinator to its collaborators.

        Args:
            backend: Inference provider for the finalisation
                ``AgentLoop``.  Typically the same backend the
                orchestrator's main loop uses.
            config: ``AgentLoopConfig`` for the finalisation loop.
                Reuses the orchestrator's defaults; the model and
                ``max_tool_rounds`` are scoped to this run only.
            delegation_manager: Required to enable ``re_delegate``
                follow-ups; ``None`` makes ``re_delegate`` a no-op.
            audit: Required to ingest the JSONL audit fallback;
                ``None`` skips that step.
            renderer: Connector-side push surface.  Forwarded into
                the per-workflow :class:`FinalizationSession`.
            system_prompt: Override for the finalisation system
                prompt.  When ``None``, loads
                ``prompts/finalization_agent.md`` if present, else
                falls back to :data:`_FINALIZATION_FALLBACK_PROMPT`.
        """
        self._backend = backend
        self._config = config
        self._delegation_manager = delegation_manager
        self._audit = audit
        self._renderer = renderer
        self._system_prompt = system_prompt or _load_system_prompt()

    async def finalize(
        self,
        context: WorkflowContext,
        complete: TaskCompletePayload,
    ) -> FinalizationResult:
        """Run finalisation for one ``task.complete`` arrival.

        The dispatcher invokes this method as an independent
        ``asyncio.Task`` (§8.2).

        Args:
            context: Workflow context registered by the originating
                ``delegate_task`` call.
            complete: Validated completion payload.

        Returns:
            A :class:`FinalizationResult` describing what the
            finalisation model chose to do.

        Raises:
            BaselineDriftError: When echoed baseline disagrees with
                the assigned one (the dispatcher catches this and
                surfaces the failure to the user via
                ``render_workflow_completion_failure``).
        """
        verify_baseline(context.task, complete)
        await self._verify_diff(context.task, complete)
        await self._ingest_jsonl_fallback(context.task)
        await self._record_completion(context, complete)

        session = FinalizationSession(
            task=context.task,
            complete=complete,
            context=context,
            delegation_manager=self._delegation_manager,
            renderer=self._renderer,
        )
        tools = create_finalization_tool_registry(session)
        loop = AgentLoop(
            backend=self._backend,
            tools=tools,
            config=self._config,
            audit=self._audit,
        )
        messages = [
            {"role": MessageRole.SYSTEM, "content": self._system_prompt},
            {
                "role": MessageRole.USER,
                "content": build_finalization_prompt(context.task, complete),
            },
        ]
        result = await loop.run(messages, request_id=f"finalize-{context.workflow_id}")
        action = session.state.action or "model_response"
        message = session.state.message or result.content
        # When the model returned text without calling any tool
        # (degenerate case), treat the workflow as terminal — there's
        # nothing else for the user to act on.  Otherwise propagate
        # the tool's own ``is_terminal`` flag.
        is_terminal = session.state.is_terminal if session.state.action else True
        return FinalizationResult(
            workflow_id=context.workflow_id,
            action=action,
            message=message,
            is_terminal=is_terminal,
        )

    async def finalize_to_text(
        self,
        task: TaskAssignPayload,
        complete: TaskCompletePayload,
    ) -> str:
        """Headless finalisation entry point (used by tests).

        Builds an ad-hoc :class:`WorkflowContext` (no channel, no
        thread) so unit tests can drive finalisation without standing
        up a connector or dispatcher.  Returns the rendered text.
        """
        from nemoclaw_escapades.orchestrator.workflow import WorkflowContext as _Ctx

        ctx = _Ctx(workflow_id=task.workflow_id, task=task)
        result = await self.finalize(ctx, complete)
        return result.message

    async def _record_completion(
        self,
        context: WorkflowContext,
        complete: TaskCompletePayload,
    ) -> None:
        """Update the audit row for *context*'s workflow with the typed result.

        ``log_delegation_started`` ran in the ``delegate_task`` tool
        before the sub-agent was spawned.  Phase 3b's fire-and-forget
        ``delegate_task`` doesn't see the eventual ``task.complete``
        — that arrives on the dispatcher — so the
        ``log_delegation_complete`` write lands here.

        Best-effort: a DB hiccup must not abort finalisation.
        """
        if self._audit is None:
            return
        try:
            await self._audit.log_delegation_complete(
                workflow_id=context.workflow_id,
                rounds_used=complete.rounds_used,
                tool_calls_made=complete.tool_calls_made,
                model_used=complete.model_used,
                summary=complete.summary,
                diff_size=len(complete.diff.encode()),
            )
        except Exception:  # noqa: BLE001 — DB surface is broad
            logger.warning(
                "log_delegation_complete failed",
                extra={"workflow_id": context.workflow_id},
                exc_info=True,
            )

    async def _verify_diff(
        self,
        task: TaskAssignPayload,
        complete: TaskCompletePayload,
    ) -> None:
        """§6.6.3 part 2: re-derive the diff and warn on disagreement.

        Best-effort.  When the orchestrator can read the workspace
        (M2b same-sandbox case), it runs ``git diff <base_sha>``
        itself and compares to ``complete.diff``; mismatch logs at
        ``WARNING`` level but does not abort.  Failure to re-derive
        the diff (non-git workspace, deleted directory) also logs at
        ``WARNING`` and proceeds — the echo-match in
        :func:`verify_baseline` is the load-bearing check.
        """
        if task.workspace_baseline is None:
            return
        try:
            local_diff = await diff_against_baseline(
                task.workspace_root,
                task.workspace_baseline.base_sha,
            )
        except GitDiffError:
            logger.warning(
                "Diff re-derivation failed; skipping §6.6.3 cross-check",
                extra={
                    "workflow_id": task.workflow_id,
                    "base_sha": task.workspace_baseline.base_sha,
                },
                exc_info=True,
            )
            return
        except Exception:  # noqa: BLE001 — broad git surface
            logger.warning(
                "Unexpected error during diff re-derivation",
                extra={"workflow_id": task.workflow_id},
                exc_info=True,
            )
            return
        if local_diff.strip() != complete.diff.strip():
            logger.warning(
                "Sub-agent diff disagrees with orchestrator-derived diff",
                extra={
                    "workflow_id": task.workflow_id,
                    "reported_bytes": len(complete.diff.encode()),
                    "rederived_bytes": len(local_diff.encode()),
                },
            )

    async def _ingest_jsonl_fallback(self, task: TaskAssignPayload) -> int:
        """Ingest the sub-agent's JSONL audit fallback if NMB flush was missed.

        Returns the number of rows ingested (0 when audit is disabled,
        no fallback file exists, the file is unreadable, or every row
        is malformed).  Idempotent through the audit DB's
        ``IntegrityError`` path: a row that already arrived via
        ``audit.flush`` is silently skipped on duplicate insert.

        **Best-effort.**  This is a recovery step for the happy path
        where the sub-agent's NMB flush already succeeded; a corrupted
        fallback (truncated mid-write by a sub-agent crash, malformed
        line from a future schema, OS-level read failure) must never
        propagate up into :meth:`finalize` and abort the user-facing
        rendering of the sub-agent's actual completed work.  Every
        failure mode is caught locally and logged at ``WARNING``.
        """
        if self._audit is None:
            return 0
        path = Path(task.workspace_root) / ".nemoclaw" / f"audit-{task.workflow_id}.jsonl"
        if not path.is_file():
            return 0
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "Failed to read JSONL audit fallback",
                extra={"workflow_id": task.workflow_id, "path": str(path)},
                exc_info=True,
            )
            return 0
        rows: list[AuditToolCallPayload] = []
        envelope: dict[str, str] = {}
        skipped = 0
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                envelope = {
                    "workflow_id": raw["workflow_id"],
                    "parent_sandbox_id": raw["parent_sandbox_id"],
                    "agent_id": raw["agent_id"],
                    "agent_role": raw.get("agent_role", "coding"),
                }
                rows.append(AuditToolCallPayload.model_validate(raw["tool_call"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValidationError):
                logger.warning(
                    "Skipping malformed JSONL audit row",
                    extra={
                        "workflow_id": task.workflow_id,
                        "path": str(path),
                        "line": line_no,
                    },
                    exc_info=True,
                )
                skipped += 1
                continue
        if skipped:
            logger.info(
                "JSONL audit fallback partial ingest",
                extra={
                    "workflow_id": task.workflow_id,
                    "rows": len(rows),
                    "skipped": skipped,
                },
            )
        if not rows:
            return 0
        payload = AuditFlushPayload(
            workflow_id=envelope["workflow_id"],
            parent_sandbox_id=envelope["parent_sandbox_id"],
            agent_id=envelope["agent_id"],
            agent_role=envelope["agent_role"],
            tool_calls=rows,
        )
        try:
            return await self._audit.ingest_audit_flush(payload)
        except Exception:  # noqa: BLE001 — DB surface is broad, recovery must not abort finalize
            logger.warning(
                "JSONL audit fallback ingest failed",
                extra={"workflow_id": task.workflow_id, "path": str(path)},
                exc_info=True,
            )
            return 0


def _load_system_prompt() -> str:
    """Read the finalisation system prompt from disk; fall back if absent.

    Looks for ``prompts/finalization_agent.md`` relative to the
    package root.  The orchestrator's existing
    :func:`config.load_system_prompt` is the canonical loader; we
    duplicate the lookup here only because the finalisation prompt
    is not part of the standard ``AppConfig`` surface and we don't
    want a load failure to crash orchestrator startup.
    """
    try:
        path = Path(__file__).resolve().parent.parent.parent.parent / _FINALIZATION_PROMPT_FILE
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — fall back to inline default
        logger.debug("Finalisation prompt file unavailable; using fallback", exc_info=True)
    return _FINALIZATION_FALLBACK_PROMPT


__all__ = [
    "BaselineDriftError",
    "FinalizationCoordinator",
    "FinalizationResult",
    "Finalizer",
    "build_finalization_prompt",
    "verify_baseline",
]
