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
from nemoclaw_escapades.audit.db import AuditDB
from nemoclaw_escapades.backends.inference_hub import InferenceHubBackend
from nemoclaw_escapades.config import load_config
from nemoclaw_escapades.connectors.slack import SlackConnector
from nemoclaw_escapades.observability.logging import get_logger, setup_logging
from nemoclaw_escapades.orchestrator.orchestrator import Orchestrator
from nemoclaw_escapades.tools.confluence import register_confluence_tools
from nemoclaw_escapades.tools.gerrit import register_gerrit_tools
from nemoclaw_escapades.tools.gitlab import register_gitlab_tools
from nemoclaw_escapades.tools.jira import register_jira_tools
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.slack_search import register_slack_search_tools


async def main() -> None:
    # ── 1. Configuration ──────────────────────────────────────────
    # Reads env vars / .env into typed dataclasses.  Inside an
    # OpenShell sandbox, credentials are proxy placeholders resolved
    # at HTTP-request time — the config layer never sees real secrets.
    config = load_config()
    setup_logging(level=config.log.level, log_file=config.log.log_file)

    logger = get_logger("main")
    logger.info("Starting NemoClaw agent loop")

    # ── 2. Inference backend ──────────────────────────────────────
    backend = InferenceHubBackend(config.inference)

    # ── 3. Tool registry ──────────────────────────────────────────
    # Populate a concrete ToolRegistry first (mypy needs the concrete
    # type for the register_* calls), then narrow to ToolRegistry |
    # None — the orchestrator treats None as "no tools, single-shot
    # inference only."
    registry = ToolRegistry()
    if config.jira.enabled:
        register_jira_tools(registry, config.jira)
    if config.gitlab.enabled:
        register_gitlab_tools(registry, config.gitlab)
    if config.gerrit.enabled:
        register_gerrit_tools(registry, config.gerrit)
    if config.confluence.enabled:
        register_confluence_tools(registry, config.confluence)
    if config.slack_search.enabled:
        register_slack_search_tools(registry, config.slack_search)

    tools: ToolRegistry | None
    if registry.names:
        tools = registry
        logger.info(
            "Tools registered",
            extra={"count": len(registry), "toolsets": sorted(registry.toolsets)},
        )
    else:
        tools = None

    # ── 4. Audit DB ───────────────────────────────────────────────
    # SQLite database for tool-call logging.  The background writer
    # batches inserts off the hot path so audit never blocks routing.
    audit: AuditDB | None = None
    if config.audit.enabled:
        audit_path = str(Path(config.audit.db_path).expanduser())
        audit = AuditDB(audit_path, persist_payloads=config.audit.persist_payloads)
        await audit.open()
        await audit.start_background_writer()
        logger.info("Audit DB opened", extra={"path": audit_path})

    # ── 5. Orchestrator + connector ───────────────────────────────
    # The orchestrator owns the agent loop; the connector bridges
    # Slack events to orchestrator.handle().
    orchestrator = Orchestrator(
        backend, config.orchestrator, approval=WriteApproval(), tools=tools, audit=audit
    )
    connector = SlackConnector(
        handler=orchestrator.handle,
        bot_token=config.slack.bot_token,
        app_token=config.slack.app_token,
    )

    # ── 6. Signal handling ────────────────────────────────────────
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

    # ── 7. Run until shutdown ─────────────────────────────────────
    try:
        await connector.start()
        logger.info("NemoClaw is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
    except Exception:
        logger.error("Fatal error during startup", exc_info=True)
        sys.exit(1)
    finally:
        # Teardown in reverse order: connector → audit → backend.
        logger.info("Shutting down...")
        await connector.stop()
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
