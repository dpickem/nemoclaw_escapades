"""Lightweight elapsed-time context manager for latency measurement.

Usage::

    with Timer() as t:
        await do_work()
        logger.info("done", extra={"latency_ms": t.ms})

The ``ms`` property can be read at any point — including inside
exception handlers — and always reflects the time since entry.
"""

from __future__ import annotations

import time


class Timer:
    """Monotonic-clock timer usable as a context manager.

    Attributes:
        ms: Elapsed milliseconds since the context was entered (or
            since ``__init__`` if used without ``with``).
    """

    def __init__(self) -> None:
        self._start = time.monotonic()

    def __enter__(self) -> Timer:
        self._start = time.monotonic()
        return self

    def __exit__(self, *args: object) -> None:
        pass

    @property
    def ms(self) -> float:
        """Milliseconds elapsed since the timer started."""
        return (time.monotonic() - self._start) * 1000
