"""Detect a second reader of our mailbox from the live poller's 429 responses.

The mailbox allows one poll per 5 s per team key. Only the live system may poll. When anyone
else (a review agent, a laptop instance, a script) polls with the team key, our poller gets 429
and mail arrives late. Rounds 47 and 48 were lost that way. A single 429 can be bad luck, so this
raises an `error` event (and with it a #sos alert, see app.ui.alerts) only after THRESHOLD hits
within WINDOW_S, and at most once per WINDOW_S.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

from app import db

WINDOW_S = 600.0
THRESHOLD = 3
EVENT_KIND = "mailbox_rate_limit"

_lock = threading.Lock()
_hits: deque[float] = deque()
_last_alert: float | None = None


def _prune(now: float) -> None:
    while _hits and now - _hits[0] > WINDOW_S:
        _hits.popleft()


def record(now: float | None = None, emit: Callable[..., object] = db.emit) -> bool:
    """Count one 429 of the live poller. True if this call raised the alert event."""
    global _last_alert
    now = time.monotonic() if now is None else now
    with _lock:
        _hits.append(now)
        _prune(now)
        due = len(_hits) >= THRESHOLD and (_last_alert is None or now - _last_alert >= WINDOW_S)
        if not due:
            return False
        _last_alert = now
        count = len(_hits)
    clock = datetime.now(UTC).strftime("%H:%M UTC")
    emit(
        EVENT_KIND,
        f"Mailbox-429 {count}× in {int(WINDOW_S // 60)} min ({clock}): jemand pollt mit dem "
        "Team-Key mit. Review-Agents und lokale Instanzen stoppen.",
        level="error",
        count=count,
    )
    return True


def recent(now: float | None = None) -> int:
    """429s of the live poller within the last WINDOW_S (for /api/health)."""
    now = time.monotonic() if now is None else now
    with _lock:
        _prune(now)
        return len(_hits)


def reset() -> None:
    """Forget all hits (tests)."""
    global _last_alert
    with _lock:
        _hits.clear()
        _last_alert = None
