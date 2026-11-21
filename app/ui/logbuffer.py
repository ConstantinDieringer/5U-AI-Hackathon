"""Recent process log lines in memory, for the manual context export.

The process logs only to stdout (Railway), so a human who needs the log lines of one
session during an incident would have to search the deploy log by hand. `install()` keeps
the last `CAPACITY` formatted records; `matching()` returns those that mention any needle
(mail IDs, session ID). Lost on restart by design: the events table is the durable log.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque

CAPACITY = 5000
FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"


class _Ring(logging.Handler):
    def __init__(self, capacity: int) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: deque[tuple[int, str]] = deque(maxlen=capacity)
        self.guard = threading.Lock()
        self.setFormatter(logging.Formatter(FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - a broken record must never break logging
            return
        with self.guard:
            self.lines.append((record.levelno, line))

    def snapshot(self) -> list[tuple[int, str]]:
        with self.guard:
            return list(self.lines)


_ring: _Ring | None = None


def install(capacity: int = CAPACITY) -> None:
    """Attach the ring to the root logger once (idempotent)."""
    global _ring
    if _ring is None:
        _ring = _Ring(capacity)
        logging.getLogger().addHandler(_ring)


def matching(needles: list[str], limit: int = 300) -> list[str]:
    """Latest lines that contain one of `needles` as a whole token (so "mail 12" does not
    match "mail 123"), oldest first."""
    if _ring is None or not needles:
        return []

    def token(n: str) -> str:
        before = r"(?<![\w-])" if n[:1].isalnum() else ""
        after = r"(?!\w)" if n[-1:].isalnum() else ""
        return before + re.escape(n) + after

    pattern = re.compile("|".join(token(n) for n in needles), re.IGNORECASE)
    hits = [line for _, line in _ring.snapshot() if pattern.search(line)]
    return hits[-limit:]


def problems(limit: int = 60) -> list[str]:
    """Latest WARNING+ lines of the whole process, for context the needles may miss."""
    if _ring is None:
        return []
    return [line for level, line in _ring.snapshot() if level >= logging.WARNING][-limit:]
