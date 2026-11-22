"""SOS alerts: post to the team's Slack #sos channel when something needs a human.

Runs as a daemon thread next to the watchers (watchers.start). Reads new events from the
database, so every agent's problems reach Slack without the agents knowing about Slack.
Needs SLACK_SOS_WEBHOOK (Slack incoming webhook for #sos); without it the loop does nothing.

What triggers an alert (each at most once per session and kind):
  - an RFQ that cannot be quoted (status error) or expired without a quote
  - a thread rescue plan that leaves required RFQ facts open (a person must quote now)
  - a 5U verdict other than accepted (rejected, no answer, voided), with its reason code
  - a program error logged as an event
  - no incoming mail for MAIL_SILENCE_MIN minutes (RFQs come every 6 min)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app import db

log = logging.getLogger(__name__)

WEBHOOK = os.getenv("SLACK_SOS_WEBHOOK", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://5u-ai-hackathon-production.up.railway.app").rstrip(
    "/"
)
EVERY_S = 20.0
MAIL_SILENCE_MIN = 15
KV_LAST = "sos.last_event_id"
BAD_VERDICTS = ("rejected", "no answer", "voided")


def classify(e: dict[str, Any]) -> tuple[str, str] | None:
    """(kind, headline) for an event that deserves an alert, else None."""
    kind, msg = e.get("kind"), str(e.get("message") or "")
    if kind == "status" and msg.startswith("Status → error"):
        reason = msg.split("(", 1)[1].rstrip(")") if "(" in msg else "unbekannt"
        return "no_quote", f":x: Keine Quote möglich: {reason}"
    if kind == "warn" and "abgelaufen ohne Quote" in msg:
        return "expired", ":alarm_clock: RFQ ohne Antwort abgelaufen"
    if kind == "warn" and msg.startswith("Preisänderung"):
        # asking the customer and a human having to step in are separate alerts
        human = "übernehmen" in msg or "manuell" in msg
        return (
            "price_change_human" if human else "price_change"
        ), f":money_with_wings: {msg[:200]}"
    if kind == "thread_rescue_blocked" and "erforderliche Angaben" in msg:
        return "rescue_blocked", f":sos: Rettung blockiert, bitte von Hand quoten. {msg[:200]}"
    if kind == "mailbox_rate_limit":
        # the message carries its time, so each 10-min window alerts once (see scan dedupe)
        return "mailbox_429", f":rotating_light: {msg[:200]}"
    if kind == "board_verdict" and any(v in msg for v in BAD_VERDICTS):
        return "verdict", f":red_circle: 5U {msg.removeprefix('Board: ')}"
    if kind == "error" or (
        e.get("level") == "error"
        and kind not in ("status", "warn", "board_verdict", "no_price", "no_trucking")
    ):
        return "error", f":boom: Fehler: {msg[:200]}"
    return None


def format_alert(headline: str, session: dict[str, Any] | None, sid: int | None) -> str:
    lines = [headline]
    if session:
        req = session.get("request") or {}
        lane = " → ".join(x for x in (req.get("origin"), req.get("destination")) if x)
        who = (session.get("customer") or "").replace("@hackloop.mail", "")
        lines.append(" · ".join(x for x in (f"#{sid}", lane, who, session.get("subject")) if x))
    if sid is not None:
        lines.append(f"<{PUBLIC_URL}/#session={sid}|In der Konsole öffnen>")
    return "\n".join(lines)


def post(text: str) -> bool:
    if not WEBHOOK:
        return False
    try:
        r = httpx.post(WEBHOOK, json={"text": text}, timeout=5)
        r.raise_for_status()
        return True
    except httpx.HTTPError as exc:  # never let Slack break the forwarder
        log.warning("sos post failed: %s", exc)
        return False


def scan(seen: set[tuple[int | None, str]]) -> list[str]:
    """Alerts for events after the stored cursor; advances the cursor."""
    last = int(db.kv_get(KV_LAST, "0") or 0)
    out = []
    for e in db.events_after(last, limit=500):
        last = max(last, int(e["id"]))
        hit = classify(e)
        if not hit:
            continue
        kind, headline = hit
        sid = e.get("session_id")
        key = (sid, kind) if sid is not None else (None, f"{kind}:{headline[:60]}")
        if key in seen:
            continue
        seen.add(key)
        out.append(format_alert(headline, db.get_session(sid) if sid else None, sid))
    db.kv_set(KV_LAST, str(last))
    return out


def mail_silence_minutes() -> float | None:
    r = db.conn().execute("SELECT MAX(received_at) FROM mails WHERE direction = 'in'").fetchone()
    if not r or not r[0]:
        return None
    t = datetime.fromisoformat(r[0])
    t = t if t.tzinfo else t.replace(tzinfo=UTC)
    return (datetime.now(UTC) - t).total_seconds() / 60


def loop() -> None:
    if not WEBHOOK:
        log.info("SOS alerts off: SLACK_SOS_WEBHOOK not set")
        return
    # start at the newest event: a restart must not replay old alerts into Slack
    if db.kv_get(KV_LAST) is None:
        newest = db.conn().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        db.kv_set(KV_LAST, str(newest))
    seen: set[tuple[int | None, str]] = set()
    silent_alerted = False
    post(":white_check_mark: Forwarder gestartet, SOS-Meldungen aktiv.")
    while True:
        try:
            for text in scan(seen):
                post(text)
            quiet = mail_silence_minutes()
            if quiet is not None and quiet >= MAIL_SILENCE_MIN and not silent_alerted:
                post(
                    f":mailbox_with_no_mail: Seit {int(quiet)} min keine Mail. Poller oder 5U prüfen."
                )
                silent_alerted = True
            elif quiet is not None and quiet < MAIL_SILENCE_MIN:
                silent_alerted = False
        except Exception:
            log.exception("sos loop")
        time.sleep(EVERY_S)


def start() -> None:
    threading.Thread(target=loop, name="sos_alerts", daemon=True).start()
