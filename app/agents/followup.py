"""Follow-up agent: when the customer goes quiet on our offer (or on our clarification
question before the offer), a short nudge goes into the thread.

Why: rounds 54/55 were accepted on price but never booked. The customer answered "I'll look
at it and get back to you" and then stayed silent; only a follow-up from us moves it on.

Rules (no LLM, no reviewer call: zero gateway cost per nudge):
- session is `quoted`, has a quote and no booking, not paused / manual
- the last mail between us and the customer is ours (quote, clarification, earlier nudge),
  or a customer reply that only stalls ("I'll get back to you", "not sure yet")
- a customer question or unclear reply we have not answered is never nudged: it stays with
  booking / prebook / thread rescue until one of our mails answers it
- that last mail is at least FOLLOWUP_AFTER_S old (default 5 s, capped at 5 s)
- at most FOLLOWUP_MAX nudges (default 2), only within FOLLOWUP_WINDOW_S after the quote
  (default 900 s = portal rate_id validity)
- an explicit customer decline after the quote stops all nudges
- clarification questions before the quote: same 5 s / FOLLOWUP_MAX rule, only while the RFQ
  deadline has not passed, never once the customer has answered
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
from app.mail.subjects import shipment_subject
from app.quotes import active

log = logging.getLogger(__name__)

CATEGORY = "followup"
CATEGORY_QUESTION = "followup_question"

_EN = (
    (
        "just checking in on the offer I sent over. Shall I go ahead and book it for you?\n\n"
        "Let me know if you would like me to arrange the booking."
    ),
    (
        "a quick follow up on our offer. If it works for you, let me know and I will "
        "arrange the booking.\n\nIf anything needs changing, tell me what and I will look into it."
    ),
    (
        "one last nudge on the offer. Rates can move quickly, so if you would like to go "
        "ahead, let me know and I will arrange the booking."
    ),
)
_DE = (
    "ich wollte kurz nachfragen, ob das Angebot für dich passt. Soll ich die Buchung für dich übernehmen?",
    "kurze Nachfrage zu unserem Angebot. Wenn es für dich passt, sag mir einfach Bescheid und ich kümmere mich um die Buchung. Falls noch eine Frage offen ist, helfe ich dir gerne weiter.",
    "ein letzter kurzer Hinweis zu unserem Angebot. Raten können sich schnell ändern. Wenn du buchen möchtest, sag mir einfach Bescheid.",
)


_Q_EN = (
    (
        "did you get a chance to look at my question below? As soon as I have your answer I "
        "can send you the price."
    ),
    (
        "a quick reminder about my question below. Once I have that detail your offer goes out "
        "right away."
    ),
    (
        "one last nudge on my question below. A short answer is enough and I will price it "
        "straight away."
    ),
)
_Q_DE = (
    (
        "konnten Sie sich meine Frage unten schon ansehen? Sobald ich Ihre Antwort habe, "
        "schicke ich Ihnen den Preis."
    ),
    (
        "kurze Erinnerung an meine Frage unten. Sobald ich die Angabe habe, geht Ihr Angebot "
        "sofort raus."
    ),
    (
        "ein letzter kurzer Hinweis auf meine Frage unten. Eine kurze Antwort genügt und ich "
        "rechne sofort."
    ),
)


def text(lang: str, name: str | None, n: int, kind: str = "offer") -> str:
    """The n-th nudge (0-based) in the customer's language. kind: "offer" or "question"."""
    de = lang == "de"
    if kind == "question":
        body = (_Q_DE if de else _Q_EN)[min(n, 2)]
    else:
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


def due(s: dict[str, Any], now: datetime | None = None) -> tuple[str, int] | None:
    """(kind, index) of the nudge to send now, or None. Pure: reads the session only.
    kind "offer": quoted, customer silent or stalling. kind "question": our clarification
    question before the quote is unanswered (until the RFQ deadline)."""
    now = now or datetime.now(UTC)
    if s.get("case_worker"):
        return None
    workflow = s.get("workflow")
    if workflow and (
        workflow["phase"] != "awaiting_customer"
        or workflow["open_items"]
        or db.reply_has_pending(s["id"])
    ):
        return None
    if s.get("bookings"):
        return None
    if s.get("status") == "quoted" and s.get("quotes"):
        return _due_offer(s, now)
    if s.get("status") in ("new", "pricing") and not s.get("quotes"):
        return _due_question(s, now)
    return None


def _due_offer(s: dict[str, Any], now: datetime) -> tuple[str, int] | None:
    from app.agents import booking
    from app.mail import multilingual

    quote = active(s)
    if quote is None:
        return None
    if _age_s(quote["sent_at"], now) > settings.followup_window_s:
        return None
    conv = _conversation(s)
    if not conv:
        return None
    quote_mail = int(quote.get("mail_id") or 0)
    after_quote = [m for m in conv if int(m["id"]) >= quote_mail] if quote_mail else conv
    replies = [m for m in after_quote if m["direction"] == "in"]
    if any(
        booking._DECLINE.search(
            booking.own_text(multilingual.working_text(m["id"], str(m["body"] or "")))
        )
        for m in replies
    ):
        return None  # explicit decline: never nudge again ("not sure yet" is no decline)
    from app.agents import prebook

    sent = sum(1 for m in conv if m["direction"] == "out" and m.get("category") == CATEGORY)
    # our answers to pre-booking questions share the cap: never more than FOLLOWUP_MAX mails
    answers = sum(
        1 for m in conv if m["direction"] == "out" and m.get("category") == prebook.CATEGORY
    )
    if sent + (0 if s.get("workflow") else answers) >= settings.followup_max:
        return None
    last = conv[-1]
    if last["direction"] == "in":
        kind = booking.classify_reply(
            multilingual.working_text(last["id"], str(last["body"] or ""))
        )
        if kind == "yes":
            return None  # the booking agent handles it
        # "answered" as db.rescue_candidates defines it (one of our mails replies to it);
        # mail order alone is not enough, our mails carry whole-second timestamps
        answered = any(m["direction"] == "out" and m.get("in_reply_to") == last["id"] for m in conv)
        if kind in ("question", "unclear") and not answered:
            # belongs to booking/prebook/rescue: a nudge in reply to it would mark it
            # answered for the rescue supervisor and the question would never get an answer
            return None
    if _age_s(last["received_at"], now) < settings.followup_after_s:
        return None
    return "offer", sent


def _due_question(s: dict[str, Any], now: datetime) -> tuple[str, int] | None:
    v = s.get("validation") or {}
    if v.get("state") != "awaiting_clarification":
        return None
    if s.get("deadline_at") and _age_s(s["deadline_at"], now) >= -5:
        return None  # the RFQ is (almost) expired: a nudge earns nothing
    conv = _conversation(s)
    if not conv or conv[-1]["direction"] != "out":
        return None  # customer answered: the clarification agent is on it
    if not any(m["direction"] == "out" and m.get("category") == "clarification" for m in conv):
        return None
    sent = sum(
        1 for m in conv if m["direction"] == "out" and m.get("category") == CATEGORY_QUESTION
    )
    if sent >= settings.followup_max:
        return None
    if _age_s(conv[-1]["received_at"], now) < settings.followup_after_s:
        return None
    return "question", sent


def _send(sid: int) -> None:
    from app.agents import booking

    with booking._lock(sid):  # never race the booking agent on the same thread
        s = db.get_session(sid)
        if not s or db.is_paused(sid) or db.is_manual(sid):
            return
        found = due(s)
        if found is None:
            return
        kind, n = found
        request = s.get("request") or {}
        conv = _conversation(s)
        last_in = next((m for m in reversed(conv) if m["direction"] == "in"), None)
        lang = (
            booking.language(str(last_in["body"] or ""))
            if last_in
            else (request.get("language") or "en")
        )
        body = text(lang, request.get("customer_name"), n, kind)
        to = address(str(s["customer"]))
        reply_to = int(last_in["id"]) if last_in else sid
        subject = str(s.get("subject") or "")
        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        quotes = s.get("quotes") or []
        if quotes:
            quote = active(s) or quotes[0]
            subject = shipment_subject(quote.get("origin"), quote.get("destination"), lang, subject)
        sent = hackloop.send_customer(
            to, subject, body, in_reply_to=reply_to, session_id=sid, review=False
        )
        db.record_outgoing(
            mail_id=int(sent["id"]),
            recipient=to,
            subject=sent["subject"],
            body=sent["body"],
            category=CATEGORY if kind == "offer" else CATEGORY_QUESTION,
            session_id=sid,
            sender=settings.team_mailbox,
            in_reply_to=reply_to,
            raw=sent,
        )
        db.emit(
            db.Ev.STATUS,
            f"Follow-up {n + 1} an Kunden gesendet ({'Angebot' if kind == 'offer' else 'Rückfrage'} unbeantwortet)",
            sid,
        )


def tick() -> None:
    # ids only: same sessions and order as db.list_sessions, without its display columns
    for sid in [i for st in ("quoted", "new", "pricing") for i in db.session_ids(st)]:
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
