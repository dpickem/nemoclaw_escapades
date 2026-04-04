"""Entry point for the NemoClaw M1 agent loop.

Wires together config → backend → orchestrator → connector and runs
until interrupted.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from nemoclaw_escapades.backends.inference_hub import InferenceHubBackend
from nemoclaw_escapades.config import load_config
from nemoclaw_escapades.connectors.slack import SlackConnector
from nemoclaw_escapades.observability.logging import get_logger, setup_logging
from nemoclaw_escapades.orchestrator import Orchestrator


async def main() -> None:
    config = load_config()
    setup_logging(level=config.log.level, log_file=config.log.log_file)

    logger = get_logger("main")
    logger.info("Starting NemoClaw M1 agent loop")

    backend = InferenceHubBackend(config.inference)
    orchestrator = Orchestrator(backend, config.orchestrator)
    connector = SlackConnector(
        handler=orchestrator.handle,
        bot_token=config.slack.bot_token,
        app_token=config.slack.app_token,
    )

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

    try:
        await connector.start()
        logger.info("NemoClaw is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
    except Exception:
        logger.error("Fatal error during startup", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Shutting down...")
        await connector.stop()
        await backend.close()
        logger.info("Shutdown complete")


def run() -> None:
    """Synchronous entry point for use in Makefile / CLI."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
