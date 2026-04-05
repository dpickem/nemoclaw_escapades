"""Structured JSON logging for the NemoClaw agent loop.

Every log line includes: timestamp, level, component, request_id, message.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "request_id": getattr(record, "request_id", None),
            "message": record.getMessage(),
        }

        extra_keys = {
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "attempt",
            "wait_s",
            "error_category",
            "status_code",
            "model",
            "thread_ts",
            "user_id",
            "channel_id",
            "finish_reason",
            "history_length",
            "continuation_attempt",
            "reason",
            "channel",
            "ts",
            "content_length",
            "action",
        }
        for key in extra_keys:
            value = getattr(record, key, None)
            if value is not None:
                log_entry[key] = value

        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure the root logger with JSON formatting.

    Args:
        level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Optional path to a log file. Logs always go to stdout as well.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    root.handlers.clear()

    formatter = JSONFormatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def get_logger(component: str) -> logging.LoggerAdapter[logging.Logger]:
    """Return a logger adapter pre-bound with a component name."""
    logger = logging.getLogger(f"nemoclaw.{component}")
    return logging.LoggerAdapter(logger, {"component": component})
