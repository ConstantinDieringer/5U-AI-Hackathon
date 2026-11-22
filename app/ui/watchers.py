"""Background jobs for task C: deadline watch and board sync.

Start from run.py:  watchers.start()   (daemon threads, never raise)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
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
            now = datetime.now(UTC)
            for s in db.list_sessions(attention=True):
                deadline = s.get("deadline_at")
                left = (
                    (datetime.fromisoformat(deadline) - now).total_seconds() if deadline else None
                )
                # attention also means paused/error/manual: warn only for a real close deadline
                if (
                    s["status"] in db.OPEN_STATUSES
                    and not s["paused"]
                    and left is not None
                    and 0 < left < 90
                    and s["id"] not in warned
                ):
                    db.emit(
                        db.Ev.WARN,
                        "Weniger als 90 s bis zur Deadline",
                        s["id"],
                        level="warn",
                    )
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


UNDECIDED = {"open", "pending"}  # the round is still running: not a failure yet


def is_failure(verdict: object, reason: object) -> bool:
    """Our early warning for 5U's unannounced setup changes: any decided verdict other than
    accepted, or any reason code at all."""
    if reason:
        return True
    return isinstance(verdict, str) and verdict not in UNDECIDED and verdict != "accepted"


def board_loop() -> None:
    while True:
        try:
            from app import hackloop  # task A

            data = hackloop.get_board_me()
            for r in _board_rows(data):
                round_id = str(
                    r.get("round_no") or r.get("round") or r.get("round_id") or r.get("id") or ""
                )
                if not round_id:
                    continue
                verdict = r.get("verdict")
                reason = r.get("reason") or r.get("reason_code")
                failed = is_failure(verdict, reason)
                sid = _match_session(r)
                sent_at = r.get("sent_at") if isinstance(r.get("sent_at"), str) else None
                # no answer: freeze our own record of why (no session = mail never arrived)
                cause = db.round_cause(sid, sent_at) if verdict == db.NO_ANSWER else None
                changed = db.save_board_result(
                    round_id=round_id,
                    verdict=verdict,
                    reason=reason,  # fields: round_no, sent_at,
                    # answered_at, verdict (open|pending|accepted|rejected|...), reason, booked
                    points=r.get("points") if isinstance(r.get("points"), dict) else None,
                    raw=r,
                    session_id=sid,
                    failed=failed,
                    cause=cause,
                )
                if changed and failed:
                    # warning, not error: the board_verdict event already shows it on the
                    # dashboard and alerts #sos; an ERROR line would repeat it as log_error
                    log.warning(
                        "board: round %s (session %s) %s, reason %s",
                        round_id, sid, verdict, reason,
                    )  # fmt: skip
        except ImportError:
            log.info("board sync waiting for app/hackloop.py")
        except Exception:
            log.exception("board loop")
        time.sleep(BOARD_EVERY_S)


def _match_session(r: dict[str, Any]) -> int | None:
    """Board rows carry `sent_at` = received_at of the customer's RFQ (verified 21.11 16:01),
    which is our session's created_at. Fall back to id-like fields if 5U adds them."""
    sent_at = r.get("sent_at")
    if isinstance(sent_at, str):
        sid = db.session_by_created_at(sent_at)
        if sid is not None:
            return sid
    for key in ("mail_id", "message_id", "request_mail_id", "thread_id"):
        v = r.get(key)
        if isinstance(v, int) and db.get_session(v):
            return v
    return None


def start() -> None:
    for target in (deadline_loop, board_loop):
        threading.Thread(target=target, name=target.__name__, daemon=True).start()
    from app.agents import carrier_prefs
    from app.ui import alerts

    try:
        n = carrier_prefs.backfill()
        if n:
            log.info("carrier preferences restored from stored mails: %d", n)
    except Exception:
        log.exception("carrier preference backfill")
    alerts.start()  # Slack #sos, only with SLACK_SOS_WEBHOOK
