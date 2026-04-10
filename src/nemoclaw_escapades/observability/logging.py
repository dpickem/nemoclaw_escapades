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

    # Attributes that belong to the LogRecord itself (not user-supplied extra).
    _BUILTIN_ATTRS: frozenset[str] = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    )

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "request_id": getattr(record, "request_id", None),
            "message": record.getMessage(),
        }

        # Include every extra field the caller passed, without needing
        # an explicit allowlist.  This ensures new fields (tool, toolset,
        # duration_ms, etc.) appear automatically.
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS and key not in log_entry:
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


class _MergingAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """LoggerAdapter that merges caller ``extra`` with the adapter's own.

    The stdlib ``LoggerAdapter.process()`` *replaces* the caller's
    extra with ``self.extra``, silently discarding per-call fields
    like ``tool`` or ``toolset``.  This subclass merges both dicts.
    """

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        extra: dict[str, Any] = dict(self.extra) if self.extra else {}
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(component: str) -> _MergingAdapter:
    """Return a logger adapter pre-bound with a component name."""
    logger = logging.getLogger(f"nemoclaw.{component}")
    return _MergingAdapter(logger, {"component": component})
