"""ERROR log lines as timeline events, so the dashboard shows them.

Many failures only reach the container log ("tracking mail 19688 failed" with a traceback,
"mail guard refused a send"). Nobody watches Railway logs at night; the Übersicht does.
The session comes from the worker thread name (`rfq-19203`, `book-6882`, `track-19184`,
`thread-rescue-6882`, `price-19531`). The same failure is stored at most once per
`DEDUPE_S` seconds, so a loop cannot flood the events table.
"""

from __future__ import annotations

import logging
import re
import threading
import time

DEDUPE_S = 30.0
KIND = "log_error"
_SESSION = re.compile(r"^(?:rfq|rfq-fallback|rfq-watchdog|book|late|track|thread-rescue|price)-(\d+)$")
_NUMBER = re.compile(r"\d+")


class ErrorEventHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._local = threading.local()
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False) or record.name.startswith("app.db"):
            return  # never recurse through a failing db.emit
        self._local.busy = True
        try:
            text = f"{record.name}: {record.getMessage()}"
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                text += f" ({type(exc).__name__}: {exc})"
            text = text[:400]
            key = f"{record.name}|{_NUMBER.sub('#', record.getMessage())}"
            now = time.monotonic()
            with self._lock:
                if now - self._seen.get(key, -DEDUPE_S) < DEDUPE_S:
                    return
                self._seen[key] = now
                if len(self._seen) > 500:
                    self._seen = {k: v for k, v in self._seen.items() if now - v < DEDUPE_S}
            match = _SESSION.match(record.threadName or "")
            from app import db

            db.emit(
                KIND,
                text,
                int(match[1]) if match else None,
                level="error",
                logger=record.name,
                thread=record.threadName,
            )
        except Exception:  # noqa: BLE001, S110 - logging must never break the caller
            pass
        finally:
            self._local.busy = False


def install() -> ErrorEventHandler:
    """Attach once to the root logger (idempotent)."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, ErrorEventHandler):
            return h
    handler = ErrorEventHandler()
    root.addHandler(handler)
    return handler
