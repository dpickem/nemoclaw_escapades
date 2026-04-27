"""Entry point for the NemoClaw agent loop.

Assembles the full runtime stack and keeps it alive until a shutdown
signal arrives.  The wiring order is:

1. **Config** — ``load_config()`` reads env vars / files into typed
   dataclasses (``AppConfig``).  Inside an OpenShell sandbox, real
   credentials are never present in the environment; they are injected
   by the L7 proxy at request time via ``openshell:resolve:env:…``
   placeholders.  The config layer only sees the placeholder strings —
   the proxy transparently resolves them before forwarding each HTTP
   request to the upstream service.
2. **Inference backend** — ``InferenceHubBackend`` wraps the
   OpenAI-compatible chat-completions endpoint (Inference Hub or
   ``inference.local`` inside an OpenShell sandbox).
3. **Tool registry** — optional; when enabled, tool modules (e.g.
   ``register_jira_tools``) populate the registry with ``ToolSpec``
   entries that the orchestrator can invoke during the agent loop.
4. **Orchestrator** — the agent loop itself: prompt building,
   multi-turn tool use, transcript repair, and approval gating.
5. **Connector** — ``SlackConnector`` opens a socket-mode WebSocket
   to Slack and bridges platform events to ``orchestrator.handle()``.

After ``connector.start()`` the process blocks on an ``asyncio.Event``
until SIGINT or SIGTERM.  A second signal forces an immediate exit.
Shutdown tears down the connector and backend in reverse order.

``run()`` is the synchronous entry point invoked by the Makefile and
CLI (``python -m nemoclaw_escapades``).
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from nemoclaw_escapades.agent.approval import WriteApproval
from nemoclaw_escapades.agent.skill_loader import SkillLoader
from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.backends.inference_hub import InferenceHubBackend
from nemoclaw_escapades.config import create_orchestrator_config, load_dotenv_if_present
from nemoclaw_escapades.connectors.slack import SlackConnector
from nemoclaw_escapades.nmb.client import MessageBus
from nemoclaw_escapades.observability.logging import get_logger, setup_logging
from nemoclaw_escapades.orchestrator.delegation import DelegationManager
from nemoclaw_escapades.orchestrator.orchestrator import Orchestrator
from nemoclaw_escapades.runtime import (
    RuntimeEnvironment,
    SandboxConfigurationError,
    detect_runtime_environment,
)
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.tool_registry_factory import build_full_tool_registry


async def main() -> None:
    # No-op inside the sandbox — no ``.env`` file ships with the
    # image; every secret is an OpenShell-provider placeholder
    # injected by the gateway.  The call is retained because
    # host-side dev helpers (``make run-broker``, scripts) share
    # this entrypoint and benefit from the convenience.
    load_dotenv_if_present()

    # ── 0. Runtime self-check ─────────────────────────────────────
    # Evaluate sandbox signals *before* config loading so a broken
    # deployment (OpenShell version drift, gateway misconfigured,
    # sandbox env vars leaked into a local shell) fails fast with a
    # structured diagnostic instead of silently booting with the
    # wrong defaults.
    runtime = detect_runtime_environment()
    if runtime.classification is RuntimeEnvironment.INCONSISTENT:
        # Logging isn't configured yet (we haven't loaded config); emit
        # via the detector's own logger, which uses whatever the root
        # handler defaults to.  That's fine for a fatal startup error.
        raise SandboxConfigurationError(runtime)

    # ── 1. Configuration ──────────────────────────────────────────
    # Dataclass defaults → YAML overlay (``/app/config.yaml``) →
    # secret env vars.  Credentials are L7-proxy placeholders
    # resolved at HTTP-request time — the config layer never sees
    # real secrets.  The ``runtime`` self-check above already
    # confirmed we're in a healthy sandbox, so the loader itself
    # doesn't need to branch on that classification.
    config = create_orchestrator_config()
    setup_logging(level=config.log.level, log_file=config.log.log_file)

    logger = get_logger("main")
    logger.info(
        "Starting NemoClaw agent loop",
        extra={
            "runtime_classification": runtime.classification.value,
            "runtime_signals_present": list(runtime.signals_present),
            "runtime_signals_missing": list(runtime.signals_missing),
        },
    )

    # ── 2. Inference backend ──────────────────────────────────────
    backend = InferenceHubBackend(config.inference)

    # ── 3. Skill loader (optional) ────────────────────────────────
    # Scans the skills directory at startup for SKILL.md files.  The
    # loader itself is harmless when the directory is empty — only the
    # subsequent register_skill_tool call is skipped.
    skill_loader: SkillLoader | None = None
    if config.skills.enabled:
        skills_dir = str(Path(config.skills.skills_dir).expanduser())
        skill_loader = SkillLoader(skills_dir)
        logger.info(
            "Skill loader initialised",
            extra={"skills_dir": skills_dir, "count": len(skill_loader.skills)},
        )

    # ── 4. Audit DB ───────────────────────────────────────────────
    # SQLite database for tool-call + delegation logging.  Built
    # *before* the tool registry so ``delegate_task`` can route
    # ``log_delegation_*`` writes through the same DB the rest of
    # the orchestrator uses.
    audit: AuditDB | None = None
    if config.audit.enabled:
        audit_path = str(Path(config.audit.db_path).expanduser())
        audit = AuditDB(audit_path, persist_payloads=config.audit.persist_payloads)
        await audit.open()
        await audit.start_background_writer()
        logger.info("Audit DB opened", extra={"path": audit_path})

    # ── 5. NMB bus + DelegationManager (optional) ─────────────────
    # ``delegate_task`` needs a live ``MessageBus`` and a
    # ``DelegationManager`` to actually reach a sub-agent.  Both are
    # constructed here so the registry factory can register the tool
    # in the same step.  A broker that's unreachable is *not* fatal —
    # we log and run degraded (no delegation), since the orchestrator
    # can still serve interactive coding requests.  Operators who
    # don't want delegation at all set ``delegation.enabled=false``
    # in YAML to skip the bus dial entirely.
    nmb_bus: MessageBus | None = None
    delegation_manager: DelegationManager | None = None
    if config.delegation.enabled:
        nmb_bus = MessageBus(
            broker_url=config.nmb.broker_url,
            sandbox_id=config.nmb.sandbox_id or "orchestrator",
        )
        try:
            await nmb_bus.connect_with_retry()
            delegation_manager = DelegationManager(nmb_bus, config.delegation)
            logger.info(
                "Delegation enabled",
                extra={
                    "broker_url": config.nmb.broker_url,
                    "sandbox_id": nmb_bus.sandbox_id,
                    "max_concurrent": config.delegation.max_concurrent,
                },
            )
        except Exception:
            logger.warning(
                "NMB broker unreachable — running without delegation",
                exc_info=True,
                extra={"broker_url": config.nmb.broker_url},
            )
            try:
                await nmb_bus.close()
            except Exception:
                pass
            nmb_bus = None
            delegation_manager = None

    # ── 6. Tool registry ──────────────────────────────────────────
    # Single-call factory builds the process-wide registry from config.
    # ``delegate_task`` is registered iff ``delegation_manager`` is set;
    # see ``tools/tool_registry_factory.py``.
    registry = build_full_tool_registry(
        config,
        skill_loader=skill_loader,
        delegation_manager=delegation_manager,
        audit=audit,
    )

    tools: ToolRegistry | None
    if registry.names:
        tools = registry
        logger.info(
            "Tools registered",
            extra={"count": len(registry), "toolsets": sorted(registry.toolsets)},
        )
    else:
        tools = None

    # ── 7. Orchestrator + connector ───────────────────────────────
    # The orchestrator owns the agent loop; the connector bridges
    # Slack events to orchestrator.handle().
    orchestrator = Orchestrator(
        backend,
        config.orchestrator,
        agent_loop=config.agent_loop,
        approval=WriteApproval(),
        tools=tools,
        audit=audit,
    )
    connector = SlackConnector(
        handler=orchestrator.handle,
        bot_token=config.slack.bot_token,
        app_token=config.slack.app_token,
    )

    # ── 8. Signal handling ────────────────────────────────────────
    # First SIGINT/SIGTERM triggers graceful shutdown; a second one
    # forces immediate exit (useful when teardown hangs).
    shutdown_event = asyncio.Event()
    _shutting_down = False

    def _signal_handler() -> None:
        nonlocal _shutting_down
        if _shutting_down:
            logger.info("Forced exit")
            sys.exit(130)
        _shutting_down = True
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # ── 9. Run until shutdown ─────────────────────────────────────
    try:
        await connector.start()
        logger.info("NemoClaw is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
    except Exception:
        logger.error("Fatal error during startup", exc_info=True)
        sys.exit(1)
    finally:
        # Teardown in reverse order: connector → bus → audit → backend.
        logger.info("Shutting down...")
        await connector.stop()
        if nmb_bus is not None:
            try:
                await nmb_bus.close()
                logger.info("NMB bus closed")
            except Exception:
                logger.warning("NMB bus close failed", exc_info=True)
        if audit:
            await audit.stop_background_writer()
            await audit.close()
            logger.info("Audit DB closed")
        await backend.close()
        logger.info("Shutdown complete")


def run() -> None:
    """Synchronous entry point for use in Makefile / CLI."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
