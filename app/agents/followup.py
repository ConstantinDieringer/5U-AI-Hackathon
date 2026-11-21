"""Follow-up agent: a quoted, unbooked session where the customer has gone quiet gets a short
nudge in the thread, asking whether we should book.

Why: rounds 54/55 were accepted on price but never booked. The customer answered "I'll look
at it and get back to you" and then stayed silent; only a follow-up from us moves it on.

Rules (no LLM, no reviewer call: zero gateway cost per nudge):
- session is `quoted`, has a quote and no booking, not paused / manual
- the last mail between us and the customer is ours (quote, clarification, earlier nudge),
  or a customer reply that is neither yes nor no ("I'll get back to you")
- that last mail is at least FOLLOWUP_AFTER_S old (default 30 s)
- at most FOLLOWUP_MAX nudges (default 3), only within FOLLOWUP_WINDOW_S after the quote
  (default 900 s = portal rate_id validity)
- an explicit customer decline after the quote stops all nudges
- the nudge never restates or changes the price: only the first quote in a thread counts
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from app import db, hackloop
from app.config import settings
from app.mail.router import address
from app.mail.style import SIGNATURE

log = logging.getLogger(__name__)

CATEGORY = "followup"

_EN = (
    (
        "just checking in on the offer I sent over. Shall I go ahead and book it for you?\n\n"
        "A short yes is enough and I will take care of the rest."
    ),
    (
        "a quick follow up on our offer. If it works for you, just reply with a yes and I will "
        "book it right away.\n\nIf anything needs changing, tell me what and I will look into it."
    ),
    (
        "one last nudge on the offer. Rates can move quickly, so if you would like to go "
        "ahead, a quick yes is all I need."
    ),
)
_DE = (
    (
        "ich wollte kurz nachfragen, ob das Angebot für Sie passt. Soll ich die Sendung für Sie "
        "buchen?\n\nEin kurzes Ja genügt, um den Rest kümmere ich mich."
    ),
    (
        "kurze Nachfrage zu unserem Angebot. Wenn es für Sie passt, antworten Sie einfach mit "
        "Ja und ich buche sofort.\n\nFalls etwas angepasst werden soll, sagen Sie mir einfach was."
    ),
    (
        "ein letzter kurzer Hinweis zu unserem Angebot. Raten können sich schnell ändern. Wenn "
        "Sie buchen möchten, reicht mir ein kurzes Ja."
    ),
)


def text(lang: str, name: str | None, n: int) -> str:
    """The n-th nudge (0-based) in the customer's language."""
    de = lang == "de"
    body = (_DE if de else _EN)[min(n, 2)]
    if de:
        greeting = f"Hallo {name}," if name else "Guten Tag,"
    else:
        greeting = f"Hi {name}," if name else "Hello,"
    return f"{greeting}\n\n{body}\n\n{SIGNATURE}\n"


def _age_s(ts: str, now: datetime) -> float:
    t = datetime.fromisoformat(str(ts))
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return (now - t).total_seconds()


def _conversation(s: dict[str, Any]) -> list[dict[str, Any]]:
    customer = address(str(s.get("customer") or ""))
    return [
        m
        for m in s.get("mails", [])
        if (m["direction"] == "in" and address(str(m["sender"] or "")) == customer)
        or (m["direction"] == "out" and address(str(m["recipient"] or "")) == customer)
    ]


def due(s: dict[str, Any], now: datetime | None = None) -> int | None:
    """Index of the nudge to send now, or None. Pure: reads the session only."""
    from app.agents import booking

    now = now or datetime.now(UTC)
    if s.get("status") != "quoted" or not s.get("quotes") or s.get("bookings"):
        return None
    quote = s["quotes"][0]
    if _age_s(quote["sent_at"], now) > settings.followup_window_s:
        return None
    conv = _conversation(s)
    if not conv:
        return None
    quote_mail = int(quote.get("mail_id") or 0)
    after_quote = [m for m in conv if int(m["id"]) >= quote_mail] if quote_mail else conv
    replies = [m for m in after_quote if m["direction"] == "in"]
    if any(booking._DECLINE.search(booking.own_text(str(m["body"] or ""))) for m in replies):
        return None  # explicit decline: never nudge again ("not sure yet" is no decline)
    sent = sum(1 for m in conv if m["direction"] == "out" and m.get("category") == CATEGORY)
    if sent >= settings.followup_max:
        return None
    last = conv[-1]
    if last["direction"] == "in" and booking.is_acceptance(str(last["body"] or "")) is True:
        return None  # the booking agent handles it
    if _age_s(last["received_at"], now) < settings.followup_after_s:
        return None
    return sent


def _send(sid: int) -> None:
    from app.agents import booking

    with booking._lock(sid):  # never race the booking agent on the same thread
        s = db.get_session(sid)
        if not s or db.is_paused(sid) or db.is_manual(sid):
            return
        n = due(s)
        if n is None:
            return
        request = s.get("request") or {}
        conv = _conversation(s)
        last_in = next((m for m in reversed(conv) if m["direction"] == "in"), None)
        lang = (
            booking.language(str(last_in["body"] or ""))
            if last_in
            else ("de" if str(request.get("language") or "").startswith("de") else "en")
        )
        body = text(lang, request.get("customer_name"), n)
        to = address(str(s["customer"]))
        reply_to = int(last_in["id"]) if last_in else sid
        subject = str(s.get("subject") or "")
        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        sent = hackloop.send_customer(
            to, subject, body, in_reply_to=reply_to, session_id=sid, review=False
        )
        db.record_outgoing(
            mail_id=int(sent["id"]),
            recipient=to,
            subject=subject,
            body=sent["body"],
            category=CATEGORY,
            session_id=sid,
            sender=settings.team_mailbox,
            in_reply_to=reply_to,
            raw=sent,
        )
        db.emit(db.Ev.STATUS, f"Follow-up {n + 1} an Kunden gesendet (keine Antwort)", sid)


def tick() -> None:
    for row in db.list_sessions(status="quoted"):
        sid = int(row["id"])
        try:
            _send(sid)
        except hackloop.CustomerSendBlocked:
            pass
        except Exception as e:
            log.exception("session %s: follow-up failed", sid)
            db.emit(
                db.Ev.ERROR,
                f"Follow-up fehlgeschlagen: {type(e).__name__}: {e}"[:300],
                sid,
                level="error",
            )


def run(stop: threading.Event) -> None:
    if not settings.followup_enabled:
        log.info("FOLLOWUP_ENABLED=false: no follow-ups")
        return
    while not stop.wait(settings.followup_check_every_s):
        try:
            tick()
        except Exception:
            log.exception("follow-up loop")
