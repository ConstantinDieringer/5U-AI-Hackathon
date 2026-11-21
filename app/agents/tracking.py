"""Tracking agent: answers "where is my shipment?" from our own CRM (no portal status call
exists, no LLM needed).

Who asks: 5U's tracking agent (tracking@hackloop.mail) and customers in their own thread.
Which shipment: matched by booking reference (BK-…), our reference (AP-<session>), rate id,
the customer's name/company, or the route. No match -> we ask for the booking reference.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app import db, hackloop
from app.agents.rfq import schedule
from app.config import settings
from app.mail.content import current_message
from app.mail.language import detect
from app.mail.router import address
from app.mail.signature import first_name
from app.mail.style import SIGNATURE, amount
from app.models import Mail, Rfq

log = logging.getLogger(__name__)

_BK = re.compile(r"\bBK-[A-Za-z0-9]{4,}\b", re.IGNORECASE)
_AP = re.compile(r"\bAP-(\d+)\b", re.IGNORECASE)
_RATE = re.compile(r"\bR-[A-Za-z0-9_-]{8,40}\b")
_ROUTE = re.compile(r"\b([A-Z]{3})\s*(?:-|–|to|nach|→|>)\s*([A-Z]{3})\b")
_QUESTION = re.compile(
    r"\?|\b(status|where|when|arriv\w*|eta|track\w*|wo ist|wann|ankunft)\b",
    re.IGNORECASE,
)
_STOP = {"gmbh", "group", "cargo", "logistics", "hackloop", "mail", "the", "and", "air"}


def is_status_question(body: str) -> bool:
    from app.agents.booking import own_text

    return bool(_QUESTION.search(own_text(body)))


def _name_tokens(customer: str) -> set[str]:
    local = address(customer).split("@")[0]
    return {
        t
        for t in re.split(r"[^a-z0-9]+", local.lower())
        if len(t) >= 4 and t not in _STOP
    }


def find(text: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The booking the mail asks about, or None."""
    for m in _BK.finditer(text):
        ref = m.group(0).upper()
        hit = next(
            (r for r in records if (r["booking_reference"] or "").upper() == ref), None
        )
        if hit:
            return hit
    for m in _AP.finditer(text):
        hit = next((r for r in records if r["session_id"] == int(m.group(1))), None)
        if hit:
            return hit
    for m in _RATE.finditer(text):
        hit = next((r for r in records if r["rate_id"] == m.group(0)), None)
        if hit:
            return hit

    low = text.lower()
    routes = {(o, d) for o, d in _ROUTE.findall(text)}
    asked_kg = _weight(text)
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in records:  # newest first
        score = 2 * sum(1 for t in _name_tokens(r["customer"] or "") if t in low)
        if (r["origin"], r["destination"]) in routes:
            score += 2
        elif r["destination"] and re.search(rf"\b{r['destination']}\b", text):
            score += 1
        kg = (r.get("request") or {}).get("weight_kg")
        if score and asked_kg and kg:
            # questions name the weight (seen 21.11: "STR to EZE, 1228 kg"): it separates
            # two shipments of one customer on one lane, and rules out a wrong match
            score += 2 if abs(float(kg) - asked_kg) <= max(1.0, 0.01 * asked_kg) else -3
        if score > 0:
            scored.append((score, r))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])  # stable: newest first among equal scores
    best = scored[0]
    if best[0] < 2:
        return None
    if len(scored) > 1 and scored[1][0] == best[0]:
        log.info(
            "tracking: %s bookings match equally, asking for the reference", len(scored)
        )
        return None  # ambiguous: asking beats answering for the wrong shipment
    return best[1]


_WEIGHT = re.compile(r"(\d[\d.,]*)\s*(?:kg|kilo\w*)\b", re.IGNORECASE)


def _weight(text: str) -> float | None:
    """'1228 kg', '1,228 kg', '1.228 kg' (German thousands) -> 1228.0."""
    m = _WEIGHT.search(text)
    if not m:
        return None
    raw = m.group(1)
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
        raw = re.sub(r"[.,]", "", raw)
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _today() -> date:
    return datetime.now(UTC).date()


def _day(d: date, lang: str) -> str:
    return d.strftime("%d.%m.%Y") if lang == "de" else f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year}"


def _kg(weight: Any, lang: str) -> str:
    text = f"{float(weight):g} kg"
    return text.replace(".", ",") if lang == "de" else text


def _ready(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class _Facts:
    """Every fact the answer states, already formatted in the answer's language."""

    hello: str
    ref: str
    route: str
    weight: str
    status: str
    where: str
    plan: str
    price: str


# Wording variants, picked by session id so consecutive answers do not repeat. Each states
# every fact: reference, route, weight, status (with the airline), where the cargo is now,
# planned departure and arrival, price. No closing formula: answer_text appends the team
# signature after a blank line (app.mail.style).
_VARIANTS: dict[str, tuple[str, ...]] = {
    "en": (
        (
            "{hello}\n\nthanks for checking in. Your shipment {ref} ({route}, {weight}) is {status}. "
            "{where}\n\n{plan} The all-in price remains {price}.\n\n"
            "I'll keep you posted if anything changes."
        ),
        (
            "{hello}\n\nhappy to give you an update on {ref}. The {route} shipment ({weight}) is "
            "{status}. {where}\n\n{plan} The price stays at {price} all-in.\n\n"
            "Just reply here if you need anything else."
        ),
        (
            "{hello}\n\nhere is where things stand with {ref} ({route}, {weight}): it is {status}. "
            "{where}\n\n{plan} Nothing changes on the price: {price} all-in.\n\n"
            "I'll let you know as soon as there is news."
        ),
        (
            "{hello}\n\nthanks for your message. {ref}, your {route} shipment of {weight}, is "
            "{status}. {where}\n\n{plan} The all-in price is {price}, as quoted.\n\n"
            "Let me know if you have any questions."
        ),
    ),
    "de": (
        (
            "{hello}\n\ndanke für Ihre Nachricht. Ihre Sendung {ref} ({route}, {weight}) ist "
            "{status}. {where}\n\n{plan} Der All-in-Preis bleibt bei {price}.\n\n"
            "Sobald sich etwas ändert, melde ich mich."
        ),
        (
            "{hello}\n\ngern ein kurzes Update zu {ref}: Die Sendung {route} ({weight}) ist "
            "{status}. {where}\n\n{plan} Am Preis ändert sich nichts, er liegt bei {price} all-in."
            "\n\nBei Fragen antworten Sie einfach auf diese Mail."
        ),
        (
            "{hello}\n\nhier der aktuelle Stand zu {ref} ({route}, {weight}): Die Sendung ist "
            "{status}. {where}\n\n{plan} Der Preis beträgt {price} all-in.\n\n"
            "Ich halte Sie auf dem Laufenden."
        ),
        (
            "{hello}\n\nvielen Dank für die Nachfrage. {ref}, Ihre Sendung {route} mit {weight}, "
            "ist {status}. {where}\n\n{plan} Der Preis liegt unverändert bei {price} all-in.\n\n"
            "Melden Sie sich gern, wenn Sie noch etwas brauchen."
        ),
    ),
}


def _where_and_plan(r: dict[str, Any], lang: str, today: date) -> tuple[str, str]:
    """Where the cargo is now and the planned flight, from the ready date and the quote's
    schedule (rfq.schedule: departure on the ready date, +1 day with our pickup)."""
    req = r.get("request") or {}
    o, d, days = r.get("origin") or "", r.get("destination") or "", r.get("transit_days")
    pickup = req.get("pickup_zip")
    ready = _ready(req.get("ready_date"))
    ready_raw = str(req.get("ready_date") or "")[:10]
    de = lang == "de"
    # the plan as booked: rfq.schedule as of the booking day, so it matches the quote and does
    # not slide with today's date
    booked = _ready(r.get("booked_at") or "") or today
    when = (
        schedule(
            Rfq.model_construct(
                ready_date=ready_raw, pickup_zip=pickup, pickup_city=req.get("pickup_city")
            ),
            int(days),
            booked,
        )
        if ready and days is not None
        else None
    )
    if ready is None or when is None:
        if de:
            where = "Aktuell hat sie sich noch nicht bewegt, die Ware steht noch bei Ihnen."
            plan = f"Die Laufzeit beträgt rund {days} Tage ab Abflug; die Flugdaten bestätige ich, sobald der Bereitstellungstermin feststeht."
        else:
            where = "Right now it has not moved yet: the cargo is still at your site."
            plan = f"Transit time is about {days} days from departure; I'll confirm the flight dates as soon as we have the ready date."
        return where, plan
    dep, arr = when
    dep_s, arr_s = _day(dep, lang), _day(arr, lang)
    if today < ready:
        if de:
            where = (
                f"Aktuell hat sie sich noch nicht bewegt: Die Ware steht noch bei Ihnen am "
                f"Standort ({pickup}), unser Lkw holt sie am Bereitstellungstag {ready_raw} ab "
                f"und bringt sie zum Flughafen {o}."
                if pickup
                else "Aktuell hat sie sich noch nicht bewegt: Die Ware steht noch bei Ihnen am "
                f"Standort und wird nach der Bereitstellung am {ready_raw} zum Flughafen {o} "
                "geliefert."
            )
        else:
            where = (
                f"Right now it has not moved yet: the cargo is still at your site ({pickup}), "
                f"and our trucker collects it on the ready date, {ready_raw}, and brings it to "
                f"{o} airport."
                if pickup
                else "Right now it has not moved yet: the cargo is still at your site, waiting "
                f"to be delivered to {o} airport once it is ready on {ready_raw}."
            )
    elif today < dep:
        where = (
            f"Laut Buchungsplan ist die Ware seit {ready_raw} bereit und wird gerade zum "
            f"Flughafen {o} gebracht."
            if de
            else f"Per the booked schedule the cargo has been ready since {ready_raw} and is "
            f"on its way to {o} airport."
        )
    elif today < arr:
        where = (
            f"Laut Buchungsplan ist die Ware am {dep_s} ab {o} abgeflogen und auf dem Weg nach {d}."
            if de
            else f"Per the booked schedule the cargo left {o} on {dep_s} and is in transit to {d}."
        )
    else:
        where = (
            f"Laut Buchungsplan ist die Ware am {arr_s} in {d} angekommen."
            if de
            else f"Per the booked schedule the cargo arrived in {d} on {arr_s}."
        )
    if today < dep:
        plan = (
            f"Geplant ist der Abflug ab {o} am {dep_s}, Ankunft in {d} am {arr_s}, also rund "
            f"{days} Tage Laufzeit."
            if de
            else f"It is planned to depart {o} on {dep_s} and arrive in {d} on {arr_s}, about "
            f"{days} days in transit."
        )
    else:
        plan = (
            f"Gebuchter Plan: Abflug ab {o} am {dep_s}, Ankunft in {d} am {arr_s} ({days} Tage)."
            if de
            else f"Booked schedule: departure from {o} on {dep_s}, arrival in {d} on {arr_s} "
            f"({days} days in transit)."
        )
    return where, plan


def answer_text(
    r: dict[str, Any],
    lang: str,
    name: str | None = None,
    variant: int = 0,
    today: date | None = None,
) -> str:
    """Short prose answer: status, where the cargo is now, planned departure and arrival, and
    every CRM fact (reference, route, airline, weight, ready date, price)."""
    lang = "de" if lang == "de" else "en"
    req = r.get("request") or {}
    de = lang == "de"
    airline = r.get("airline") or ""
    if r.get("origin"):
        route = f"{r['origin']} {'nach' if de else 'to'} {r['destination']}"
    else:
        route = r.get("subject") or ""
    if r.get("status") == "confirmed":
        status = (
            f"gebucht und von {airline} bestätigt" if de else f"booked and confirmed by {airline}"
        )
    else:
        status = (
            f"bei {airline} gebucht, die Bestätigung der Airline steht noch aus"
            if de
            else f"booked with {airline}, and the airline's confirmation is still pending"
        )
    where, plan = _where_and_plan(r, lang, today or _today())
    facts = _Facts(
        hello=(f"Hallo {name}," if name else "Guten Tag,")
        if de
        else (f"Hi {name}," if name else "Hello,"),
        ref=r.get("booking_reference") or r.get("team_reference") or "",
        route=route,
        weight=_kg(req["weight_kg"], lang) if req.get("weight_kg") else "",
        status=status,
        where=where,
        plan=plan,
        price=amount(r["price_minor"], r.get("currency") or "EUR") if r.get("price_minor") else "",
    )
    templates = _VARIANTS[lang]
    text = templates[variant % len(templates)].format(**facts.__dict__)
    for gap, fill in ((", )", ")"), (" of ,", ","), (" mit ,", ",")):
        text = text.replace(gap, fill)  # a missing weight leaves no gap
    return f"{text}\n\n{SIGNATURE}\n"


def _not_found_text(lang: str, name: str | None = None) -> str:
    if lang == "de":
        return (
            f"{'Hallo ' + name if name else 'Guten Tag'},\n\n"
            "zu Ihrer Anfrage finde ich leider keine gebuchte Sendung. Könnten Sie mir die "
            "Buchungsnummer (BK-…) oder unsere Referenz (AP-…) nennen? Dann melde ich mich "
            f"sofort.\n\n{SIGNATURE}\n"
        )
    return (
        f"{'Hi ' + name if name else 'Hello'},\n\n"
        "I couldn't find a booked shipment for your request. Could you send me the booking "
        "reference (BK-…) or our reference (AP-…)? I'll get back to you right away.\n\n"
        f"{SIGNATURE}\n"
    )


def _reply(mail: Mail, body: str, session_id: int | None, category: str) -> bool:
    """Send the answer in the asker's thread. False when a human holds the session."""
    if session_id is not None and (db.is_paused(session_id) or db.is_manual(session_id)):
        db.emit(
            db.Ev.WARN,
            "Tracking-Antwort nicht gesendet: Session pausiert/manuell",
            session_id,
        )
        return False
    to = address(mail.sender)
    subject = (
        mail.subject
        if mail.subject.lower().startswith("re:")
        else f"Re: {mail.subject}"
    )
    sent = hackloop.send(to, subject, body, in_reply_to=mail.id)
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=to,
        subject=subject,
        body=body,
        category=category,
        session_id=session_id,
        sender=settings.team_mailbox,
        in_reply_to=mail.id,
        raw=sent,
    )
    return True


def handle(
    mail: Mail, session_id: int | None, booked_session: int | None = None
) -> None:
    """Answer one tracking question. `booked_session` is set when a customer asks in the
    thread of their own booking. Never raises (runs in a worker thread)."""
    try:
        text = f"{mail.subject}\n{mail.body}"
        lang = "de" if detect(text) == "de" else "en"  # other languages get English
        name = first_name(mail.body)
        variant = session_id if session_id is not None else mail.id
        # a question in the customer's own booked thread keeps that session's status
        own_session = session_id if booked_session is None else None
        records = db.tracking_records()
        if booked_session is not None:
            rec = next((r for r in records if r["session_id"] == booked_session), None)
        else:
            rec = find(text, records)
        if (
            rec is None
            and session_id is not None
            and not (_BK.search(text) or _AP.search(text) or _RATE.search(text))
        ):
            history = db.customer_mail_history(session_id, mail.sender, mail.id)
            refs = {
                match.group(0)
                for item in history
                for match in _BK.finditer(current_message(item["body"]).facts)
            }
            if (
                len(refs) == 1
                and _weight(text) is None
                and not re.search(r"\b[A-Z]{3}\b", text)
            ):
                rec = find(next(iter(refs)), records)
        if rec is None:
            log.warning("tracking mail %s: no booking matched", mail.id)
            db.emit(
                db.Ev.WARN,
                f"Tracking-Frage {mail.id}: keine Buchung gefunden",
                session_id,
                level="warn",
            )
            if session_id is not None and db.get_session(session_id):
                from app.agents import clarification

                clarification.ask(
                    mail,
                    session_id,
                    "tracking",
                    "Could you send us the booking reference or our AP reference? We could not identify a unique shipment from this thread and our records.",
                    "Könnten Sie uns die Buchungsnummer oder unsere AP-Referenz nennen? Aus diesem Verlauf und unseren Unterlagen konnten wir keine eindeutige Sendung zuordnen.",
                )
            else:
                sent = _reply(mail, _not_found_text(lang, name), session_id, "tracking_answer")
                if sent and own_session is not None:
                    db.set_status(own_session, "answered", "nach Buchungsnummer gefragt")
            return
        body = answer_text(rec, lang, name, variant)
        if not _reply(mail, body, session_id, "tracking_answer"):
            return
        if own_session is not None:
            db.set_status(own_session, "answered")
        db.emit(
            db.Ev.STATUS,
            f"Tracking beantwortet: {rec['booking_reference']} ({rec['status']})",
            rec["session_id"],
        )
        log.info("tracking mail %s answered with %s", mail.id, rec["booking_reference"])
    except Exception as e:
        log.exception("tracking mail %s failed", mail.id)
        db.emit(
            db.Ev.ERROR,
            f"Tracking fehlgeschlagen: {type(e).__name__}: {e}"[:300],
            session_id,
            level="error",
        )
