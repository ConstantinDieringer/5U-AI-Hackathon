"""Tracking agent: answers "where is my shipment?" from our own CRM (no portal status call
exists, no LLM needed).

Who asks: 5U's tracking agent (tracking@hackloop.mail) and customers in their own thread.
Which shipment: matched by booking reference (BK-…), our reference (AP-<session>), rate id,
the customer's name/company, or the route. No match -> we ask for the booking reference.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from app import db, hackloop
from app.config import settings
from app.mail.router import address
from app.models import Mail

log = logging.getLogger(__name__)

_BK = re.compile(r"\bBK-[A-Za-z0-9]{4,}\b", re.IGNORECASE)
_AP = re.compile(r"\bAP-(\d+)\b", re.IGNORECASE)
_RATE = re.compile(r"\bR-[A-Za-z0-9_-]{8,40}\b")
_ROUTE = re.compile(r"\b([A-Z]{3})\s*(?:-|–|to|nach|→|>)\s*([A-Z]{3})\b")
_DE = re.compile(
    r"\b(bitte|danke|sendung|wo ist|status der|grüße|guten tag|hallo)\b", re.IGNORECASE
)
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
    best: tuple[int, dict[str, Any]] | None = None
    for r in records:  # newest first: ties go to the latest booking
        score = 2 * sum(1 for t in _name_tokens(r["customer"] or "") if t in low)
        if (r["origin"], r["destination"]) in routes:
            score += 2
        elif r["destination"] and re.search(rf"\b{r['destination']}\b", text):
            score += 1
        if score and (best is None or score > best[0]):
            best = (score, r)
    return best[1] if best and best[0] >= 2 else None


def _de_amount(minor: int) -> str:
    return (
        f"{Decimal(minor) / 100:,.2f}".replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


def answer_text(r: dict[str, Any], lang: str) -> str:
    req = r.get("request") or {}
    ref = r["booking_reference"] or r["team_reference"]
    route = (
        f"{r['origin']} – {r['destination']}"
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
            f"- Status: {status}",
            f"- Strecke: {route} mit {r['airline']}",
        ]
        if weight:
            lines.append(f"- Gewicht: {float(weight):g} kg")
        if pickup:
            lines.append(f"- Abholung in {pickup}, Lkw zum Abflughafen {r['origin']}")
        if ready:
            lines.append(f"- Abholbereit ab: {ready}")
        if days is not None:
            lines.append(f"- Laufzeit: ca. {days} Tage ab Abflug")
        if r.get("price_minor"):
            lines.append(
                f"- Preis: {_de_amount(r['price_minor'])} {r['currency']} all-in"
            )
        lines += ["", "Viele Grüße", "Austrian Power Forwarding"]
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
        f"- Status: {status}",
        f"- Route: {route} with {r['airline']}",
    ]
    if weight:
        lines.append(f"- Weight: {float(weight):g} kg")
    if pickup:
        lines.append(f"- Pickup at {pickup}, trucked to {r['origin']} airport")
    if ready:
        lines.append(f"- Ready for pickup: {ready}")
    if days is not None:
        lines.append(f"- Transit time: about {days} days from departure")
    if r.get("price_minor"):
        lines.append(
            f"- Price: {Decimal(r['price_minor']) / 100:,.2f} {r['currency']} all-in"
        )
    lines += ["", "Best regards,", "Austrian Power Forwarding"]
    return "\n".join(lines)


def _not_found_text(lang: str) -> str:
    if lang == "de":
        return (
            "Guten Tag,\n\nzu Ihrer Anfrage finden wir leider keine gebuchte Sendung. "
            "Könnten Sie uns die Buchungsnummer (BK-…) oder unsere Referenz (AP-…) "
            "nennen? Dann melden wir uns sofort.\n\nViele Grüße\nAustrian Power Forwarding"
        )
    return (
        "Hello,\n\nwe couldn't find a booked shipment for your request. Could you send us "
        "the booking reference (BK-…) or our reference (AP-…)? We'll get back to you right "
        "away.\n\nBest regards,\nAustrian Power Forwarding"
    )


def _reply(mail: Mail, body: str, session_id: int | None, category: str) -> None:
    if session_id is not None and (
        db.is_paused(session_id) or db.is_manual(session_id)
    ):
        db.emit(
            db.Ev.WARN,
            "Tracking-Antwort nicht gesendet: Session pausiert/manuell",
            session_id,
        )
        return
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


def handle(
    mail: Mail, session_id: int | None, booked_session: int | None = None
) -> None:
    """Answer one tracking question. `booked_session` is set when a customer asks in the
    thread of their own booking. Never raises (runs in a worker thread)."""
    try:
        text = f"{mail.subject}\n{mail.body}"
        lang = "de" if _DE.search(text) else "en"
        records = db.tracking_records()
        if booked_session is not None:
            rec = next((r for r in records if r["session_id"] == booked_session), None)
        else:
            rec = find(text, records)
        if rec is None:
            log.warning("tracking mail %s: no booking matched", mail.id)
            db.emit(
                db.Ev.WARN,
                f"Tracking-Frage {mail.id}: keine Buchung gefunden",
                session_id,
                level="warn",
            )
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
