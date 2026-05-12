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
3. **Audit DB** — SQLite database for tool-call + delegation logging.
4. **NMB bus** + delegation manager + workflow dispatcher — Phase 3b
   centralised event loop (design §8.2).  The dispatcher owns
   ``bus.listen()``; the delegation manager fires ``task.assign`` and
   returns immediately; finalisation runs as an independent
   ``asyncio.Task`` per ``task.complete`` arrival.
5. **Tool registry** — optional; when enabled, tool modules (e.g.
   ``register_jira_tools``) populate the registry with ``ToolSpec``
   entries that the orchestrator can invoke during the agent loop.
6. **Orchestrator** — the agent loop itself: prompt building,
   multi-turn tool use, transcript repair, and approval gating.
7. **Connector** — ``SlackConnector`` opens a socket-mode WebSocket
   to Slack and bridges platform events to ``orchestrator.handle()``.

After ``connector.start()`` the process blocks on an ``asyncio.Event``
until SIGINT or SIGTERM.  A second signal forces an immediate exit.
Shutdown tears down the connector, dispatcher, delegation manager,
audit DB, and backend in reverse order.

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
from nemoclaw_escapades.connectors.base import StatusCallback
from nemoclaw_escapades.connectors.slack import SlackConnector, SlackFinalizationRenderer
from nemoclaw_escapades.models.types import NormalizedRequest, RichResponse
from nemoclaw_escapades.nmb.client import MessageBus
from nemoclaw_escapades.observability.logging import get_logger, setup_logging
from nemoclaw_escapades.orchestrator.delegation import DelegationManager
from nemoclaw_escapades.orchestrator.dispatcher import WorkflowDispatcher
from nemoclaw_escapades.orchestrator.finalization import FinalizationCoordinator
from nemoclaw_escapades.orchestrator.finalization_actions import FinalizationActionHandler
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
    runtime = detect_runtime_environment()
    if runtime.classification is RuntimeEnvironment.INCONSISTENT:
        raise SandboxConfigurationError(runtime)

    # ── 1. Configuration ──────────────────────────────────────────
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
    skill_loader: SkillLoader | None = None
    if config.skills.enabled:
        skills_dir = str(Path(config.skills.skills_dir).expanduser())
        skill_loader = SkillLoader(skills_dir)
        logger.info(
            "Skill loader initialised",
            extra={"skills_dir": skills_dir, "count": len(skill_loader.skills)},
        )

    # ── 4. Audit DB ───────────────────────────────────────────────
    audit: AuditDB | None = None
    if config.audit.enabled:
        audit_path = str(Path(config.audit.db_path).expanduser())
        audit = AuditDB(audit_path, persist_payloads=config.audit.persist_payloads)
        await audit.open()
        await audit.start_background_writer()
        logger.info("Audit DB opened", extra={"path": audit_path})

    # ── 5. NMB bus + delegation manager + dispatcher ──────────────
    # Phase 3b architecture (design §8.2): the dispatcher owns
    # ``bus.listen()``, the delegation manager is fire-and-forget,
    # and finalisation runs as independent asyncio tasks.
    nmb_bus: MessageBus | None = None
    delegation_manager: DelegationManager | None = None
    dispatcher: WorkflowDispatcher | None = None
    finalization_actions: FinalizationActionHandler | None = None
    slack_connector: SlackConnector | None = None
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
    # Build a placeholder Slack connector early so the renderer can
    # attach to its already-authenticated client.  The connector is
    # configured but not started yet; ``connector.start()`` runs in
    # step 9 once the orchestrator + dispatcher are ready.
    if delegation_manager is not None:
        renderer: SlackFinalizationRenderer | None = None
        slack_connector = SlackConnector(
            handler=_placeholder_handler,
            bot_token=config.slack.bot_token,
            app_token=config.slack.app_token,
        )
        renderer = SlackFinalizationRenderer(slack_connector.client)

        finalizer = FinalizationCoordinator(
            backend=backend,
            config=config.agent_loop,
            delegation_manager=delegation_manager,
            audit=audit,
            renderer=renderer,
        )
        assert nmb_bus is not None  # set in step 5 alongside delegation_manager
        dispatcher = WorkflowDispatcher(
            nmb_bus,
            audit=audit,
            finalizer=finalizer,
            renderer=renderer,
            delegation_manager=delegation_manager,
        )
        await dispatcher.start()
        finalization_actions = FinalizationActionHandler(
            dispatcher=dispatcher,
            delegation_manager=delegation_manager,
            renderer=renderer,
        )

    registry = build_full_tool_registry(
        config,
        skill_loader=skill_loader,
        delegation_manager=delegation_manager,
        dispatcher=dispatcher,
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
    orchestrator = Orchestrator(
        backend,
        config.orchestrator,
        agent_loop=config.agent_loop,
        approval=WriteApproval(),
        tools=tools,
        audit=audit,
        finalization_action_handler=finalization_actions,
    )
    if slack_connector is None:
        slack_connector = SlackConnector(
            handler=orchestrator.handle,
            bot_token=config.slack.bot_token,
            app_token=config.slack.app_token,
        )
    else:
        slack_connector.set_handler(orchestrator.handle)

    # ── 8. Signal handling ────────────────────────────────────────
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
        await slack_connector.start()
        logger.info("NemoClaw is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
    except Exception:
        logger.error("Fatal error during startup", exc_info=True)
        sys.exit(1)
    finally:
        # Teardown in reverse order: connector → dispatcher →
        # delegation manager → bus → audit → backend.  The dispatcher
        # owns the longest-lived asyncio tasks (the listen loop +
        # in-flight finalisations), so cancelling it first lets the
        # bus.close() below complete cleanly.
        logger.info("Shutting down...")
        await slack_connector.stop()
        if dispatcher is not None:
            try:
                await dispatcher.close()
            except Exception:
                logger.warning("Dispatcher close failed", exc_info=True)
        if delegation_manager is not None:
            try:
                await delegation_manager.close()
            except Exception:
                logger.warning("Delegation manager close failed", exc_info=True)
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


async def _placeholder_handler(
    _request: NormalizedRequest,
    _on_status: StatusCallback | None = None,
) -> RichResponse:
    """Connector handler used during startup before the orchestrator exists.

    The :class:`SlackConnector` is constructed early so the
    finalisation renderer can attach to its authenticated client;
    the connector's handler is rewritten via
    :meth:`SlackConnector.set_handler` once the orchestrator is built.
    The placeholder is never actually invoked because the connector
    isn't started until step 9.
    """
    raise RuntimeError("Slack connector started before orchestrator wiring completed")


def run() -> None:
    """Synchronous entry point for use in Makefile / CLI."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
