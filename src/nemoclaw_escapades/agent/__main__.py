"""Coding sub-agent entrypoint (``python -m nemoclaw_escapades.agent``).

Stands up a single coding sub-agent process that uses M2a's
``AgentLoop`` with the workspace-rooted coding tool suite (``file`` /
``search`` / ``bash`` / ``git``) and the ``SkillLoader``-discovered
``skill`` tool.  See ``docs/design_m2b.md`` §6.2 (Sub-Agent Entrypoint)
and the Phase 1 exit criteria in §14.

Two run modes:

- **CLI** (``--task "…"``) — run one task with the given description,
  print the result, exit.  No NMB, no orchestrator.  The "standalone"
  path used by the integration test and by developers iterating on
  the coding agent without a full stack up.

- **NMB** (``--nmb``) — connect to the broker, listen for
  ``task.assign`` messages, handle each by running the ``AgentLoop``,
  and reply with ``task.complete``.  The full production path from
  the design.  **Phase 1 ships a skeleton** that wires the
  connection and the handler; the orchestrator's delegation side
  (sending ``task.assign``, collecting results) lands in Phase 2
  (§Phase 2 of the design).  Running ``--nmb`` without an
  orchestrator talking to the broker will idle indefinitely.

The module preserves the same startup discipline as the orchestrator:
runtime self-check → config load → logging → stack assembly.  That
keeps "why didn't the sub-agent start" failures structured and
diagnosable.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import signal
import sys
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from nemoclaw_escapades.agent.approval import AutoApproval
from nemoclaw_escapades.agent.git_helpers import (
    WorkspaceNotAGitRepoError,
    diff_against_baseline,
)
from nemoclaw_escapades.agent.loop import AgentLoop
from nemoclaw_escapades.agent.prompt_builder import LayeredPromptBuilder, SourceType
from nemoclaw_escapades.agent.skill_loader import SkillLoader
from nemoclaw_escapades.agent.types import AgentLoopResult, AgentSetupBundle
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.backends.inference_hub import InferenceHubBackend
from nemoclaw_escapades.config import (
    AgentLoopConfig,
    AppConfig,
    create_coding_agent_config,
    load_dotenv_if_present,
    load_system_prompt,
)
from nemoclaw_escapades.nmb.protocol import (
    TASK_COMPLETE,
    TASK_ERROR,
    PayloadValidationError,
    TaskAssignPayload,
    TaskCompletePayload,
    TaskErrorPayload,
    dump,
    load,
)
from nemoclaw_escapades.observability.logging import (
    _MergingAdapter,
    get_logger,
    setup_logging,
)
from nemoclaw_escapades.runtime import (
    RuntimeEnvironment,
    SandboxConfigurationError,
    detect_runtime_environment,
)
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.tool_registry_factory import create_coding_tool_registry

if TYPE_CHECKING:
    from nemoclaw_escapades.nmb.client import MessageBus
    from nemoclaw_escapades.nmb.models import NMBMessage

# Identity layer for the sub-agent's system prompt.  Written as a
# file so operators can tune it without redeploying.  Falls back to
# the ``prompts/system_prompt.md`` general prompt if missing.
_DEFAULT_CODING_PROMPT: str = "prompts/coding_agent.md"


# ── Stack assembly ─────────────────────────────────────────────────


def _make_agent_id() -> str:
    """Generate a short, filename-safe identifier for this invocation.

    Used as the ``agent_id`` in the default ``AgentSetupBundle`` when
    running in CLI mode, and as the fallback owner for the scratchpad
    skill's notes file.  Truncated to 8 chars because the skill doc
    recommends keeping the id embedded in filenames readable.
    """
    return uuid.uuid4().hex[:8]


def _load_coding_prompt(path: str = _DEFAULT_CODING_PROMPT) -> str:
    """Load the coding-agent identity prompt.

    Uses ``load_system_prompt`` so a missing file falls back to the
    module-level fallback text rather than failing startup.
    """
    resolved = Path(path).expanduser()
    return load_system_prompt(str(resolved))


def _build_tool_registry(config: AppConfig, bundle: AgentSetupBundle) -> ToolRegistry:
    """Build the sub-agent's coding tool registry.

    Unlike the orchestrator's ``build_full_tool_registry`` this is a
    *focused* surface — only the coding suite and the ``skill`` tool,
    no service tools.  That's the whole point of delegation: give the
    sub-agent the tools it actually needs and nothing else.  Fewer
    tools in the prompt → better selection accuracy (BYOO tutorial
    §9.2 — same rationale as the ``ToolSearch`` meta-tool coming in
    Phase 4).

    The ``skill`` tool specifically is load-bearing: the coding
    sub-agent's system prompt (``prompts/coding_agent.md``) instructs
    the model to call ``skill("scratchpad")`` for any task spanning
    more than three tool rounds.  Without the tool registered, the
    model would hallucinate the call and waste rounds on "unknown
    tool" errors.  ``register_skill_tool`` is a no-op when the
    skills directory is empty, so this is safe to wire
    unconditionally when skills are enabled.
    """
    workspace_root = str(Path(bundle.workspace_root).expanduser())
    Path(workspace_root).mkdir(parents=True, exist_ok=True)

    skill_loader: SkillLoader | None = None
    if config.skills.enabled:
        skills_dir = str(Path(config.skills.skills_dir).expanduser())
        skill_loader = SkillLoader(skills_dir)

    return create_coding_tool_registry(
        workspace_root,
        git_clone_allowed_hosts=config.coding.git_clone_allowed_hosts,
        skill_loader=skill_loader,
    )


async def _run_task(
    backend: BackendBase,
    tools: ToolRegistry,
    identity_prompt: str,
    bundle: AgentSetupBundle,
    config: AppConfig,
    logger: logging.Logger | _MergingAdapter,
    *,
    loop_config: AgentLoopConfig | None = None,
) -> AgentLoopResult:
    """Run one task end-to-end and return the loop result.

    Returns ``AgentLoopResult`` (rather than just ``content``) so NMB
    mode can build a ``TaskCompletePayload`` with ``rounds_used`` /
    ``tool_calls_made`` without opening the loop twice.  CLI mode
    reads ``result.content``.

    Args:
        loop_config: Per-task ``AgentLoopConfig`` override.  ``None``
            means "use ``config.agent_loop`` verbatim" — that's the
            CLI-mode path.  NMB mode passes a one-shot config built
            via ``dataclasses.replace`` so per-task ``max_turns`` and
            ``model`` apply for this run only and don't mutate the
            process-wide config (design §6.4 / §6.5.2).
    """
    prompt_builder = LayeredPromptBuilder(
        identity=identity_prompt,
        task_context=f"Workspace root: {bundle.workspace_root}\nTask: {bundle.task_description}",
    )
    messages = prompt_builder.messages_for_inference(
        thread_key=bundle.task_id,
        user_text=bundle.task_description,
        agent_id=bundle.agent_id,
        source_type=bundle.source_type,
    )

    loop = AgentLoop(
        backend=backend,
        tools=tools,
        # Per-task config when supplied (NMB mode), otherwise the
        # shared sub-agent default.  Operators tune the sub-agent's
        # model independently from the orchestrator's via the
        # ``agent_loop.model`` YAML key; per-task overrides live on
        # ``TaskAssignPayload.{max_turns,model}`` and arrive here as
        # a ``dataclasses.replace`` of that default.
        config=loop_config or config.agent_loop,
        # Phase 1 ships the sub-agent without its own ``AuditDB``: the
        # design (``docs/design_m2b.md`` §13) is that sub-agent tool
        # calls accumulate in an in-memory ``AuditBuffer`` and flush
        # to the orchestrator over NMB, which writes them to the
        # single authoritative audit DB.  Opening a second DB here
        # would fight the orchestrator for the ``/sandbox/audit.db``
        # write lock and lose the agent-id attribution.
        # ``AuditBuffer`` lands in Phase 3b alongside the NMB receive
        # loop; until then the sub-agent runs without audit.  Tool
        # invocations still surface in the structured log.
        audit=None,
        # Sub-agents run inside their own sandbox / workspace, so auto-
        # approve writes — any external containment is provided by the
        # sandbox policy, not the approval gate.  The orchestrator's
        # finalisation step (Phase 3b) invokes ``WriteApproval`` on
        # its side before forwarding actions that touch shared state.
        approval=AutoApproval(),
    )
    result = await loop.run(
        messages=list(messages),
        request_id=bundle.task_id,
    )
    logger.info(
        "Coding agent task completed",
        extra={
            "task_id": bundle.task_id,
            "agent_id": bundle.agent_id,
            "rounds": result.rounds,
            "tool_calls_made": result.tool_calls_made,
            "hit_safety_limit": result.hit_safety_limit,
        },
    )
    return result


# ── Run modes ──────────────────────────────────────────────────────


async def _run_cli_mode(
    task_description: str,
    workspace_root: str | None,
    config: AppConfig,
    backend: BackendBase,
    logger: logging.Logger | _MergingAdapter,
) -> int:
    """Run a single task from a CLI arg and print the result.

    No NMB — the task goes in via ``argv``, the result comes out via
    stdout.  Useful for iterating locally and for the integration
    test.  Workspace defaults to ``config.coding.workspace_root`` if
    the caller doesn't pass one explicitly.

    Each invocation gets its own ``agent-<agent_id>`` subdirectory
    under the base workspace so two concurrent CLI runs (or a cron
    job and an interactive session) don't clobber each other's
    ``notes-<task-slug>-<agent-id>.md`` scratch files.  Matches the
    design §4.2 isolation invariant: the sub-agent's ``agent_id``
    appears in both the workspace path and the scratchpad filename.
    """
    agent_id = _make_agent_id()
    base_workspace = Path(workspace_root or config.coding.workspace_root).expanduser()
    per_agent_workspace = base_workspace / f"agent-{agent_id}"
    bundle = AgentSetupBundle(
        task_id=f"cli-{_make_agent_id()}",
        agent_id=agent_id,
        parent_agent_id="cli",
        task_description=task_description,
        workspace_root=str(per_agent_workspace),
        source_type=SourceType.AGENT,
    )
    tools = _build_tool_registry(config, bundle)
    identity = _load_coding_prompt()
    try:
        result = await _run_task(backend, tools, identity, bundle, config, logger)
    except Exception:  # pragma: no cover - surfaced in logs
        logger.error("Coding agent task failed", exc_info=True)
        return 1
    print(result.content)
    return 0


async def _run_nmb_mode(
    config: AppConfig,
    backend: BackendBase,
    logger: logging.Logger | _MergingAdapter,
    shutdown_event: asyncio.Event,
) -> int:
    """Connect to NMB and handle exactly one ``task.assign``.

    Single-shot per process: connect, receive the first
    ``task.assign``, run the loop, send ``task.complete`` (or
    ``task.error``), close the bus, return.  Concurrency at the
    workflow level lives on the orchestrator's side via per-agent
    semaphores (§8.1) and per-task spawning, not in the sub-agent.

    Matches the M3 shape where ``openshell sandbox create`` will
    spawn one sandbox per task — the M2b → M3 migration only changes
    the spawn mechanism, not the sub-agent's lifecycle.

    Returns:
        Process exit code.  ``0`` on a clean ``task.complete``,
        ``0`` on a handled ``task.error`` (the error's been delivered
        upstream), ``1`` on anything that prevented us from even
        opening the connection.
    """
    # NMB client is imported lazily so CLI mode doesn't pay the import
    # cost when NMB isn't being used.
    from nemoclaw_escapades.nmb.client import MessageBus  # noqa: PLC0415

    # Every field comes from ``config.nmb`` — the raw-env-var reads
    # this used to do (``NMB_URL``, ``AGENT_SANDBOX_ID``) are now
    # per-field env *overrides* on the dataclass, applied during
    # ``AppConfig.load``.  That keeps non-secret config flowing
    # through the YAML overlay inside the sandbox (design §5.3).
    broker_url = config.nmb.broker_url
    agent_sandbox_id = config.nmb.sandbox_id or f"coding-{_make_agent_id()}"
    logger.info(
        "Connecting to NMB broker",
        extra={"broker_url": broker_url, "sandbox_id": agent_sandbox_id},
    )
    bus = MessageBus(broker_url=broker_url, sandbox_id=agent_sandbox_id)
    try:
        await bus.connect_with_retry()
    except Exception:
        logger.error("Failed to connect to NMB broker", exc_info=True)
        return 1

    try:
        return await _await_and_handle_one_task(bus, config, backend, logger, shutdown_event)
    finally:
        await bus.close()


async def _await_and_handle_one_task(
    bus: MessageBus,
    config: AppConfig,
    backend: BackendBase,
    logger: logging.Logger | _MergingAdapter,
    shutdown_event: asyncio.Event,
) -> int:
    """Wait for a ``task.assign``, run it, reply, return exit code.

    Split out from ``_run_nmb_mode`` so the bus lifecycle (connect /
    close) stays in one obvious place and the per-task logic is
    testable in isolation against a stub bus.
    """
    assign_msg = await _wait_for_assign(bus, logger, shutdown_event)
    if assign_msg is None:
        # Shutdown received before any task arrived — clean exit.
        return 0

    try:
        task = load(TaskAssignPayload, "task.assign", assign_msg.payload)
    except PayloadValidationError as exc:
        logger.error(
            "Rejected malformed task.assign",
            extra={"validation_error": str(exc), "from": assign_msg.from_sandbox},
        )
        # Reply with task.error so the orchestrator's listener doesn't
        # hang waiting for a complete that will never come.
        await _reply_validation_error(bus, assign_msg, exc)
        return 0

    logger.info(
        "Accepted task.assign",
        extra={
            "workflow_id": task.workflow_id,
            "agent_id": task.agent_id,
            "max_turns": task.max_turns,
            "model": task.model,
            "is_iteration": task.is_iteration,
        },
    )

    try:
        complete = await _run_assigned_task(task, config, backend, logger)
    except Exception as exc:
        # Don't crash the process — emit a structured task.error so
        # the orchestrator's finalisation flow can surface the failure
        # to the user instead of timing out on a missing reply.
        error_payload = TaskErrorPayload(
            workflow_id=task.workflow_id,
            error=f"{type(exc).__name__}: {exc}",
            error_kind=_classify_error(exc),
            recoverable=_is_recoverable(exc),
            traceback=traceback.format_exc(),
        )
        await bus.reply(assign_msg, type=TASK_ERROR, payload=dump(error_payload))
        logger.error(
            "Sent task.error",
            extra={
                "workflow_id": task.workflow_id,
                "error_kind": error_payload.error_kind,
            },
            exc_info=True,
        )
        return 0

    try:
        await bus.reply(assign_msg, type=TASK_COMPLETE, payload=dump(complete))
    except Exception:
        # The task succeeded; only delivery of the success payload failed.
        # Do not reclassify this as task.error, otherwise the orchestrator
        # may discard completed work or offer a spurious re-delegate path.
        logger.error(
            "Failed to send task.complete after successful task",
            extra={
                "workflow_id": task.workflow_id,
                "rounds_used": complete.rounds_used,
                "tool_calls_made": complete.tool_calls_made,
            },
            exc_info=True,
        )
        return 1

    logger.info(
        "Sent task.complete",
        extra={
            "workflow_id": task.workflow_id,
            "rounds_used": complete.rounds_used,
            "tool_calls_made": complete.tool_calls_made,
        },
    )
    return 0


async def _wait_for_assign(
    bus: MessageBus,
    logger: logging.Logger | _MergingAdapter,
    shutdown_event: asyncio.Event,
) -> NMBMessage | None:
    """Wait for the next message on the bus, racing against shutdown.

    Returns ``None`` if shutdown was requested before any message
    arrived; otherwise returns the first :class:`NMBMessage` whose
    ``type == "task.assign"``.  Other types are logged and ignored —
    the sub-agent's contract is "I run one task and exit", so a
    ``task.progress`` or ``task.complete`` arriving here means the
    orchestrator is talking to the wrong sandbox or replaying an
    old message.

    The single ``async for msg in bus.listen()`` pattern is what NMB
    expects (see ``nmb/client.py``); we layer a shutdown-event race
    on top so SIGINT / SIGTERM during connection idle exits cleanly.
    """
    listen_task = asyncio.create_task(_first_assign(bus))
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    try:
        done, _ = await asyncio.wait(
            {listen_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            logger.info("Shutdown signal received while awaiting task.assign")
            listen_task.cancel()
            return None
        return listen_task.result()
    finally:
        if not listen_task.done():
            listen_task.cancel()
        if not shutdown_task.done():
            shutdown_task.cancel()


async def _first_assign(bus: MessageBus) -> NMBMessage:
    """Return the first ``task.assign`` message off the bus.

    Lazily skips over non-``task.assign`` deliveries so a stray
    message from a confused orchestrator doesn't crash the sub-agent.
    """
    async for msg in bus.listen():
        if msg.type == "task.assign":
            return msg
        # Else: log and keep waiting; an unsolicited task.complete
        # for a different workflow is a misroute, not a fatal error.
    raise RuntimeError("NMB listen() ended without delivering a task.assign")


async def _run_assigned_task(
    task: TaskAssignPayload,
    config: AppConfig,
    backend: BackendBase,
    logger: logging.Logger | _MergingAdapter,
) -> TaskCompletePayload:
    """Run one validated task and return the success payload.

    Builds a per-task ``AgentLoopConfig`` via ``dataclasses.replace``
    so per-task ``max_turns`` and ``model`` apply for this run only.
    The process-wide ``config.agent_loop`` is untouched (design §6.4
    / §6.5.2).

    Computes the baseline-anchored diff (``git diff <base_sha>``,
    working-tree-inclusive) when the orchestrator pinned a
    ``workspace_baseline``; otherwise leaves ``diff`` empty per §6.6.

    Hitting the per-task ``max_turns`` cap is **not** a success.
    Per design §6.4 the sub-agent must surface that as a recoverable
    ``task.error`` so the orchestrator's finalisation step (Phase
    3b) can offer the user a ``re_delegate`` button.  We raise
    :class:`MaxTurnsExceededError` here and let the existing
    ``_classify_error`` path build a ``TaskErrorPayload`` with
    ``error_kind="max_turns_exceeded"`` and ``recoverable=True``.

    Raises:
        MaxTurnsExceededError: When the loop hit its per-task
            ``max_tool_rounds`` cap.  Carries the partial assistant
            content + tool-call counters for inclusion in the error
            payload.
        Whatever the underlying ``AgentLoop`` raises.  The caller
        (``_await_and_handle_one_task``) wraps these into a
        ``task.error`` payload — this function never builds a
        ``TaskErrorPayload`` itself.
    """
    bundle = AgentSetupBundle(
        task_id=task.workflow_id,
        agent_id=task.agent_id,
        parent_agent_id=task.parent_sandbox_id,
        task_description=task.prompt,
        workspace_root=task.workspace_root,
        source_type=SourceType.AGENT,
    )
    Path(task.workspace_root).expanduser().mkdir(parents=True, exist_ok=True)

    tools = _build_tool_registry(config, bundle)
    identity = _load_coding_prompt()

    # Per-task ``AgentLoopConfig`` — only override the fields the
    # orchestrator pinned, fall back to YAML/env defaults for the
    # rest.  ``replace`` returns a fresh dataclass; the caller's
    # ``config.agent_loop`` is untouched.
    overrides: dict[str, object] = {}
    if task.max_turns is not None:
        overrides["max_tool_rounds"] = task.max_turns
    if task.model is not None:
        overrides["model"] = task.model
    loop_config = (
        dataclasses.replace(config.agent_loop, **overrides)  # type: ignore[arg-type]
        if overrides
        else config.agent_loop
    )

    result = await _run_task(
        backend,
        tools,
        identity,
        bundle,
        config,
        logger,
        loop_config=loop_config,
    )

    if result.hit_safety_limit:
        # Design §6.4: the orchestrator's finalisation step needs to
        # know this was a max-turns failure (recoverable, can offer
        # re_delegate), not a successful task with a small diff.
        raise MaxTurnsExceededError(
            max_tool_rounds=loop_config.max_tool_rounds,
            rounds_used=result.rounds,
            tool_calls_made=result.tool_calls_made,
            partial_summary=result.content,
        )

    diff = await _compute_baseline_diff(task, logger)
    return TaskCompletePayload(
        workflow_id=task.workflow_id,
        summary=result.content or "(empty response)",
        diff=diff,
        workspace_baseline=task.workspace_baseline,
        tool_calls_made=result.tool_calls_made,
        rounds_used=result.rounds,
        model_used=loop_config.model,
    )


async def _compute_baseline_diff(
    task: TaskAssignPayload,
    logger: logging.Logger | _MergingAdapter,
) -> str:
    """Compute ``git diff <base_sha>..HEAD`` against the assigned baseline.

    Returns the empty string when no baseline was pinned (the
    orchestrator's "non-diff-producing task" signal — §6.6).  Logs
    and returns empty on git failure rather than raising; the diff
    is informational, the source of truth is the orchestrator's
    re-derivation at finalisation (§6.6.3).
    """
    if task.workspace_baseline is None:
        return ""
    try:
        diff = await diff_against_baseline(task.workspace_root, task.workspace_baseline.base_sha)
    except WorkspaceNotAGitRepoError:
        logger.warning(
            "Workspace is not a git repo; skipping baseline diff",
            extra={"workflow_id": task.workflow_id, "workspace": task.workspace_root},
        )
        return ""
    if diff.startswith(("Exit code:", "Error:")):
        logger.warning(
            "git diff failed",
            extra={"workflow_id": task.workflow_id, "stderr": diff[:500]},
        )
        return ""
    return diff


async def _reply_validation_error(
    bus: MessageBus,
    assign_msg: NMBMessage,
    exc: PayloadValidationError,
) -> None:
    """Reply with a ``task.error`` for a payload that failed validation.

    Without a workflow_id (the very thing we couldn't parse), we use
    the original NMB message id as a workflow placeholder — the
    orchestrator's listener can correlate via the reply-to chain.
    """
    error = TaskErrorPayload(
        workflow_id=assign_msg.id,
        error=f"task.assign payload validation failed: {exc}",
        error_kind="other",
        recoverable=False,
    )
    await bus.reply(assign_msg, type=TASK_ERROR, payload=dump(error))


_ErrorKind = Literal[
    "max_turns_exceeded",
    "tool_failure",
    "policy_denied",
    "inference_error",
    "other",
]


class MaxTurnsExceededError(Exception):
    """Raised when the loop hit its per-task ``max_tool_rounds`` cap.

    Distinguished from a generic loop failure so
    :func:`_classify_error` can map it to the recoverable
    ``"max_turns_exceeded"`` ``error_kind`` per design §6.4.  Carries
    the partial assistant content + counters so the eventual
    ``task.error`` payload tells the orchestrator's finalisation
    model how much of the budget the sub-agent burned and what
    progress (if any) was made before the cap fired — enough context
    to decide between ``re_delegate`` (with a higher cap) and
    ``discard_work``.
    """

    def __init__(
        self,
        *,
        max_tool_rounds: int,
        rounds_used: int,
        tool_calls_made: int,
        partial_summary: str,
    ) -> None:
        super().__init__(
            f"max_tool_rounds={max_tool_rounds} exceeded after "
            f"{rounds_used} rounds, {tool_calls_made} tool calls"
        )
        self.max_tool_rounds = max_tool_rounds
        self.rounds_used = rounds_used
        self.tool_calls_made = tool_calls_made
        self.partial_summary = partial_summary


def _classify_error(exc: BaseException) -> _ErrorKind:
    """Map a Python exception type to a ``TaskErrorPayload.error_kind``.

    The five literal values come from the design (§6.3, §6.5).
    Default is ``"other"`` — we'd rather be honest about an
    unclassified failure than mis-bucket it.
    """
    if isinstance(exc, MaxTurnsExceededError):
        return "max_turns_exceeded"
    if isinstance(exc, TimeoutError):
        return "tool_failure"
    name = type(exc).__name__
    if "Inference" in name or "Backend" in name:
        return "inference_error"
    if "Approval" in name or "Policy" in name:
        return "policy_denied"
    return "other"


def _is_recoverable(exc: BaseException) -> bool:
    """Whether *exc* is the kind of failure ``re_delegate`` could fix.

    Per design §6.4 only ``max_turns_exceeded`` is considered
    recoverable today: bumping the cap and re-running the same
    prompt against the same baseline is a sensible response.  Tool
    failures, policy denials, and inference errors all need a
    different prompt or a code change, not just another turn —
    finalisation surfaces them as terminal errors so the user can
    decide.
    """
    return isinstance(exc, MaxTurnsExceededError)


# ── Entrypoint ─────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Mutually exclusive run modes: ``--task`` (CLI) or ``--nmb``
    (broker).  Exactly one must be supplied.
    """
    parser = argparse.ArgumentParser(
        prog="python -m nemoclaw_escapades.agent",
        description="Run a coding sub-agent — CLI mode or NMB mode.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--task",
        type=str,
        help="Run one task from the command line.  Prints the final "
        "assistant reply to stdout and exits.",
    )
    mode.add_argument(
        "--nmb",
        action="store_true",
        help="Connect to the NMB broker and wait for task.assign "
        "messages.  Orchestrator-side delegation lands in Phase 2; "
        "this mode currently idles until shutdown.",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Workspace root (CLI mode only).  Defaults to ``config.coding.workspace_root``.",
    )
    return parser.parse_args(argv)


async def _async_main(argv: list[str] | None = None) -> int:
    """Async entrypoint — orchestrates the startup sequence.

    Steps mirror the orchestrator's ``main.py``:

    0. Parse CLI args.
    1. Runtime self-check (fail fast on ``INCONSISTENT``).
    2. Config load (dataclass → YAML → env → validate).
    3. Logging setup.
    4. Inference backend.
    5. Dispatch on run mode (CLI or NMB).
    6. Teardown in reverse order.

    The sub-agent deliberately does *not* open its own ``AuditDB``:
    Phase 2 introduces an ``AuditBuffer`` that flushes over NMB to the
    orchestrator's single authoritative DB.  See ``docs/design_m2b.md``
    §13.
    """
    args = _parse_args(argv)

    # Pick up ``.env`` at the current working directory so
    # ``python -m nemoclaw_escapades.agent --task ...`` finds the
    # operator's ``INFERENCE_HUB_*`` credentials without requiring a
    # shell-level ``export``.  Idempotent + ``override=False`` so
    # shell-set vars still win.  No-op inside the sandbox (no
    # ``.env`` file shipped) and in tests that run from a ``tmp_path``.
    load_dotenv_if_present()

    runtime = detect_runtime_environment()
    if runtime.classification is RuntimeEnvironment.INCONSISTENT:
        raise SandboxConfigurationError(runtime)

    # ``create_coding_agent_config`` skips Slack validation — the
    # sub-agent never connects to Slack (CLI mode prints to stdout,
    # NMB mode talks to the broker).  The orchestrator's ``main.py``
    # uses ``create_orchestrator_config`` which requires Slack tokens.
    config = create_coding_agent_config()
    setup_logging(level=config.log.level, log_file=config.log.log_file)
    logger = get_logger("agent.main")
    logger.info(
        "Starting coding sub-agent",
        extra={
            "runtime_classification": runtime.classification.value,
            "mode": "cli" if args.task else "nmb",
        },
    )

    backend = InferenceHubBackend(config.inference)
    # No AuditDB on the sub-agent side: Phase 2 introduces an in-memory
    # ``AuditBuffer`` that flushes over NMB to the orchestrator's single
    # authoritative audit DB (``docs/design_m2b.md`` §13).  For Phase 1
    # the sub-agent's tool calls are captured via the structured log.

    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        if args.task:
            return await _run_cli_mode(
                task_description=args.task,
                workspace_root=args.workspace,
                config=config,
                backend=backend,
                logger=logger,
            )
        return await _run_nmb_mode(
            config=config,
            backend=backend,
            logger=logger,
            shutdown_event=shutdown_event,
        )
    finally:
        await backend.close()


def run(argv: list[str] | None = None) -> int:
    """Synchronous entrypoint for ``python -m nemoclaw_escapades.agent``."""
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    sys.exit(run())
