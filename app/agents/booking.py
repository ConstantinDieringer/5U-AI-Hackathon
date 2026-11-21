"""Booking agent: customer accepts our quote -> book by rate_id -> confirm in the thread.

No LLM: acceptance is detected by rules (most acceptances are one short sentence). An unclear
reply is left to a human (event + warn), never booked on a guess.

Flow for a customer reply in a quoted session:
1. acceptance? (rules, quoted text stripped)
2. portal quote: POST /v1/portal/bookings with rate_id + team_reference (AP-<session>).
   410/404 (expired/unknown) -> fetch a fresh rate for the same airline and book that.
   503 -> book by email with the airline (the message names the address).
   Sheet/bid quote (no rate_id) -> book by email with the airline.
3. record_booking. The customer is confirmed only once the airline's price is known: from
   the portal booking response, else from the airline's confirmation mail, else after
   AIRLINE_CONFIRM_WAIT_S. A price off the quoted air price by more than
   PRICE_CHANGE_TOLERANCE_PCT is put to the customer instead (new total, one cheaper
   alternative if we have one). Their yes confirms at the new total; anything else goes to
   a human. The portal has no cancel call, so nothing is ever rebooked automatically.
Airline mails "Booking <ref> confirmed" set the booking to confirmed (on_airline_mail).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app import config, db, hackloop
from app.config import settings
from app.mail.language import detect
from app.mail.router import address
from app.mail.style import SIGNATURE, amount, booked_joke, has_template_artifacts
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


# REVISION_NOT_ACCEPTANCE: "sounds good, but quote the fastest instead", "mit 800 kg statt
# 640 kg" asks for a different offer; booking the old one would be wrong, and a second quote
# in the thread never counts
_REVISION = re.compile(
    r"\binstead\b|\bstatt\b|\banstelle\b|\brevis\w*|\bupdated?\s+(?:weight|quote|offer|figures)"
    r"|\b(?:also|additionally)\s+(?:quote|send|price)|\b(?:another|second|new)\s+(?:quote|offer)"
    r"|\bneues?\s+angebot|\bnoch\s+ein\w*\s+angebot|\bgeändert",
    re.IGNORECASE,
)

_PREBOOK_QUESTION = re.compile(
    r"\bbefore\s+(?:(?:we|i|you)\s+(?:book|proceed|accept|approve)\b|"
    r"booking\b|proceeding\b)"
    r"|\b(?:can|could|may|should)\s+(?:we|i)\s+(?:book|proceed|accept)\b"
    r"|\b(?:book|proceed|accept)\b[^.!?\n]{0,100}\b(?:if|once)\b"
    r"|\b(?:if|once)\b[^.!?\n]{0,100}\b(?:book|proceed|accept)\b"
    r"|\b(?:bevor|ehe)\b[^.!?\n]{0,80}\b(?:buchen|bestätigen|freigeben)\b"
    r"|\b(?:buchen|bestätigen|freigeben)\b[^.!?\n]{0,80}\b(?:wenn|falls)\b",
    re.IGNORECASE,
)


def is_acceptance(body: str) -> bool | None:
    """True = accepts, False = declines, None = unclear (left to a human)."""
    text = own_text(body)
    if _DECLINE.search(text):
        return False
    if _PREBOOK_QUESTION.search(text):
        return None  # mentioning booking in a question/condition is not consent
    if config.settings.revision_not_acceptance and _REVISION.search(text):
        return None
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
    # The customer total includes trucking and may include a commercial adjustment.
    # Legacy quotes lack a frozen air component: do not assert their total as an air rate.
    air_price = quote.get("air_total_minor")
    air_currency = quote.get("air_currency")
    offer = (
        f" at your offer of {amount(air_price, air_currency)}"
        if air_price is not None and air_currency
        else " as quoted by you"
    )
    body = (
        "Hello,\n\n"
        f"we would like to book air freight {quote['origin']} to {quote['destination']} "
        f"for {request['weight_kg']:g} kg / {float(request.get('volume_cbm') or 0):g} cbm"
        f"{offer}.\n"
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


def _confirmation(
    lang: str,
    name: str | None,
    quote: dict[str, Any],
    booking_ref: str,
    total_minor: int | None = None,
) -> str:
    if name and has_template_artifacts(name):
        name = None
    price = amount(quote["total_minor"] if total_minor is None else total_minor, quote["currency"])
    connector = "nach" if lang == "de" else "to"
    route = f"{quote['origin']} {connector} {quote['destination']}"
    if lang == "de":
        return (
            f"{'Hallo ' + name if name else 'Guten Tag'},\n\n"
            f"vielen Dank, Ihre Sendung ist gebucht.\n\n"
            f"Ihre Buchungsnummer lautet {booking_ref}. Die Sendung fliegt mit "
            f"{quote['airline']} von {route}.\n\n"
            f"Der Gesamtpreis beträgt {price}, inklusive aller vereinbarten Leistungen.\n\n"
            f"{booked_joke('de', booking_ref)}\n\n"
            "Wir melden uns, sobald es Neuigkeiten gibt. Bei Fragen zum Status "
            "antworten Sie einfach auf diese Mail.\n\n"
            f"{SIGNATURE}\n"
        )
    return (
        f"{'Hi ' + name if name else 'Hello'},\n\n"
        "thanks, your shipment is booked.\n\n"
        f"Your booking reference is {booking_ref}. Your shipment will fly with "
        f"{quote['airline']} from {route}.\n\n"
        f"The total is {price}, including all agreed services.\n\n"
        f"{booked_joke('en', booking_ref)}\n\n"
        "We'll keep you posted. If you have any questions about the status, just reply "
        "to this mail.\n\n"
        f"{SIGNATURE}\n"
    )


def _confirm_to_customer(
    sid: int,
    mail: Mail,
    text: str,
    protected_terms: tuple[str, ...] = (),
    category: str = "booking_confirmation",
) -> bool:
    if db.is_paused(sid) or db.is_manual(sid):
        db.emit(db.Ev.WARN, "Bestätigung nicht gesendet: Session pausiert/manuell", sid)
        return False
    to = address(mail.sender)
    subject = mail.subject if mail.subject.lower().startswith("re:") else f"Re: {mail.subject}"
    sent = hackloop.send_customer(
        to, subject, text, in_reply_to=mail.id, session_id=sid, protected_terms=protected_terms
    )
    text = sent["body"]
    subject = sent["subject"]
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=to,
        subject=subject,
        body=text,
        category=category,
        session_id=sid,
        sender=settings.team_mailbox,
        in_reply_to=mail.id,
        raw=sent,
    )
    return True


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

        b = s["bookings"][0]
        if b.get("customer_state") == PRICE_ASKED:  # open price question: answer it first
            _on_price_change_answer(mail, sid, s, b)
        elif tracking.is_status_question(mail.body):  # "where is it?" in their own thread
            tracking.handle(mail, sid, booked_session=sid)
        else:
            log.info("session %s already booked, reply %s ignored", sid, mail.id)
        return
    verdict = is_acceptance(mail.body)
    if verdict is not True:
        if verdict is None:
            from app.agents import clarification

            clarification.ask(
                mail,
                sid,
                "booking",
                "Could you confirm whether you would like us to book the offer in this thread, or tell us which detail needs clarifying first? We have not booked based on your last message.",
                "Könnten Sie bestätigen, ob wir das Angebot in diesem Thread buchen sollen, oder uns sagen, welche Angabe Sie vorher klären möchten? Aufgrund Ihrer letzten Nachricht haben wir noch nicht gebucht.",
            )
        what = "Absage" if verdict is False else "unklare Antwort"
        db.emit(
            db.Ev.WARN,
            f"Kundenantwort ({what}): nicht gebucht, bitte prüfen",
            sid,
            level="warn",
        )
        return
    quote = s["quotes"][0]  # only our first quote in a thread counts
    from app.agents import carrier_prefs

    customer = address(mail.sender)
    carrier_prefs.remember(customer, own_text(mail.body), sid)
    if quote["airline"].lower() in carrier_prefs.excluded(customer):
        raise ValueError("customer exclusion blocks booking the quoted carrier")
    request = s.get("request") or {}
    db.emit(db.Ev.STATUS, "Kunde hat angenommen: buche", sid)
    portal_price: tuple[int, str] | None = None

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
        if price:  # the portal's own booking price is the first word on a change
            portal_price = (price_minor, str(res.get("currency") or quote["currency"]).upper())
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

    log.info("session %s booked %s", sid, booking_ref)
    if portal_price is not None and _differs(quote, portal_price):
        _ask_price_change(sid, mail, portal_price)
        return
    if settings.airline_confirm_wait_s > 0:
        db.set_booking_customer_state(sid, AWAITING_AIRLINE)
        db.emit(db.Ev.STATUS, "Gebucht: warte auf den Preis in der Airline-Bestätigung", sid)
        _start_waiter(sid, mail, time.monotonic() + settings.airline_confirm_wait_s)
        return
    _settle(sid, mail)


# ---------------------------------------------------------------- price changes

# bookings.customer_state
AWAITING_AIRLINE = "awaiting_airline"  # booked, customer not yet confirmed, airline price open
CONFIRMED_SENT = "confirmed_sent"  # customer confirmed at a price we believed
PRICE_ASKED = "price_change_asked"  # customer asked whether the new total is ok
PRICE_ACCEPTED = "price_accepted"  # customer said yes to the new total, confirmed
HUMAN = "needs_human"  # declined, wants the alternative, unclear or not computable

_WANTS_OTHER = re.compile(
    r"alternativ\w*|instead|stattdessen|other (?:airline|carrier|option)|"
    r"andere\w* (?:airline|fluggesellschaft|option)|switch|wechseln",
    re.IGNORECASE,
)


def _has_trucking(quote: dict[str, Any]) -> bool:
    return any("trucker" in a for a in quote.get("alternatives") or [] if isinstance(a, dict))


def _terms(quote: dict[str, Any], *extra: str) -> tuple[str, ...]:
    """Words the final style reviewer must keep exactly."""
    return (quote["airline"], quote["origin"], quote["destination"], *extra)


def _quoted_air(quote: dict[str, Any]) -> tuple[int, str] | None:
    """The air price we quoted. Legacy quotes without a frozen air amount: the total, unless
    trucking is part of it (then unknown, and a change cannot be measured)."""
    if quote.get("air_total_minor") is not None and quote.get("air_currency"):
        return int(quote["air_total_minor"]), str(quote["air_currency"]).upper()
    if _has_trucking(quote):
        return None
    return int(quote["total_minor"]), str(quote["currency"]).upper()


def _differs(quote: dict[str, Any], actual: tuple[int, str]) -> bool:
    quoted = _quoted_air(quote)
    if quoted is None:
        return False
    if quoted[1] != actual[1]:
        return True
    tolerance = Decimal(str(settings.price_change_tolerance_pct)) / 100 * quoted[0]
    return abs(actual[0] - quoted[0]) > tolerance


def _airline_price(sid: int) -> tuple[int, str] | None:
    b = (db.get_session(sid) or {}).get("bookings") or []
    if not b or b[0].get("airline_price_minor") is None or not b[0].get("airline_currency"):
        return None
    return int(b[0]["airline_price_minor"]), str(b[0]["airline_currency"])


def _customer_state(sid: int) -> str | None:
    b = (db.get_session(sid) or {}).get("bookings") or []
    return b[0].get("customer_state") if b else None


def _settle(sid: int, mail: Mail) -> None:
    """Confirm the booking to the customer, or ask first if the airline price changed.
    Caller holds the session lock."""
    s = db.get_session(sid)
    if not s or not s.get("quotes") or not s.get("bookings"):
        return
    quote, b = s["quotes"][0], s["bookings"][0]
    actual = _airline_price(sid)
    if actual is not None and _differs(quote, actual):
        _ask_price_change(sid, mail, actual)
        return
    booking_ref = str(b.get("booking_reference") or b["team_reference"])
    name = (s.get("request") or {}).get("customer_name")
    text = _confirmation(language(mail.body), name, quote, booking_ref)
    if _confirm_to_customer(sid, mail, text, _terms(quote, booking_ref)):
        db.set_booking_customer_state(sid, CONFIRMED_SENT, quote["total_minor"])


def _start_waiter(sid: int, mail: Mail, deadline: float) -> None:
    threading.Thread(
        target=_wait_for_airline, args=(sid, mail, deadline), name=f"price-{sid}", daemon=True
    ).start()


def _wait_for_airline(sid: int, mail: Mail, deadline: float) -> None:
    """Until the airline's confirmation (with its price) arrives or the wait is over."""
    try:
        while time.monotonic() < deadline and _customer_state(sid) == AWAITING_AIRLINE:
            if _airline_price(sid) is not None:
                break
            time.sleep(1.0)
        with _lock(sid):
            if _customer_state(sid) != AWAITING_AIRLINE:
                return
            if _airline_price(sid) is None:
                db.emit(db.Ev.WARN, "Keine Airline-Bestätigung abgewartet: bestätige Kunde", sid)
            _settle(sid, mail)
    except Exception as e:
        log.exception("session %s: settling booking failed", sid)
        db.emit(db.Ev.ERROR, f"Buchungsbestätigung fehlgeschlagen: {e}"[:300], sid, level="error")


def _alternative(quote: dict[str, Any], below_minor: int) -> dict[str, Any] | None:
    """The cheapest other airline from the quote's own options whose all-in (same trucking)
    stays below the new total. Prices are from the quote time and are labelled as such."""
    if str(quote["currency"]).upper() != "EUR":
        return None
    alts = [a for a in quote.get("alternatives") or [] if isinstance(a, dict)]
    truck = next((a for a in alts if "trucker" in a), None)
    truck_minor = to_minor(Decimal(str(truck["total_eur"]))) if truck else 0
    best: dict[str, Any] | None = None
    for a in alts:
        air = a.get("total_eur_minor")
        if not a.get("airline") or a["airline"] == quote["airline"] or air is None:
            continue
        total = int(air) + truck_minor
        if total < below_minor and (best is None or total < best["total_minor"]):
            best = {"airline": a["airline"], "total_minor": total, "days": a.get("transit_days")}
    return best


def _price_change_text(
    lang: str,
    name: str | None,
    quote: dict[str, Any],
    booking_ref: str,
    new_total: int,
    alt: dict[str, Any] | None,
    already_confirmed: bool,
) -> str:
    if name and has_template_artifacts(name):
        name = None
    cur = quote["currency"]
    old, new = amount(quote["total_minor"], cur), amount(new_total, cur)
    if lang == "de":
        route = f"{quote['origin']} nach {quote['destination']}"
        lead = (
            "in unserer Bestätigung hatten wir den angebotenen Preis genannt. "
            f"{quote['airline']} hat die Buchung {booking_ref} inzwischen bestätigt, "
            "allerdings zu einer höheren Luftfrachtrate."
            if already_confirmed
            else f"vielen Dank für Ihre Zusage. {quote['airline']} hat die Buchung "
            f"{booking_ref} angenommen, allerdings zu einer höheren Luftfrachtrate als in "
            "unserem Angebot."
        )
        parts = [
            f"{'Hallo ' + name if name else 'Guten Tag'},",
            lead,
            f"Für Ihre Sendung von {route} liegt der Gesamtpreis damit bei {new} statt {old}.",
        ]
        if alt:
            days = f" mit etwa {alt['days']} Tagen Flugzeit" if alt.get("days") else ""
            parts.append(
                f"Alternativ könnten wir mit {alt['airline']} fliegen{days}. Nach unserem "
                f"letzten Stand wären das {amount(alt['total_minor'], cur)} insgesamt. Diesen "
                "Preis würden wir vor einer Umbuchung noch einmal bestätigen lassen."
            )
        parts.append(
            f"Ist der neue Gesamtpreis von {new} für Sie in Ordnung? Eine kurze Antwort hier "
            "genügt."
        )
        return "\n\n".join(parts) + f"\n\n{SIGNATURE}\n"
    route = f"{quote['origin']} to {quote['destination']}"
    lead = (
        "our confirmation mentioned the price from our offer. "
        f"{quote['airline']} has since confirmed booking {booking_ref}, but at a higher air rate."
        if already_confirmed
        else f"thanks for confirming. {quote['airline']} has accepted booking {booking_ref}, "
        "but at a higher air rate than in our offer."
    )
    parts = [
        f"{'Hi ' + name if name else 'Hello'},",
        lead,
        f"The total for your shipment from {route} would now be {new} instead of {old}.",
    ]
    if alt:
        days = f" with about {alt['days']} days in the air" if alt.get("days") else ""
        parts.append(
            f"Alternatively, we could fly it with {alt['airline']}{days}. At our last check "
            f"that came to {amount(alt['total_minor'], cur)} in total. We'd have that price "
            "confirmed again before switching."
        )
    parts.append(f"Is the new total of {new} okay for you? A short reply here is all we need.")
    return "\n\n".join(parts) + f"\n\n{SIGNATURE}\n"


def _ask_price_change(sid: int, mail: Mail, actual: tuple[int, str]) -> None:
    """The airline's price differs from our quote: tell the customer before confirming."""
    s = db.get_session(sid) or {}
    quote, b = s["quotes"][0], s["bookings"][0]
    quoted = _quoted_air(quote)
    shown = amount(actual[0], actual[1])
    if quoted is None or quoted[1] != actual[1] or quoted[1] != str(quote["currency"]).upper():
        db.set_booking_customer_state(sid, HUMAN)
        db.emit(
            db.Ev.WARN,
            f"Preisänderung: Airline bestätigt {shown}, neuer Gesamtpreis nicht berechenbar. "
            "Bitte Kunde manuell informieren",
            sid,
            level="warn",
        )
        return
    new_total = int(quote["total_minor"]) + actual[0] - quoted[0]
    booking_ref = str(b.get("booking_reference") or b["team_reference"])
    name = (s.get("request") or {}).get("customer_name")
    lang = language(mail.body)
    if new_total < int(quote["total_minor"]):  # cheaper: nothing to agree, confirm the truth
        text = _confirmation(lang, name, quote, booking_ref, new_total)
        if _confirm_to_customer(sid, mail, text, _terms(quote, booking_ref)):
            db.set_booking_customer_state(sid, CONFIRMED_SENT, new_total)
            db.emit(db.Ev.STATUS, f"Airline-Preis niedriger ({shown}): bestätigt", sid)
        return
    already = b.get("customer_state") == CONFIRMED_SENT
    alt = _alternative(quote, new_total)
    text = _price_change_text(lang, name, quote, booking_ref, new_total, alt, already)
    terms = _terms(quote, booking_ref, *([alt["airline"]] if alt else []))
    if not _confirm_to_customer(sid, mail, text, terms, category="price_change"):
        db.set_booking_customer_state(sid, HUMAN, new_total)
        return
    db.set_booking_customer_state(sid, PRICE_ASKED, new_total)
    db.emit(
        db.Ev.WARN,
        f"Preisänderung: Airline bestätigt {shown} statt {amount(quoted[0], quoted[1])}. "
        f"Kunde gefragt, neuer Gesamtpreis {amount(new_total, quote['currency'])}"
        + (f", Alternative {alt['airline']}" if alt else ""),
        sid,
        level="warn",
    )


def _on_price_change_answer(mail: Mail, sid: int, s: dict[str, Any], b: dict[str, Any]) -> None:
    """Yes to the new total: confirm at it. Anything else (no, the alternative, unclear):
    a human decides, because the booking cannot be cancelled or swapped by the agent."""
    quote = s["quotes"][0]
    text = own_text(mail.body)
    alt = _alternative(quote, int(b.get("customer_total_minor") or 0))
    wants_other = bool(_WANTS_OTHER.search(text)) or bool(
        alt and alt["airline"].lower() in text.lower()
    )
    verdict = is_acceptance(mail.body)
    if verdict is True and not wants_other and b.get("customer_total_minor") is not None:
        booking_ref = str(b.get("booking_reference") or b["team_reference"])
        name = (s.get("request") or {}).get("customer_name")
        body = _confirmation(
            language(mail.body), name, quote, booking_ref, int(b["customer_total_minor"])
        )
        if _confirm_to_customer(sid, mail, body, _terms(quote, booking_ref)):
            db.set_booking_customer_state(sid, PRICE_ACCEPTED)
            db.emit(db.Ev.STATUS, "Kunde akzeptiert neuen Preis: Buchung bestätigt", sid)
        return
    what = (
        "wünscht die Alternative"
        if wants_other
        else ("lehnt ab" if verdict is False else "unklare Antwort")
    )
    db.set_booking_customer_state(sid, HUMAN)
    db.emit(
        db.Ev.WARN,
        f"Preisänderung: Kunde {what}. Bitte übernehmen, nichts wurde umgebucht",
        sid,
        level="warn",
    )


def _last_customer_mail(sid: int) -> Mail | None:
    s = db.get_session(sid) or {}
    customer = address(str(s.get("customer") or ""))
    mails = [
        m
        for m in s.get("mails") or []
        if m["direction"] == "in" and customer and address(str(m["sender"] or "")) == customer
    ]
    if not mails:
        return None
    r = mails[-1]
    return Mail(
        id=int(r["id"]),
        thread_root=sid,
        sender=str(r["sender"]),
        to=settings.team_mailbox,
        subject=str(r["subject"] or ""),
        body=str(r["body"] or ""),
        received_at=str(r["received_at"]),
    )


def _late_price_change(sid: int) -> None:
    """Airline price arrived after we had already confirmed the customer."""
    try:
        with _lock(sid):
            s = db.get_session(sid) or {}
            actual = _airline_price(sid)
            mail = _last_customer_mail(sid)
            if (
                _customer_state(sid) != CONFIRMED_SENT
                or actual is None
                or mail is None
                or not s.get("quotes")
                or not _differs(s["quotes"][0], actual)
            ):
                return
            _ask_price_change(sid, mail, actual)
    except Exception as e:
        log.exception("session %s: late price change failed", sid)
        db.emit(db.Ev.ERROR, f"Preisänderung nicht gemeldet: {e}"[:300], sid, level="error")


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
    if sid is not None:
        from app.agents.bids import parse_price

        found = parse_price(mail.body)
        if found and found[1]:
            db.set_airline_price(sid, to_minor(found[0]), found[1])
            # awaiting: the waiter picks it up; already confirmed: tell the customer now
            if _customer_state(sid) == CONFIRMED_SENT:
                threading.Thread(
                    target=_late_price_change, args=(sid,), name=f"late-{sid}", daemon=True
                ).start()
    return True


# ---------------------------------------------------------------- catch-up after restart


def catch_up() -> None:
    """Acceptances that arrived while no booking agent ran: book them now (rates are re-fetched
    if expired). Only quoted, unbooked sessions with a customer reply after our quote.
    Bookings still waiting for the airline's price get their waiter back."""
    for b in db.bookings_with_customer_state(AWAITING_AIRLINE):
        sid = int(b["session_id"])
        mail = _last_customer_mail(sid)
        if mail is None:
            continue
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(str(b["created_at"]))).total_seconds()
        except ValueError:
            age = 0.0
        _start_waiter(sid, mail, time.monotonic() + max(0.0, settings.airline_confirm_wait_s - age))
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
