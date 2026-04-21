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
import os
import signal
import sys
import uuid
from pathlib import Path

from nemoclaw_escapades.agent.approval import AutoApproval
from nemoclaw_escapades.agent.loop import AgentLoop
from nemoclaw_escapades.agent.prompt_builder import LayeredPromptBuilder, SourceType
from nemoclaw_escapades.agent.types import AgentSetupBundle
from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.backends.inference_hub import InferenceHubBackend
from nemoclaw_escapades.config import AppConfig, load_system_prompt
from nemoclaw_escapades.observability.logging import get_logger, setup_logging
from nemoclaw_escapades.runtime import (
    RuntimeEnvironment,
    SandboxConfigurationError,
    detect_runtime_environment,
)
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.tool_registry_factory import create_coding_tool_registry


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
    *focused* surface — only the coding suite, no service tools.
    That's the whole point of delegation: give the sub-agent the
    tools it actually needs and nothing else.  Fewer tools in the
    prompt → better selection accuracy (BYOO tutorial §9.2 — same
    rationale as the ``ToolSearch`` meta-tool coming in Phase 4).
    """
    workspace_root = str(Path(bundle.workspace_root).expanduser())
    Path(workspace_root).mkdir(parents=True, exist_ok=True)
    return create_coding_tool_registry(
        workspace_root,
        git_clone_allowed_hosts=config.coding.git_clone_allowed_hosts,
    )


async def _run_task(
    backend: BackendBase,
    tools: ToolRegistry,
    identity_prompt: str,
    bundle: AgentSetupBundle,
    config: AppConfig,
    audit: AuditDB | None,
    logger: object,
) -> str:
    """Run one task end-to-end and return the final assistant text.

    Separate from the entrypoint loop so both CLI mode and NMB mode
    share the same code path.  Intentionally does *not* receive the
    ``request_id`` from outside — the CLI generates one, the NMB
    handler uses the NMB message id.
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
        # Sub-agents use ``config.agent_loop`` directly — loop-runtime
        # knobs (tool-round cap, compaction thresholds) come from YAML
        # / env; prompt-level fields (model / temperature / max_tokens)
        # live on the same dataclass and default to the shared inference
        # defaults.  Operators can tune a sub-agent's model independently
        # from the orchestrator's via the ``agent_loop.model`` YAML key.
        config=config.agent_loop,
        audit=audit,
        # Sub-agents run inside their own sandbox / workspace, so auto-
        # approve writes — any external containment is provided by the
        # sandbox policy, not the approval gate.  The orchestrator
        # (Phase 2) will invoke ``WriteApproval`` on its side before
        # forwarding finalisation actions that touch shared state.
        approval=AutoApproval(),
    )
    result = await loop.run(
        messages=list(messages),
        request_id=bundle.task_id,
    )
    logger.info(  # type: ignore[attr-defined]
        "Coding agent task completed",
        extra={
            "task_id": bundle.task_id,
            "agent_id": bundle.agent_id,
            "rounds": result.rounds,
            "tool_calls_made": result.tool_calls_made,
            "hit_safety_limit": result.hit_safety_limit,
        },
    )
    return result.content


# ── Run modes ──────────────────────────────────────────────────────


async def _run_cli_mode(
    task_description: str,
    workspace_root: str | None,
    config: AppConfig,
    backend: BackendBase,
    audit: AuditDB | None,
    logger: object,
) -> int:
    """Run a single task from a CLI arg and print the result.

    No NMB — the task goes in via ``argv``, the result comes out via
    stdout.  Useful for iterating locally and for the integration
    test.  Workspace defaults to ``config.coding.workspace_root`` if
    the caller doesn't pass one explicitly.
    """
    bundle = AgentSetupBundle(
        task_id=f"cli-{_make_agent_id()}",
        agent_id=_make_agent_id(),
        parent_agent_id="cli",
        task_description=task_description,
        workspace_root=workspace_root or config.coding.workspace_root,
        source_type=SourceType.AGENT,
    )
    tools = _build_tool_registry(config, bundle)
    identity = _load_coding_prompt()
    try:
        content = await _run_task(backend, tools, identity, bundle, config, audit, logger)
    except Exception:  # pragma: no cover - surfaced in logs
        logger.error("Coding agent task failed", exc_info=True)  # type: ignore[attr-defined]
        return 1
    print(content)
    return 0


async def _run_nmb_mode(
    config: AppConfig,
    backend: BackendBase,
    audit: AuditDB | None,
    logger: object,
    shutdown_event: asyncio.Event,
) -> int:
    """Connect to NMB and handle ``task.assign`` messages.

    **Phase 1 skeleton.**  The full orchestrator-side delegation
    protocol (``task.assign`` dispatch, ``task.progress`` relaying,
    ``task.complete.ack``) lands in Phase 2.  This function reserves
    the slot: it imports the NMB client and wires a listener, but
    the receive-loop body is a TODO because the Phase 2 handler
    pattern hasn't been designed yet.

    Running with ``--nmb`` today is supported but idles until
    shutdown; it's useful for sanity-checking that the sub-agent can
    open a broker connection with the config it's been given.
    """
    # NMB client is imported lazily so CLI mode doesn't pay the import
    # cost when NMB isn't being used.
    from nemoclaw_escapades.nmb.client import MessageBus  # noqa: PLC0415

    broker_url = os.environ.get("NMB_URL", "ws://messages.local:9876")
    agent_id = os.environ.get("AGENT_SANDBOX_ID") or f"coding-{_make_agent_id()}"
    logger.info(  # type: ignore[attr-defined]
        "Connecting to NMB broker",
        extra={"broker_url": broker_url, "agent_id": agent_id},
    )
    bus = MessageBus(broker_url=broker_url, sandbox_id=agent_id)
    await bus.connect_with_retry()

    # Phase 2 TODO: implement the receive loop.
    # ``async for msg in bus.listen(): ... parse AgentSetupBundle ...
    # run _run_task ... bus.reply(msg, 'task.complete', {...})``.
    # Until Phase 2, just wait for shutdown so the process stays up
    # and an operator can verify the connection took.
    logger.warning(
        "NMB receive loop is a Phase 2 TODO — sub-agent is idle.  "
        "Use --task for CLI mode until orchestrator delegation lands.",
    )
    try:
        await shutdown_event.wait()
    finally:
        await bus.close()
    # Surprisingly, the process reached here — means shutdown was
    # requested externally, not an error.
    return 0


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
        help="Workspace root (CLI mode only).  Defaults to "
        "``config.coding.workspace_root``.",
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
    5. Audit DB (optional).
    6. Dispatch on run mode (CLI or NMB).
    7. Teardown in reverse order.
    """
    args = _parse_args(argv)

    runtime = detect_runtime_environment()
    if runtime.classification is RuntimeEnvironment.INCONSISTENT:
        raise SandboxConfigurationError(runtime)

    config = AppConfig.load()
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
    audit: AuditDB | None = None
    if config.audit.enabled:
        audit = AuditDB(
            str(Path(config.audit.db_path).expanduser()),
            persist_payloads=config.audit.persist_payloads,
        )
        await audit.open()
        await audit.start_background_writer()

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
                audit=audit,
                logger=logger,
            )
        return await _run_nmb_mode(
            config=config,
            backend=backend,
            audit=audit,
            logger=logger,
            shutdown_event=shutdown_event,
        )
    finally:
        if audit is not None:
            await audit.stop_background_writer()
            await audit.close()
        await backend.close()


def run(argv: list[str] | None = None) -> int:
    """Synchronous entrypoint for ``python -m nemoclaw_escapades.agent``."""
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    sys.exit(run())
