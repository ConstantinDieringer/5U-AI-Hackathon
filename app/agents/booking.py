"""Booking agent: customer accepts our quote -> book by rate_id -> confirm in the thread.

No LLM: acceptance is detected by rules (most acceptances are one short sentence). An unclear
reply is left to a human (event + warn), never booked on a guess.

Flow for a customer reply in a quoted session:
1. acceptance? (rules, quoted text stripped)
2. portal quote: POST /v1/portal/bookings with rate_id + team_reference (AP-<session>).
   410/404 (expired/unknown) -> fetch a fresh rate for the same airline and book that.
   503 -> book by email with the airline (the message names the address).
   Sheet/bid quote (no rate_id) -> book by email with the airline.
3. record_booking, then confirm to the customer in the thread (in_reply_to).
Airline mails "Booking <ref> confirmed" set the booking to confirmed (on_airline_mail).
"""

from __future__ import annotations

import logging
import re
import threading
from decimal import Decimal
from typing import Any

from app import db, hackloop
from app.config import settings
from app.mail.language import detect
from app.mail.router import address
from app.mail.style import SIGNATURE, amount, has_template_artifacts
from app.models import Mail
from app.pricing.pricing import to_minor
from app.pricing.reference import airline_email

log = logging.getLogger(__name__)

_YES = re.compile(
    r"\b(book|book it|go ahead|proceed|accept|accepted|works for us|"
    r"sounds good|let'?s do it|approved?|"
    r"buchen|einverstanden|passt|nehmen wir|angenommen|"
    r"réserve\w*|reserv\w*|prenot\w*|procedi\w*|akzeptier\w*|d'accord|adelante)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"\b(not|don'?t|do not|no thanks|decline|too expensive|cancel|nicht|kein\w*|leider|"
    r"zu teuer|absagen|stornier\w*|pas|non)\b",
    re.IGNORECASE,
)
_DECLINE = re.compile(  # wins over any "yes" word ("we will not proceed")
    r"too expensive|decline|not (?:go ahead|proceed|book)|no thanks|zu teuer|absagen|"
    r"nicht buchen",
    re.IGNORECASE,
)
_CONFIRMED = re.compile(r"\bBooking\s+(BK-[A-Za-z0-9]+)\s+confirmed\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(session_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


# ---------------------------------------------------------------- intent


def own_text(body: str) -> str:
    """The customer's own words: quoted lines and everything after a quote header dropped."""
    from app.mail.content import current_message

    return current_message(body).text


def is_acceptance(body: str) -> bool | None:
    """True = accepts, False = declines, None = unclear (left to a human)."""
    text = own_text(body)
    if _DECLINE.search(text):
        return False
    yes, no = bool(_YES.search(text)), bool(_NO.search(text))
    if yes and not no:
        return True
    if no and not yes:
        return False
    return None


def language(body: str) -> str:
    return "de" if detect(own_text(body)) == "de" else "en"


# ---------------------------------------------------------------- booking


def _airline_code(origin: str, destination: str, airline: str) -> str | None:
    from app.pricing.portal_sync import portal_airlines

    for a in portal_airlines(origin, destination):
        if airline in (a.get("name"), a.get("code")):
            return a["code"]
    return None


def _fresh_rate_id(quote: dict[str, Any], request: dict[str, Any]) -> str | None:
    code = _airline_code(quote["origin"], quote["destination"], quote["airline"])
    if code is None:
        return None
    from app.pricing.portal_sync import next_break
    from app.pricing.pricing import chargeable_kg

    weight = float(request["weight_kg"])
    volume = float(request.get("volume_cbm") or 0.0)
    best: dict[str, Any] | None = None
    # same weights as the quote: exact and the next break (whichever was cheaper is quoted)
    for w in (weight, next_break(code, chargeable_kg(weight, volume))):
        if w is None:
            continue
        raw = hackloop.get_rate(quote["origin"], quote["destination"], code, w, volume)
        if best is None or Decimal(str(raw["total"])) < Decimal(str(best["total"])):
            best = raw
    return str(best["rate_id"]) if best else None


def _book_portal(sid: int, quote: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Book the quoted rate; on expired/unknown fetch a fresh one once. Raises HackloopError."""
    ref = db.team_reference(sid)
    try:
        return hackloop.book(quote["rate_id"], ref)
    except hackloop.HackloopError as e:
        if e.status not in (404, 410):
            raise
        db.emit(db.Ev.WARN, f"Rate {quote['rate_id']} {e.status}: hole neue Rate", sid)
        fresh = _fresh_rate_id(quote, request)
        if fresh is None:
            raise
        return hackloop.book(fresh, ref)


def _book_by_email(sid: int, quote: dict[str, Any], request: dict[str, Any], to: str) -> None:
    ref = db.team_reference(sid)
    subject = f"[{ref}] Booking request {quote['origin']} to {quote['destination']}"
    body = (
        "Hello,\n\n"
        f"we would like to book air freight {quote['origin']} to {quote['destination']} "
        f"for {request['weight_kg']:g} kg / {float(request.get('volume_cbm') or 0):g} cbm "
        f"at your offer of {amount(quote['total_minor'], quote['currency'])}.\n"
        f"Our reference: {ref}.\n\nPlease confirm the booking.\n\n"
        f"{SIGNATURE}\n"
    )
    sent = hackloop.send(to, subject, body)
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=to,
        subject=subject,
        body=body,
        category="booking_request",
        session_id=sid,
        sender=settings.team_mailbox,
        raw=sent,
    )


def _confirmation(lang: str, name: str | None, quote: dict[str, Any], booking_ref: str) -> str:
    if name and has_template_artifacts(name):
        name = None
    price = amount(quote["total_minor"], quote["currency"])
    connector = "nach" if lang == "de" else "to"
    route = f"{quote['origin']} {connector} {quote['destination']}"
    if lang == "de":
        return (
            f"{'Hallo ' + name if name else 'Guten Tag'},\n\n"
            f"vielen Dank, Ihre Sendung ist gebucht.\n\n"
            f"Buchungsnummer: {booking_ref}\n"
            f"Strecke: {route} mit {quote['airline']}\n"
            f"Preis: {price} all-in\n\n"
            "Wir melden uns, sobald es Neuigkeiten gibt. Bei Fragen zum Status "
            "antworten Sie einfach auf diese Mail.\n\n"
            f"{SIGNATURE}\n"
        )
    return (
        f"{'Hi ' + name if name else 'Hello'},\n\n"
        "thanks, your shipment is booked.\n\n"
        f"Booking reference: {booking_ref}\n"
        f"Route: {route} with {quote['airline']}\n"
        f"Price: {price} all-in\n\n"
        "We'll keep you posted. If you have any questions about the status, just reply "
        "to this mail.\n\n"
        f"{SIGNATURE}\n"
    )


def _confirm_to_customer(sid: int, mail: Mail, text: str) -> None:
    if db.is_paused(sid) or db.is_manual(sid):
        db.emit(db.Ev.WARN, "Bestätigung nicht gesendet: Session pausiert/manuell", sid)
        return
    to = address(mail.sender)
    subject = mail.subject if mail.subject.lower().startswith("re:") else f"Re: {mail.subject}"
    sent = hackloop.send(to, subject, text, in_reply_to=mail.id)
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=to,
        subject=subject,
        body=text,
        category="booking_confirmation",
        session_id=sid,
        sender=settings.team_mailbox,
        in_reply_to=mail.id,
        raw=sent,
    )


def handle_reply(mail: Mail, session_id: int | None) -> None:
    """Customer reply in a thread. Never raises (runs in a worker thread)."""
    if session_id is None:
        return
    try:
        with _lock(session_id):
            _handle(mail, session_id)
    except Exception as e:
        log.exception("session %s: booking failed", session_id)
        db.emit(
            db.Ev.ERROR,
            f"Buchung fehlgeschlagen: {type(e).__name__}: {e}"[:300],
            session_id,
            level="error",
        )


def _handle(mail: Mail, sid: int) -> None:
    s = db.get_session(sid)
    if not s or not s.get("quotes"):
        return
    if any(b["status"] in ("booked", "confirmed") for b in s.get("bookings", [])):
        from app.agents import tracking

        if tracking.is_status_question(mail.body):  # "where is it?" in their own thread
            tracking.handle(mail, sid, booked_session=sid)
        else:
            log.info("session %s already booked, reply %s ignored", sid, mail.id)
        return
    verdict = is_acceptance(mail.body)
    if verdict is not True:
        what = "Absage" if verdict is False else "unklare Antwort"
        db.emit(
            db.Ev.WARN,
            f"Kundenantwort ({what}): nicht gebucht, bitte prüfen",
            sid,
            level="warn",
        )
        return
    quote = s["quotes"][0]  # only our first quote in a thread counts
    request = s.get("request") or {}
    db.emit(db.Ev.STATUS, "Kunde hat angenommen: buche", sid)

    if quote.get("source") == "portal" and quote.get("rate_id"):
        try:
            res = _book_portal(sid, quote, request)
        except hackloop.HackloopError as e:
            if e.status != 503:
                db.record_booking(
                    sid,
                    booking_reference=None,
                    rate_id=quote["rate_id"],
                    airline=quote["airline"],
                    price_minor=quote["total_minor"],
                    currency=quote["currency"],
                    status="failed",
                )
                raise
            m = _EMAIL.search(e.detail or "")
            to = m.group(0) if m else airline_email(quote["airline"])
            if not to:
                raise
            _book_by_email(sid, quote, request, to)
            res = {}
        booking_ref = str(res.get("booking_reference") or db.team_reference(sid))
        price = res.get("price")
        price_minor = to_minor(Decimal(str(price))) if price else quote["total_minor"]
        db.record_booking(
            sid,
            booking_reference=res.get("booking_reference"),
            rate_id=res.get("rate_id") or quote["rate_id"],
            airline=quote["airline"],
            price_minor=price_minor,
            currency=res.get("currency") or quote["currency"],
        )
    else:
        to = airline_email(quote["airline"])
        if not to:
            raise ValueError(f"no email for airline {quote['airline']!r}")
        _book_by_email(sid, quote, request, to)
        booking_ref = db.team_reference(sid)
        db.record_booking(
            sid,
            booking_reference=None,
            rate_id=None,
            airline=quote["airline"],
            price_minor=quote["total_minor"],
            currency=quote["currency"],
        )

    text = _confirmation(language(mail.body), request.get("customer_name"), quote, booking_ref)
    _confirm_to_customer(sid, mail, text)
    log.info("session %s booked %s", sid, booking_ref)


# ---------------------------------------------------------------- airline confirmations


def on_airline_mail(mail: Mail) -> bool:
    """'Booking BK-… confirmed' -> booking confirmed. Returns True if it was such a mail."""
    m = _CONFIRMED.search(f"{mail.subject}\n{mail.body}")
    if not m:
        return False
    sid = db.update_booking_status(m.group(1), "confirmed")
    if sid is None:  # booked by email: no BK ref stored yet; match our AP- tag
        tag = re.search(r"\bAP-(\d+)\b", f"{mail.subject}\n{mail.body}")
        if tag:
            sid = db.update_booking_status(f"AP-{tag.group(1)}", "confirmed")
    log.info("airline confirmation %s -> session %s", m.group(1), sid)
    return True


# ---------------------------------------------------------------- catch-up after restart


def catch_up() -> None:
    """Acceptances that arrived while no booking agent ran: book them now (rates are re-fetched
    if expired). Only quoted, unbooked sessions with a customer reply after our quote."""
    for s in db.list_sessions(status="quoted"):
        full = db.get_session(int(s["id"]))
        if not full or not full.get("quotes") or full.get("bookings"):
            continue
        quote_mail = full["quotes"][0].get("mail_id") or 0
        replies = [
            m
            for m in full["mails"]
            if m["direction"] == "in"
            and m["id"] > quote_mail
            and address(str(m["sender"] or "")) == address(str(full.get("customer") or ""))
        ]
        if not replies:
            continue
        r = replies[-1]
        mail = Mail(
            id=int(r["id"]),
            thread_root=int(s["id"]),
            sender=str(r["sender"]),
            to=settings.team_mailbox,
            subject=str(r["subject"] or ""),
            body=str(r["body"] or ""),
            received_at=str(r["received_at"]),
        )
        threading.Thread(
            target=handle_reply,
            args=(mail, int(s["id"])),
            name=f"book-{s['id']}",
            daemon=True,
        ).start()
