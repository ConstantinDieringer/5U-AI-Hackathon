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
   a human. A customer's cancellation of a booked shipment is cancelled through the portal
   (DELETE /v1/portal/bookings/{ref}); nothing is ever rebooked automatically.
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
from app.mail import multilingual
from app.mail.language import detect
from app.mail.router import address
from app.mail.style import SIGNATURE, amount, booked_joke, has_template_artifacts
from app.mail.subjects import shipment_subject
from app.models import Mail
from app.pricing.pricing import to_minor
from app.pricing.reference import airline_email
from app.quotes import operational as selected_quote

log = logging.getLogger(__name__)

FRESH_RATE_BUDGET_S = 30.0  # re-reading an expired rate before booking never takes longer

_YES = re.compile(
    r"\b(book|book it|go ahead|proceed|accept|accepted|works for us|"
    r"sounds good|let'?s do it|approved?|"
    r"buchen|einverstanden|passt|nehmen wir|angenommen|"
    r"réserve\w*|reserv\w*|prenot\w*|procedi\w*|akzeptier\w*|d'accord|adelante|"
    r"allez-y|confirm(?:ons|amos|iamo)|aceit\w*|acept\w*|de acuerdo|de acordo|va bene|"
    r"boeken|boek het|akkoord|ga uw gang|ga je gang|boka|godkänn\w*|godkend\w*|"
    r"bestil\w*|rezerw\w*|zarezerw\w*|akceptuj\w*|potwierdzam|rezervuj\w*|souhlasím|"
    r"rezervați|acceptăm|de acord|onayl\w*|kabul\w*|rezervasyon|"
    r"бронир\w*|заброниру\w*|согласны|подтверждаем|принимаем)\b"
    # CJK has no word boundaries: these phrases match without \b
    r"|订舱|预订|請預訂|请预订|同意|确认预订|可以订|予約(?:して|をお願い)|手配をお願い|承認します"
    r"|예약해|진행해 주세요|승인합니다|احجز|نوافق",
    re.IGNORECASE,
)
_NO = re.compile(
    r"\b(not|don'?t|do not|no thanks|decline|too expensive|cancel|nicht|kein\w*|leider|"
    r"zu teuer|absagen|stornier\w*|pas|non|no|niet|geen|nie|inte|ikke|não|nu|değil|"
    r"не|нет)\b"
    r"|不要|不用|别订|不订|取消|いいえ|キャンセル|しません|아니요|취소|لا",
    re.IGNORECASE,
)
_DECLINE = re.compile(  # wins over any "yes" word ("we will not proceed")
    r"too expensive|decline|not (?:go ahead|proceed|book)|no thanks|zu teuer|absagen|"
    r"nicht buchen|\b(?:will\s+not|won't|would\s+not|wouldn't|cannot|can't|"
    r"do\s+not|don't)\s+(?:go\s+ahead|proceed|book|accept|approve|reserve)\b|"
    r"trop cher|demasiado caro|muito caro|troppo caro|te duur|za drogo|för dyrt|pahalı|"
    r"слишком дорого|太贵|高すぎ|너무 비싸",
    re.IGNORECASE,
)
# The customer calls off a booked shipment. Questions about cancellation terms are no
# cancellation ("what is your cancellation fee?", "in case we need to cancel").
_CANCEL = re.compile(
    r"\b(?:cancel(?:l?ed)?|storn(?:o|ier\w*)|annul(?:er|ez|ieren)\w*|cancelar|cancele|"
    r"annullare|annullate|call (?:it )?off|withdraw (?:the|our) (?:booking|order))\b",
    re.IGNORECASE,
)
_CANCEL_TERMS = re.compile(
    r"cancell?ation (?:fee|polic|terms|charge|cost)|storno(?:gebühr|kosten|bedingung)|"
    r"\b(?:if|in case|falls|wenn|should) (?:we|you|i)? ?(?:need to |have to |müssen )?"
    r"(?:cancel|stornier)|\bcan (?:we|i) cancel\b.*\blater\b|\bnot cancel|nicht stornier",
    re.IGNORECASE,
)
_CONFIRMED = re.compile(r"\bBooking\s+(BK-[A-Za-z0-9]+)\s+confirmed\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_locks: dict[int, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(session_id: int) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.RLock())


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

_PENDING_APPROVAL = re.compile(
    r"\b(?:check|checking|prüfen|prüfe|klären|kläre)\b[^.!?\n]{0,60}"
    r"\b(?:whether|if|ob)\b",
    re.IGNORECASE,
)

# A yes that waits on something: a condition, a sign-off still to come or a later answer.
# These never book by rules or by a second opinion ("Sounds good, but our CFO has to sign
# first", "Go ahead with option 2 if it still arrives before Friday").
_POLITE_IF = re.compile(
    r"\b(?:let|tell|inform|notify)\s+(?:me|us)\s+(?:know\s+)?(?:if|when|once|whether)\b"
    r"|\b(?:melde|meldet|sag|sagt|informier\w*)\s+(?:dich|euch|sich|mir|uns|bescheid)?\s*"
    r"(?:bescheid\s+)?(?:wenn|falls|sobald|ob)\b",
    re.IGNORECASE,
)
_CONDITION = re.compile(
    r"\b(?:if|once|unless|provided|as\s+long\s+as|assuming|subject\s+to|on\s+condition"
    r"|wenn|falls|sofern|vorausgesetzt|sobald|unter\s+der\s+bedingung)\b",
    re.IGNORECASE,
)
_SIGN_OFF = re.compile(
    r"\b(?:sign\w*|approv\w*|authori[sz]\w*|freigabe|freigeben|genehmig\w*|unterschr\w*"
    r"|zustimm\w*|zustimmung)\b",
    re.IGNORECASE,
)
_STILL_OPEN = re.compile(
    r"\b(?:first|before|still|pending|has\s+to|have\s+to|needs?\s+to|must|waiting"
    r"|erst|noch|muss|müssen|ausstehend)\b|\?",
    re.IGNORECASE,
)


def _held_yes(text: str) -> bool:
    """A yes sentence carrying a condition, a pending sign-off, or a reply that promises a
    later answer: consent is not current and explicit yet."""
    if _DEFERRAL.search(text):
        return True
    for sentence in re.split(r"(?<=[.!?\n])\s*", _POLITE_IF.sub(" ", text)):
        if not _YES.search(sentence):
            continue
        if _CONDITION.search(sentence):
            return True
        if _SIGN_OFF.search(sentence) and _STILL_OPEN.search(sentence):
            return True
    # "Sounds good. Our CFO has to sign first." across sentences
    return bool(_SIGN_OFF.search(text) and _STILL_OPEN.search(text) and _YES.search(text))


# Changed cargo after the quote (round 86, session 24377: 753 kg quoted, the customer wrote
# "now 5 pieces, 1317 kg", later "yes, book" and the 753 kg rate was booked). Numbers in the
# customer's own words that differ from the quoted request mean the offer no longer fits.
_NUMBER = r"(\d{1,3}(?:[.,]\d{3})+(?![.,]?\d)|\d+(?:[.,]\d+)?)"
_CARGO_FIELDS = (
    (
        "weight_kg",
        "Gewicht",
        "kg",
        re.compile(_NUMBER + r"\s*(?:kgs?|kilos?|kilograms?)\b", re.IGNORECASE),
        0.5,
    ),
    (
        "pieces",
        "Stückzahl",
        "Stück",
        re.compile(
            _NUMBER + r"\s*(?:pieces|pcs|packages|pkgs|cartons|crates|pallets|colli|boxes"
            r"|stück|stk|paletten|kartons|kisten|packstücke)\b",
            re.IGNORECASE,
        ),
        0.0,
    ),
    (
        "volume_cbm",
        "Volumen",
        "cbm",
        re.compile(_NUMBER + r"\s*(?:cbm|m3|m³)", re.IGNORECASE),
        0.01,
    ),
)


def _parse_number(raw: str) -> Decimal | None:
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):  # 1.317 / 1,317 = thousands
        raw = re.sub(r"[.,]", "", raw)
    try:
        return Decimal(raw.replace(",", "."))
    except ArithmeticError:
        return None


def cargo_revision(body: str, request: dict[str, Any] | None) -> list[str]:
    """What the customer's own words change against the quoted request, e.g.
    ["Gewicht 753 → 1317 kg"]. Empty when nothing differs or nothing is stated."""
    text = own_text(body)
    changes: list[str] = []
    for field, label, unit, pattern, tolerance in _CARGO_FIELDS:
        quoted = (request or {}).get(field)
        if quoted is None:
            continue
        values = [v for v in (_parse_number(m.group(1)) for m in pattern.finditer(text)) if v]
        if not values:
            continue
        old = Decimal(str(quoted))
        new = [v for v in values if abs(v - old) > Decimal(str(tolerance))]
        if new:  # "800 kg instead of 640 kg" names the old figure too
            changes.append(f"{label} {old.normalize():f} → {new[0].normalize():f} {unit}")
    return changes


def _revised_key(sid: int) -> str:
    return f"booking.revised.{sid}"


def is_acceptance(body: str) -> bool | None:
    """True = accepts, False = declines, None = unclear (left to a human)."""
    text = own_text(body).replace("’", "'").replace("‘", "'")
    if _DECLINE.search(text):
        return False
    pending = any(
        _PENDING_APPROVAL.search(sentence) and _YES.search(sentence)
        for sentence in re.split(r"[.!?\n]", text)
    )
    if _PREBOOK_QUESTION.search(text) or pending or _held_yes(text):
        return None  # mentioning booking in a question/condition is not consent
    if config.settings.revision_not_acceptance and _REVISION.search(text):
        return None
    yes, no = bool(_YES.search(text)), bool(_NO.search(text))
    if yes and not no and "?" in text:
        return None  # a yes next to an open question is not decided yet: model or a person
    if yes and not no:
        return True
    if no and not yes:
        return False
    return None


_DEFERRAL = re.compile(
    r"\b(?:get|come)\s+back\s+to\s+you|\blook\s+(?:at|into|over)\s+(?:it|this|the\s+offer)|"
    r"\b(?:review|discuss|check)\w*\s+(?:it\s+|this\s+|the\s+offer\s+)?internally|"
    r"\bmelde\w*\s+(?:mich|uns)|\bkomm\w*\s+(?:darauf\s+)?zurück|\bschau\w*\s+(?:es|uns\s+das)\s+an",
    re.IGNORECASE,
)
# After a pre-booking answer: "book the faster one instead" must never book the quoted one
# (also when REVISION_NOT_ACCEPTANCE is off). Stricter only: it can stop a booking.
_OTHER_OPTION = re.compile(
    r"\binstead\b|\bstattdessen\b|\balternativ\w*|\bother\s+(?:airline|carrier|option|one)|"
    r"\b(?:faster|quicker|cheaper|slower)\s+(?:one|option|connection|flight|airline)|"
    r"\b(?:schneller|günstiger|billiger)\w*\s+(?:verbindung|option|variante|flug|airline)|"
    r"\bdie\s+(?:schnellere|günstigere|billigere)\b|\bandere\w*\s+(?:airline|option|verbindung)",
    re.IGNORECASE,
)


def _asks_price(body: str) -> bool:
    """A price-match or "does the price stay?" question, answered even with PREBOOK_ANSWER off."""
    from app.agents import prebook

    asked = prebook.parse(own_text(body))
    return asked is not None and "price" in asked.topics


def classify_reply(body: str) -> str:
    """yes | no | question | deferral | unclear, by rules (no LLM). Only "yes" books, exactly
    as is_acceptance decides. "question": every question sentence names a topic of the quoted
    option (prebook.parse); a negation word inside it ("is pickup not included?") is no
    decline, an explicit decline ("we will not proceed") stays "no"."""
    from app.agents import prebook

    verdict = is_acceptance(body)
    if verdict is True:
        return "yes"
    text = own_text(body)
    if verdict is False and _DECLINE.search(text):
        return "no"
    if prebook.parse(text) is not None:
        return "question"
    if verdict is False:
        return "no"
    if _PENDING_APPROVAL.search(text) or _DEFERRAL.search(text):
        return "deferral"
    return "unclear"


def language(body: str, sid: int | None = None) -> str:
    """de or en for our own text (send_customer translates fr/es/it). A short reply with no
    language of its own ("OK", "Passt") keeps the language of the customer's request."""
    lang = detect(own_text(body), default="")
    if lang in ("de", "en"):
        return lang
    if not lang and sid is not None:
        request = (db.get_session(sid) or {}).get("request") or {}
        if request.get("language") == "de":
            return "de"
    return "en"


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
    from app.pricing.portal_sync import fresh_rate, next_break
    from app.pricing.pricing import chargeable_kg

    weight = float(request["weight_kg"])
    volume = float(request.get("volume_cbm") or 0.0)
    deadline = time.monotonic() + FRESH_RATE_BUDGET_S
    best: dict[str, Any] | None = None
    # same weights as the quote: exact and the next break (whichever was cheaper is quoted)
    for w in (weight, next_break(code, chargeable_kg(weight, volume))):
        if w is None:
            continue
        key = (quote["origin"], quote["destination"], code, w, volume)
        try:
            raw = fresh_rate(key, stale_rate_id=str(quote["rate_id"]), deadline=deadline)
        except hackloop.HackloopError as e:
            log.warning("fresh rate %s at %s kg failed: %s", code, w, e)
            continue
        if best is None or Decimal(str(raw["total"])) < Decimal(str(best["total"])):
            best = raw
    return str(best["rate_id"]) if best else None


def _book_portal(sid: int, quote: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Book the quoted rate; on expired/unknown fetch a fresh one once. Raises HackloopError."""
    ref = db.team_reference(sid)
    state = db.reply_workflow(sid)
    if state and not db.reply_authorized(sid, (state.get("approval") or {}).get("mail_id", 0)):
        raise hackloop.CustomerSendBlocked("Approval superseded before booking transport")
    try:
        return hackloop.book(quote["rate_id"], ref)
    except hackloop.HackloopError as e:
        if db.reply_workflow(sid) is not None:
            raise  # a replacement rate needs a new offer and a new approval
        if e.status not in (404, 410):
            raise
        db.emit(db.Ev.WARN, f"Rate {quote['rate_id']} {e.status}: hole neue Rate", sid)
        fresh = _fresh_rate_id(quote, request)
        if fresh is None:
            raise
        return hackloop.book(fresh, ref)


def _book_by_email(sid: int, quote: dict[str, Any], request: dict[str, Any], to: str) -> None:
    state = db.reply_workflow(sid)
    if state and not db.reply_authorized(sid, (state.get("approval") or {}).get("mail_id", 0)):
        raise hackloop.CustomerSendBlocked("Approval superseded before booking email")
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
    session = db.get_session(sid) or {}
    quotes = session.get("quotes") or []
    if quotes:
        quote = selected_quote(session) or quotes[0]
        subject = shipment_subject(
            quote.get("origin"), quote.get("destination"), language(mail.body, sid), subject
        )
    workflow = session.get("workflow")
    version = workflow["request_version"] if workflow else 0
    if (
        workflow
        and category == "booking_confirmation"
        and (workflow.get("approval") or {}).get("conditions")
    ):
        checked = (
            "Ich habe deine Buchungsbedingungen geprüft und dieses Angebot erfüllt sie."
            if language(mail.body, sid) == "de"
            else "I have checked your booking conditions and this offer meets them."
        )
        text = checked + "\n\n" + text
    if workflow:
        effect = db.reply_effect(sid, mail.id, category, version)
        if effect["status"] == "done":
            return True
        if effect["status"] != "pending":
            return False

    def before_send() -> None:
        if workflow:
            if not db.reply_current(sid, mail.id, version):
                raise hackloop.CustomerSendBlocked("Customer confirmation superseded")
            db.reply_effect(sid, mail.id, category, version, dispatch=True)

    sent = hackloop.send_customer(
        to,
        subject,
        text,
        in_reply_to=mail.id,
        session_id=sid,
        protected_terms=protected_terms,
        language_code=multilingual.mail_language(mail),
        tracking_booked=category == "booking_confirmation",
        guard_transport=before_send if workflow else None,
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
    if workflow:
        db.reply_effect(sid, mail.id, category, version, result={"mail_id": sent["id"]})
    return True


def handle_reply(
    mail: Mail, session_id: int | None, *, consent_evidence: str | None = None
) -> None:
    """Customer reply in a thread. Never raises (runs in a worker thread)."""
    if session_id is None:
        return
    try:
        with _lock(session_id):
            _handle(mail, session_id, consent_evidence=consent_evidence)
    except Exception as e:
        log.exception("session %s: booking failed", session_id)
        db.emit(
            db.Ev.ERROR,
            f"Buchung fehlgeschlagen: {type(e).__name__}: {e}"[:300],
            session_id,
            level="error",
        )


def _hand_over_revision(mail: Mail, sid: int, changes: list[str]) -> None:
    """The cargo changed after our quote: the quoted rate no longer fits and a second quote
    never counts. Never book the old offer (not even on a later "yes"), ask no generic
    question; a person takes over and decides what to tell the customer."""
    first = db.kv_get(_revised_key(sid)) is None
    if first:
        db.kv_set(_revised_key(sid), str(mail.id))
        db.takeover(sid, "Agent (Sendungsdaten geändert)")
    what = ", ".join(changes) if changes else "früher in diesem Thread"
    db.emit(
        "needs_human",
        f"Kunde hat die Sendung geändert ({what}): altes Angebot wird nicht gebucht, "
        f"bitte Kunden selbst antworten (Mail {mail.id})"[:300],
        sid,
        level="error" if first else "warn",
        mail_id=mail.id,
        changes=changes,
    )


def wants_cancellation(body: str) -> bool:
    """The customer's own words call off the booked shipment."""
    text = own_text(body)
    return bool(_CANCEL.search(text)) and not _CANCEL_TERMS.search(text)


def _cancel(mail: Mail, sid: int, s: dict[str, Any], b: dict[str, Any]) -> None:
    """Cancel through the portal (DELETE /v1/portal/bookings/{ref}), then confirm in the
    thread. Once per session; without a booking reference or on an error a person decides."""
    ref = b.get("booking_reference")
    if db.is_paused(sid) or db.is_manual(sid):
        db.emit("needs_human", f"Storno-Wunsch in pausierter Session (Mail {mail.id})", sid)
        return
    if not ref:
        db.emit(
            "needs_human",
            f"Kunde storniert, aber keine Buchungsnummer bekannt: bitte manuell (Mail {mail.id})",
            sid,
            level="error",
        )
        return
    if not db.kv_claim(f"booking.cancel.{sid}"):
        return
    try:
        result = hackloop.cancel_booking(str(ref))
    except hackloop.HackloopError as exc:
        db.emit(
            "needs_human",
            f"Storno von {ref} fehlgeschlagen ({exc.status}): bitte manuell, Kunde wartet",
            sid,
            level="error",
        )
        return
    if str(result.get("status", "")).lower() != "cancelled":
        db.emit("needs_human", f"Storno von {ref}: unerwartete Antwort {result}"[:300], sid,
                level="error")  # fmt: skip
        return
    db.cancel_session_booking(sid, f"{ref} auf Kundenwunsch (Mail {mail.id})")
    quote = selected_quote(s) or (s.get("quotes") or [{}])[0]
    lang = language(mail.body, sid)
    name = (s.get("request") or {}).get("customer_name")
    if not name or has_template_artifacts(str(name)):
        name = None
    route = (
        f" von {quote.get('origin')} nach {quote.get('destination')}"
        if lang == "de"
        else f" from {quote.get('origin')} to {quote.get('destination')}"
    ) if quote.get("origin") and quote.get("destination") else ""  # fmt: skip
    if lang == "de":
        text = (
            f"{'Hallo ' + name if name else 'Guten Tag'},\n\n"
            f"erledigt, Ihre Buchung {ref}{route} ist storniert.\n\n"
            "Wenn Sie die Sendung später doch verschicken möchten, antworten Sie einfach auf "
            "diese Mail und wir schicken Ihnen ein neues Angebot.\n\n"
            f"{SIGNATURE}\n"
        )
    else:
        text = (
            f"{'Hi ' + name if name else 'Hello'},\n\n"
            f"done, your booking {ref}{route} is cancelled.\n\n"
            "If you want to ship it later after all, just reply to this email and we will "
            "send you a fresh offer.\n\n"
            f"{SIGNATURE}\n"
        )
    _confirm_to_customer(sid, mail, text, protected_terms=(str(ref),), category="cancellation")


def _guarded(text: str) -> bool:
    """A question, a condition, a pending check or a change request: the rules hold these
    back on purpose, and no second opinion may turn them into a booking."""
    pending = any(
        _PENDING_APPROVAL.search(sentence) and _YES.search(sentence)
        for sentence in re.split(r"[.!?\n]", text)
    )
    return bool(
        pending
        or _held_yes(text)
        or _PREBOOK_QUESTION.search(text)
        or _REVISION.search(text)
        or _OTHER_OPTION.search(text)
    )


def _consent(body: str, evidence: str | None, sid: int) -> bool | None:
    """Rules first. CONSENT_LLM_CHECK: a reply the rules leave undecided (no signal, or yes
    and no words together), or a yes next to a doubt word, gets one checked gateway call.
    The rules' explicit decline always stands; a question/condition/change never books."""
    verdict = _rescue_acceptance(body, evidence)
    if not settings.consent_llm_check or verdict is False:
        return verdict
    from app.agents import consent_check

    text = own_text(body).replace("’", "'").replace("‘", "'")
    if verdict is True and not consent_check.DOUBT.search(text):
        return True
    if _guarded(text):
        return None
    label = consent_check.classify(text, sid)
    db.emit(
        "consent_check",
        f"Zusage-Prüfung per Modell: {label} (Regeln: {verdict})",
        sid,
        level="info" if label == "book" or verdict is None else "warn",
    )
    if label == "book":
        return True
    if verdict is True:
        # a yes next to a doubt word needs the model's own "book": "unclear" (also returned on
        # timeout, budget or gateway error) holds the booking for clarification
        return None
    return False if label == "decline" else None


def _rescue_acceptance(body: str, evidence: str | None) -> bool | None:
    verdict = is_acceptance(body)
    if verdict is not None or not evidence:
        return verdict
    own = own_text(body).strip()
    # The stronger owner may interpret a bare affirmation in the current offer context.
    # Any added condition, change, question or deferred approval still uses the normal path.
    if evidence.strip().casefold() in own.casefold() and re.fullmatch(
        r"(?:yes(?:\s+please)?|ja(?:\s+bitte)?|ok(?:ay)?|i confirm|that works|bestätigt)[.!\s]*",
        own,
        re.IGNORECASE,
    ):
        return True
    return None


def _mail_acceptance(
    mail: Mail, evidence: str | None = None, sid: int | None = None
) -> bool | None:
    """Rules plus CONSENT_LLM_CHECK (_consent, needs a session), then the multilingual guard."""
    workflow_verdict = db.reply_authorized(sid, mail.id) if sid is not None else None
    if workflow_verdict is not None:
        return workflow_verdict if workflow_verdict else None
    verdict = (
        _consent(mail.body, evidence, sid)
        if sid is not None
        else _rescue_acceptance(mail.body, evidence)
    )
    authorization = multilingual.consent_allowed(mail)
    if authorization is False and verdict is True:
        return None
    if (
        authorization is True
        and verdict is None
        and re.fullmatch(
            r"(?:yes(?:\s+please)?|ok(?:ay)?)[.!\s]*", own_text(mail.body), re.IGNORECASE
        )
    ):
        return True
    return verdict


def _handle(mail: Mail, sid: int, *, consent_evidence: str | None = None) -> None:
    s = db.get_session(sid)
    if not s or not s.get("quotes"):
        return
    mail = multilingual.prepare(mail, sid, (s.get("request") or {}).get("language") or "und")
    if any(b["status"] in ("booked", "confirmed") for b in s.get("bookings", [])):
        from app.agents import tracking

        b = s["bookings"][0]
        if wants_cancellation(mail.body):
            _cancel(mail, sid, s, b)
        elif b.get("customer_state") == PRICE_ASKED:  # open price question: answer it first
            _on_price_change_answer(mail, sid, s, b, consent_evidence=consent_evidence)
        elif tracking.is_status_question(mail.body):  # "where is it?" in their own thread
            tracking.handle(mail, sid, booked_session=sid)
        else:
            log.info("session %s already booked, reply %s ignored", sid, mail.id)
        return
    changes = cargo_revision(mail.body, s.get("request"))
    if not s.get("workflow") and (changes or db.kv_get(_revised_key(sid)) is not None):
        _hand_over_revision(mail, sid, changes)
        return
    verdict = _mail_acceptance(mail, consent_evidence, sid)
    if verdict is None and multilingual.consent_allowed(mail) is not False:
        from app.agents import prebook

        # "Is this the fastest option? If so, please go ahead": the stored quote proves it
        if prebook.proven_go_ahead(own_text(mail.body), prebook.facts_for(s)):
            verdict = True
            db.emit(
                db.Ev.STATUS,
                "Zusage unter einer Bedingung, die das Angebot belegt (schnellste/günstigste "
                "Option): buche",
                sid,
                mail_id=mail.id,
            )
    if settings.prebook_answer:
        from app.agents import prebook

        if verdict is True:
            if prebook.answered_before(s) and _OTHER_OPTION.search(own_text(mail.body)):
                verdict = None  # names another option after our answer: never book on it
                db.emit(
                    db.Ev.WARN,
                    "Kunde nennt nach unserer Antwort eine andere Option: nicht gebucht",
                    sid,
                    level="warn",
                )
        elif classify_reply(mail.body) == "question" and prebook.reply(mail, sid, s):
            return  # answered (or deliberately held); booking still waits for a clear yes
    elif verdict is None and _asks_price(mail.body):
        from app.agents import prebook

        # round 144: "can you match their rate?" needs our answer, not "shall we book?"
        if classify_reply(mail.body) == "question" and prebook.reply(
            mail, sid, s, count_nudges=False
        ):
            return
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
    quote = selected_quote(s)
    if quote is None:
        return
    from app.agents import carrier_prefs

    customer = address(mail.sender)
    carrier_prefs.remember(customer, own_text(mail.body), sid)
    if quote["airline"].lower() in carrier_prefs.excluded(customer):
        raise ValueError("customer exclusion blocks booking the quoted carrier")
    request = s.get("request") or {}
    db.emit(db.Ev.STATUS, "Kunde hat angenommen: buche", sid)
    if s.get("workflow"):
        if not db.reply_authorized(sid, mail.id):
            return
        db.reply_effect(sid, mail.id, "book", s["workflow"]["request_version"], dispatch=True)
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

    db.reply_bind_booking(sid, quote["id"])
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
    if _has_trucking(quote) or quote.get("insurance_minor", 0):
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
    quote, b = selected_quote(s), s["bookings"][0]
    actual = _airline_price(sid)
    if actual is not None and _differs(quote, actual):
        _ask_price_change(sid, mail, actual)
        return
    booking_ref = str(b.get("booking_reference") or b["team_reference"])
    name = (s.get("request") or {}).get("customer_name")
    text = _confirmation(language(mail.body, sid), name, quote, booking_ref)
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
        total = int(air) + truck_minor + int(quote.get("insurance_minor", 0))
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
    quote, b = selected_quote(s), s["bookings"][0]
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
    lang = language(mail.body, sid)
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


def _on_price_change_answer(
    mail: Mail,
    sid: int,
    s: dict[str, Any],
    b: dict[str, Any],
    *,
    consent_evidence: str | None = None,
) -> None:
    """Yes to the new total: confirm at it. Anything else (no, the alternative, unclear):
    a human decides, because the booking cannot be cancelled or swapped by the agent."""
    quote = selected_quote(s)
    text = own_text(mail.body)
    alt = _alternative(quote, int(b.get("customer_total_minor") or 0))
    wants_other = bool(_WANTS_OTHER.search(text)) or bool(
        alt and alt["airline"].lower() in text.lower()
    )
    verdict = _mail_acceptance(mail, consent_evidence, sid)
    if verdict is True and not wants_other and b.get("customer_total_minor") is not None:
        booking_ref = str(b.get("booking_reference") or b["team_reference"])
        name = (s.get("request") or {}).get("customer_name")
        body = _confirmation(
            language(mail.body, sid), name, quote, booking_ref, int(b["customer_total_minor"])
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
                or not _differs(selected_quote(s) or s["quotes"][0], actual)
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
            # keep the airline's BK reference: customer confirmation and tracking answers
            # must name it, not our AP tag (rounds 27/29/45/46/51/52 scored half tracking)
            sid = db.update_booking_status(
                f"AP-{tag.group(1)}", "confirmed", booking_reference=m.group(1).upper()
            )
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
        quote_mail = (selected_quote(full) or {}).get("mail_id") or 0
        replies = [
            m
            for m in full["mails"]
            if m["direction"] == "in"
            and m["id"] > quote_mail
            and m.get("category") != "phishing"  # quarantined: never acted on
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
        from app.agents import customer_reply, rescue

        if customer_reply.enabled():
            continue
        threading.Thread(
            target=rescue.take_over if rescue.is_owner(int(s["id"])) else handle_reply,
            args=(mail, int(s["id"])),
            name=f"book-{s['id']}",
            daemon=True,
        ).start()
