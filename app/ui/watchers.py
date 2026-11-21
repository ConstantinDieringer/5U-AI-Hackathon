"""Background jobs for task C: deadline watch and board sync.

Start from run.py:  watchers.start()   (daemon threads, never raise)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app import db
from app.config import settings

log = logging.getLogger(__name__)
DEADLINE_EVERY_S = settings.deadline_check_every_s
BOARD_EVERY_S = settings.board_sync_every_s


def deadline_loop() -> None:
    warned: set[int] = set()
    while True:
        try:
            for sid in db.expire_overdue():
                log.warning("session %s expired without quote", sid)
            for s in db.list_sessions(attention=True):
                if s["status"] in db.OPEN_STATUSES and not s["paused"] and s["id"] not in warned:
                    db.emit(db.Ev.WARN, "Weniger als 90 s bis zur Deadline", s["id"], level="warn")
                    warned.add(s["id"])
        except Exception:
            log.exception("deadline loop")
        time.sleep(DEADLINE_EVERY_S)


def _board_rows(data: Any) -> list[dict[str, Any]]:
    """Extract per-request rows from /v1/board/me. Field names to be verified on real data."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("results", "requests", "rounds", "items"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    return []


def board_loop() -> None:
    while True:
        try:
            from app import hackloop  # task A

            data = hackloop.get_board_me()
            for r in _board_rows(data):
                round_id = str(r.get("round") or r.get("round_id") or r.get("id") or "")
                if not round_id:
                    continue
                db.save_board_result(
                    round_id=round_id,
                    verdict=r.get("verdict"),
                    reason=r.get("reason") or r.get("reason_code"),
                    points=r.get("points") if isinstance(r.get("points"), dict) else None,
                    raw=r,
                    session_id=_match_session(r),
                )
        except ImportError:
            log.info("board sync waiting for app/hackloop.py")
        except Exception:
            log.exception("board loop")
        time.sleep(BOARD_EVERY_S)


def _match_session(r: dict[str, Any]) -> int | None:
    """Best effort: the board may carry the customer mail id. Verify on real data."""
    for key in ("mail_id", "message_id", "request_mail_id", "thread_id"):
        v = r.get(key)
        if isinstance(v, int) and db.get_session(v):
            return v
    return None


def start() -> None:
    for target in (deadline_loop, board_loop):
        threading.Thread(target=target, name=target.__name__, daemon=True).start()
