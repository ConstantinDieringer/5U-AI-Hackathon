"""Deliver a separate booking tracking mail and recover unanswered tracking requests."""

import logging
import threading
import time
from datetime import datetime

from app import db, hackloop
from app.config import settings
from app.mail import multilingual
from app.mail.router import address
from app.mail.style import SIGNATURE
from app.mail.tracking_notice import customer_context, has_notice

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
    from app.agents import booking, tracking
    from app.mail import tracking_delivery

    with booking._lock(sid):
        session = db.get_session(sid)
        if not session or db.is_paused(sid) or db.is_manual(sid):
            return False
        workflow = session.get("workflow")
        if workflow and (
            workflow["phase"] not in ("awaiting_customer", "booked") or db.reply_has_pending(sid)
        ):
            return False
        customer = address(session.get("customer") or "")
        booked = customer_context(session, customer)
        if booked is None:
            return False
        incoming = [
            m
            for m in session["mails"]
            if m["direction"] == "in" and address(m.get("sender") or "") == customer
        ]
        if not incoming:
            return False
        key = f"tracking.notice.recovery.{sid}"
        parent = incoming[-1]
        lang = multilingual.mail_language(booking._last_customer_mail(sid))
        if booked:
            booking_row = next(
                b
                for b in session["bookings"]
                if b["status"] in ("booked", "confirmed")
                and b.get("customer_state") in (None, "confirmed_sent", "price_accepted")
            )
            key += f".booking.{booking_row['id']}"
            record = next((r for r in db.tracking_records() if r["session_id"] == sid), None)
            if record is None:
                return False
            body = tracking.booking_notice_text(
                record, lang, (session.get("request") or {}).get("customer_name")
            )
        else:
            if session["status"] not in ("quoted", "booked", "confirmed"):
                return False
            outgoing = [
                m
                for m in session["mails"]
                if m["direction"] == "out" and address(m.get("recipient") or "") == customer
            ]
            if any(has_notice(m.get("body", "")) for m in outgoing):
                return False
            if (
                booking.classify_reply(multilingual.working_text(parent["id"], parent["body"]))
                == "no"
            ):
                return False
            quotes = session.get("quotes") or []
            if (
                not quotes
                or time.time() - datetime.fromisoformat(quotes[-1]["sent_at"]).timestamp()
                > settings.followup_window_s
            ):
                return False
            if _already_received(session):
                return False
            greeting = "Hallo," if lang.startswith("de") else "Hi,"
            body = f"{greeting}\n\n{SIGNATURE}\n"
        delivered = tracking_delivery.send(
            key=key,
            recipient=customer,
            subject=parent["subject"],
            body=body,
            parent_id=parent["id"],
            session_id=sid,
            category="tracking_notice",
            language_code=lang,
            tracking_booked=booked,
        )
        if delivered:
            db.emit(
                "tracking_notice_recovered",
                "Trackingmail zur Buchung oder fehlender Hinweis versendet",
                sid,
            )
        return delivered


def tick() -> None:
    from app.agents import tracking

    tracking.recover_pending()
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
