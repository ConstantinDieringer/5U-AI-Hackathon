"""Repair a missing tracking invitation once, with no model or new quote."""

import logging
import threading
import time
from datetime import datetime

from app import db, hackloop
from app.config import settings
from app.mail.language import detect
from app.mail.outgoing import CustomerStyleError
from app.mail.router import address
from app.mail.style import SIGNATURE
from app.mail.tracking_notice import customer_context, ensure_notice, has_notice

log = logging.getLogger(__name__)


def _already_received(session: dict) -> bool:
    """Old sends sometimes lack a local outgoing row. Inspect their referenced originals."""
    sid = session["id"]
    if db.kv_get(f"tracking.notice.evidence.{sid}"):
        return True
    known = {m["id"] for m in session["mails"]}
    parents = {
        m["in_reply_to"]
        for m in session["mails"]
        if m["direction"] == "in"
        and address(m.get("sender") or "") == address(session["customer"])
        and m.get("in_reply_to")
        and m["in_reply_to"] not in known
    }
    for parent in parents:
        original = hackloop.get_message(parent)
        if (
            address(original.get("from", "")) == settings.team_mailbox
            and address(original.get("to", "")) == address(session["customer"])
            and has_notice(original.get("body", ""))
        ):
            db.kv_set(f"tracking.notice.evidence.{sid}", str(parent))
            return True
    return False


def repair(sid: int) -> bool:
    from app.agents import booking
    from app.mail.guard import OutboundLoopBlocked

    with booking._lock(sid):
        session = db.get_session(sid)
        if not session or db.is_paused(sid) or db.is_manual(sid):
            return False
        customer = address(session.get("customer") or "")
        booked = customer_context(session, customer)
        if booked is None or session["status"] not in ("quoted", "booked", "confirmed"):
            return False
        incoming = [
            m
            for m in session["mails"]
            if m["direction"] == "in" and address(m.get("sender") or "") == customer
        ]
        outgoing = [
            m
            for m in session["mails"]
            if m["direction"] == "out" and address(m.get("recipient") or "") == customer
        ]
        if any(has_notice(m.get("body", "")) for m in outgoing):
            return False
        if not incoming or booking.classify_reply(incoming[-1]["body"]) == "no":
            return False
        if not booked:
            quotes = session.get("quotes") or []
            if (
                not quotes
                or time.time() - datetime.fromisoformat(quotes[-1]["sent_at"]).timestamp()
                > settings.followup_window_s
            ):
                return False
        if _already_received(session):
            return False
        key = f"tracking.notice.recovery.{sid}"
        if not db.claim_tracking_notice(sid):
            if db.kv_get(key) == "reserved" and db.kv_claim(key + ".alert"):
                db.emit(
                    "tracking_notice_unknown",
                    "Trackinghinweis nach Neustart unklar, bitte Versand prüfen",
                    sid,
                    level="error",
                )
            return False
        parent = incoming[-1]
        lang = detect(parent["body"])
        greeting = {"de": "Hallo,", "fr": "Bonjour,", "es": "Hola,", "it": "Buongiorno,"}.get(
            lang, "Hi,"
        )
        body = ensure_notice(f"{greeting}\n\n{SIGNATURE}\n", lang, booked)
        db.kv_set("tracking.notice.recovery.last_attempt", f"{sid}:{time.time_ns()}")
        try:
            sent = hackloop.send_customer(
                customer,
                parent["subject"],
                body,
                in_reply_to=parent["id"],
                session_id=sid,
                review=False,
            )
        except (hackloop.CustomerSendBlocked, OutboundLoopBlocked, CustomerStyleError):
            db.release_tracking_notice(sid)
            return False
        except Exception as exc:  # noqa: BLE001 - ambiguous delivery must never be retried blindly
            if isinstance(exc, hackloop.HackloopError) and exc.status == 429:
                db.release_tracking_notice(sid)
                db.emit(
                    "tracking_notice_deferred",
                    "Trackinghinweis wegen Ratenlimit zurückgestellt",
                    sid,
                    level="warn",
                )
                return False
            db.kv_set(key, "unknown")
            db.emit(
                "tracking_notice_unknown",
                "Versand des Trackinghinweises unklar, keine doppelte Nachsendung",
                sid,
                level="error",
                reason=type(exc).__name__,
            )
            return False
        # Record the receipt first so a local recording failure cannot send the mail twice.
        db.kv_set(key, f"sent:{sent['id']}")
        db.record_outgoing(
            mail_id=int(sent["id"]),
            recipient=customer,
            subject=sent["subject"],
            body=sent["body"],
            category="tracking_notice",
            session_id=sid,
            in_reply_to=parent["id"],
            raw=sent,
        )
        db.emit("tracking_notice_recovered", "Fehlenden Trackinghinweis einmalig nachgesendet", sid)
        return True


def tick() -> None:
    for sid in db.tracking_notice_candidates():
        before = db.kv_get("tracking.notice.recovery.last_attempt")
        try:
            if repair(sid):
                break  # low priority, at most one repair per interval
        except Exception:
            log.exception("session %s: tracking notice check failed", sid)
            if db.kv_claim(f"tracking.notice.check_error.{sid}"):
                db.emit(
                    "tracking_notice_check_failed",
                    "Trackinghinweis konnte nicht sicher geprüft werden",
                    sid,
                    level="error",
                )
        if before != db.kv_get("tracking.notice.recovery.last_attempt"):
            break  # rejected and ambiguous attempts also consume the interval's slot


def run(stop: threading.Event) -> None:
    while not stop.wait(settings.tracking_notice_check_every_s):
        try:
            tick()
        except Exception:
            log.exception("tracking notice recovery loop failed; retrying next interval")
