"""Tracking agent: answers "where is my shipment?" from our own CRM (no portal status call
exists, no LLM needed).

Who asks: 5U's tracking agent (tracking@hackloop.mail) and customers in their own thread.
Which shipment: matched by booking reference (BK-…), our reference (AP-<session>), rate id,
the customer's name/company, or the route. No match -> we ask for the booking reference.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app import db, hackloop
from app.config import settings
from app.mail.content import current_message
from app.mail.language import detect
from app.mail.router import address
from app.mail.style import SIGNATURE, amount
from app.models import Mail

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
    return {t for t in re.split(r"[^a-z0-9]+", local.lower()) if len(t) >= 4 and t not in _STOP}


def find(text: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The booking the mail asks about, or None."""
    for m in _BK.finditer(text):
        ref = m.group(0).upper()
        hit = next((r for r in records if (r["booking_reference"] or "").upper() == ref), None)
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
        log.info("tracking: %s bookings match equally, asking for the reference", len(scored))
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


def answer_text(r: dict[str, Any], lang: str) -> str:
    req = r.get("request") or {}
    ref = r["booking_reference"] or r["team_reference"]
    connector = "nach" if lang == "de" else "to"
    route = (
        f"{r['origin']} {connector} {r['destination']}"
        if r.get("origin")
        else r.get("subject") or ""
    )
    confirmed = r["status"] == "confirmed"
    weight = req.get("weight_kg")
    ready = req.get("ready_date")
    pickup = req.get("pickup_zip")
    days = r.get("transit_days")
    if lang == "de":
        status = (
            "gebucht und von der Airline bestätigt"
            if confirmed
            else "gebucht, die Bestätigung der Airline steht noch aus"
        )
        lines = [
            "Guten Tag,",
            "",
            f"hier der aktuelle Stand zur Sendung {ref}:",
            "",
            f"Status: {status}",
            f"Strecke: {route} mit {r['airline']}",
        ]
        if weight:
            lines.append(f"Gewicht: {float(weight):g} kg")
        if pickup:
            lines.append(f"Abholung in {pickup}, Lkw zum Abflughafen {r['origin']}")
        if ready:
            lines.append(f"Abholbereit ab: {ready}")
        if days is not None:
            lines.append(f"Laufzeit: ca. {days} Tage ab Abflug")
        if r.get("price_minor"):
            lines.append(f"Preis: {amount(r['price_minor'], r['currency'])} all-in")
        lines += ["", SIGNATURE]
        return "\n".join(lines)
    status = (
        "booked and confirmed by the airline"
        if confirmed
        else "booked, waiting for the airline's confirmation"
    )
    lines = [
        "Hello,",
        "",
        f"here is the current status of shipment {ref}:",
        "",
        f"Status: {status}",
        f"Route: {route} with {r['airline']}",
    ]
    if weight:
        lines.append(f"Weight: {float(weight):g} kg")
    if pickup:
        lines.append(f"Pickup at {pickup}, trucked to {r['origin']} airport")
    if ready:
        lines.append(f"Ready for pickup: {ready}")
    if days is not None:
        lines.append(f"Transit time: about {days} days from departure")
    if r.get("price_minor"):
        lines.append(f"Price: {amount(r['price_minor'], r['currency'])} all-in")
    lines += ["", SIGNATURE]
    return "\n".join(lines)


def _not_found_text(lang: str) -> str:
    if lang == "de":
        return (
            "Guten Tag,\n\nzu Ihrer Anfrage finden wir leider keine gebuchte Sendung. "
            "Könnten Sie uns die Buchungsnummer (BK-…) oder unsere Referenz (AP-…) "
            f"nennen? Dann melden wir uns sofort.\n\n{SIGNATURE}"
        )
    return (
        "Hello,\n\nwe couldn't find a booked shipment for your request. Could you send us "
        "the booking reference (BK-…) or our reference (AP-…)? We'll get back to you right "
        f"away.\n\n{SIGNATURE}"
    )


def _reply(mail: Mail, body: str, session_id: int | None, category: str) -> None:
    if session_id is not None and (db.is_paused(session_id) or db.is_manual(session_id)):
        db.emit(
            db.Ev.WARN,
            "Tracking-Antwort nicht gesendet: Session pausiert/manuell",
            session_id,
        )
        return
    to = address(mail.sender)
    subject = mail.subject if mail.subject.lower().startswith("re:") else f"Re: {mail.subject}"
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


def handle(mail: Mail, session_id: int | None, booked_session: int | None = None) -> None:
    """Answer one tracking question. `booked_session` is set when a customer asks in the
    thread of their own booking. Never raises (runs in a worker thread)."""
    try:
        text = f"{mail.subject}\n{mail.body}"
        lang = "de" if detect(text) == "de" else "en"
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
            if len(refs) == 1 and _weight(text) is None and not re.search(r"\b[A-Z]{3}\b", text):
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
                _reply(mail, _not_found_text(lang), session_id, "tracking_answer")
            return
        _reply(mail, answer_text(rec, lang), session_id, "tracking_answer")
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
