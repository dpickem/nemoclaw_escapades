"""Observability — structured logging, error categorization, and timing."""

from nemoclaw_escapades.observability.logging import setup_logging
from nemoclaw_escapades.observability.timer import Timer

__all__ = ["Timer", "setup_logging"]
