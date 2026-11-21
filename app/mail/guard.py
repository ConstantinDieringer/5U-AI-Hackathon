"""Mail guard: cheap, deterministic limits around the agents (no LLM, no I/O besides SQLite).

Inbound (`inbound`, called in poller.handle after the phishing gate and storage):
- empty mail: no text (body, or subject for a new request) and no attachment
- automatic mail: out-of-office, bounces, mailer-daemon/no-reply senders
- RFQ flood: more than INBOUND_FLOOD_MAX new requests from one sender in INBOUND_FLOOD_S
  (5U sends one RFQ every 6 minutes in total)
Such a mail is stored and shown, but no agent runs: no LLM extraction, no question, no reply.

Outbound (`check_outbound`, called in hackloop.send before the HTTP call):
- burst: at most OUTBOUND_BURST_MAX mails to one customer address in OUTBOUND_BURST_S
- thread: at most OUTBOUND_SESSION_MAX mails to one address in one session
- duplicate: the same text a third time to one address in one session (OUTBOUND_DUPLICATE_S)
A refused send raises `OutboundLoopBlocked` and emits one error event (Slack #sos) per session
and address. Carrier and desk addresses are exempt: we never answer their replies, so they
cannot drive a loop, and bid fan-out legitimately sends many similar mails at once.

Only applies to RFQ/customer mail; carrier bids, tariffs, tracking and desk mail pass through.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from app import db
from app.config import settings
from app.mail.content import current_message
from app.mail.router import address
from app.models import Category, Mail
from app.pricing.reference import carrier_by_email

log = logging.getLogger(__name__)

_FLAGS = re.IGNORECASE
_AUTO_SUBJECT = re.compile(
    r"^\s*(?:(?:re|aw|fw|fwd|wg)\s*:\s*)*(?:"
    r"auto(?:matic|matische)?[ -]?(?:reply|response|antwort)|autoreply|"
    r"out of (?:the )?office|ooo\b|abwesenheit|abwesend|vacation|urlaub|"
    r"undeliverable|undelivered|unzustellbar|non[- ]?delivery|returned mail|"
    r"delivery (?:status notification|failure|has failed|delayed)|"
    r"mail delivery (?:failed|failure|system|subsystem)|failure notice|"
    r"zustellung fehlgeschlagen|nicht zustellbar|"
    r"r[ée]ponse automatique|risposta automatica|respuesta autom[áa]tica)",
    _FLAGS,
)
_AUTO_BODY = re.compile(
    r"\bthis is an automati(?:c|cally generated) (?:reply|response|message)\b|"
    r"\bi am (?:currently )?out of (?:the )?office\b|"
    r"\bi(?:'m| am) (?:currently )?(?:away|on (?:vacation|holiday|leave))\b|"
    r"\bdiese nachricht wurde automatisch\b|\bich bin (?:derzeit |zurzeit )?nicht im b[üu]ro\b|"
    r"\bdelivery to the following recipients? failed\b|\bmessage could not be delivered\b",
    _FLAGS,
)
_AUTO_SENDER = re.compile(
    r"^(?:mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounces?|autoreply|"
    r"auto-?responder)(?:[+.\-]|@)",
    _FLAGS,
)
_CONTENT = re.compile(r"[^\W_]", re.UNICODE)  # a letter or digit in any script


class OutboundLoopBlocked(RuntimeError):
    """The mail guard refused a send that looks like a loop; a person decides."""


def _has_content(text: str) -> bool:
    return bool(_CONTENT.search(text or ""))


_FORWARD = re.compile(
    r"forwarded message|weitergeleitete nachricht|begin forwarded message|^\s*(?:fw|fwd|wg)\s*:",
    _FLAGS | re.MULTILINE,
)


def is_empty(mail: Mail, category: Category) -> bool:
    """No usable text and no attachment. A new request counts its subject and any forwarded
    text (pure forwarding requests carry their only specification there). A reply counts only
    the customer's own new words: quoting our mail back is no answer."""
    if mail.attachments:
        return False
    body = mail.body or ""
    if category == Category.RFQ:
        return not (_has_content(body) or _has_content(mail.subject))
    if _has_content(current_message(body).text):
        return False
    return not (_FORWARD.search(body) and _has_content(body))


def is_automatic(mail: Mail) -> str | None:
    """Reason when the mail was written by a machine (out-of-office, bounce), else None."""
    sender = address(mail.sender)
    if _AUTO_SENDER.match(sender):
        return f"automatischer Absender {sender}"
    if m := _AUTO_SUBJECT.search(mail.subject or ""):
        return f"automatische Antwort ('{m.group(0).strip()}')"
    if m := _AUTO_BODY.search((mail.body or "")[:2000]):
        return f"automatische Antwort ('{m.group(0)}')"
    return None


def _since(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def rfq_flood(mail: Mail) -> int:
    """New request threads from this sender within INBOUND_FLOOD_S, this one included."""
    row = (
        db.conn()
        .execute(
            "SELECT COUNT(*) FROM mails WHERE direction='in' AND category=? AND lower(sender)=?"
            " AND (id=? OR julianday(received_at) >= julianday(?))",
            (Category.RFQ.value, address(mail.sender), mail.id, _since(settings.inbound_flood_s)),
        )
        .fetchone()
    )
    return int(row[0]) if row else 0


def precheck(mail: Mail, category: Category) -> str | None:
    """Pure part of the inbound check (no database): automatic or empty mail. Runs before the
    phishing gate so such mail never costs a gateway call."""
    if not settings.mail_guard_enabled or category not in (Category.RFQ, Category.CUSTOMER):
        return None
    if auto := is_automatic(mail):
        return auto
    if is_empty(mail, category):
        return "leere Mail (kein Text, kein Anhang)"
    return None


def inbound(
    mail: Mail, category: Category, session_id: int | None, early: str | None = None
) -> str | None:
    """Reason to keep this mail away from the agents, or None to process it normally.
    `early` is the result of `precheck`, when the caller already ran it."""
    if not settings.mail_guard_enabled or category not in (Category.RFQ, Category.CUSTOMER):
        return None
    reason = early or precheck(mail, category)
    if reason is None and category == Category.RFQ:
        count = rfq_flood(mail)
        if count > settings.inbound_flood_max:
            reason = (
                f"{count} neue Anfragen von {address(mail.sender)} in "
                f"{settings.inbound_flood_s / 60:g} min (Grenze {settings.inbound_flood_max})"
            )
    if reason is None:
        return None
    log.warning("mail %s from %s held by the mail guard: %s", mail.id, mail.sender, reason)
    db.emit(
        "guard",
        f"Mail {mail.id} nicht verarbeitet: {reason}. Keine Antwort, kein LLM-Aufruf.",
        session_id,
        level="warn",
        mail_id=mail.id,
        reason=reason,
    )
    if category == Category.RFQ and session_id is not None:
        db.set_status(session_id, "lost", f"Mail-Guard: {reason}"[:120])
    return reason


# ---------------------------------------------------------------- outbound


_lock = threading.Lock()
_recent: deque[tuple[float, str, int | None, str]] = deque(maxlen=2000)  # this process


def _fingerprint(body: str) -> str:
    text = re.sub(r"\s+", " ", (body or "").strip().lower())
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _guarded(recipient: str) -> bool:
    if recipient in (settings.team_mailbox, settings.ratedesk_address):
        return False
    return carrier_by_email(recipient) is None


def _session_of(in_reply_to: int | None) -> int | None:
    if in_reply_to is None:
        return None
    row = db.conn().execute("SELECT session_id FROM mails WHERE id = ?", (in_reply_to,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _sent(recipient: str, seconds: float) -> list[tuple[float, int | None, str]]:
    """(time, session, fingerprint) of our mails to `recipient` in the last `seconds`."""
    rows = (
        db.conn()
        .execute(
            "SELECT id, session_id, body, received_at FROM mails WHERE direction='out'"
            " AND lower(recipient)=? AND julianday(received_at) >= julianday(?)",
            (recipient, _since(seconds)),
        )
        .fetchall()
    )
    out = []
    for row in rows:
        try:
            at = datetime.fromisoformat(str(row[3])).timestamp()
        except ValueError:
            at = time.time()
        out.append((at, row[1], _fingerprint(row[2] or "")))
    cutoff = time.time() - seconds
    with _lock:  # sends of this process not (yet) stored by their caller
        mine = [(t, s, f) for t, r, s, f in _recent if r == recipient and t >= cutoff]
    stored = {(s, f) for _, s, f in out}
    out += [m for m in mine if (m[1], m[2]) not in stored]
    return out


def _session_total(recipient: str, session_id: int) -> int:
    row = (
        db.conn()
        .execute(
            "SELECT COUNT(*) FROM mails WHERE direction='out' AND session_id=?"
            " AND lower(recipient)=?",
            (session_id, recipient),
        )
        .fetchone()
    )
    return int(row[0]) if row else 0


def check_outbound(to: str, body: str, in_reply_to: int | None) -> None:
    """Raise OutboundLoopBlocked when this send would continue a loop. A failing check (e.g.
    database trouble) never stops a send: a lost quote costs more than one extra mail."""
    try:
        reason, recipient, session_id = _outbound_reason(to, body, in_reply_to)
    except Exception:
        log.exception("mail guard check failed for a send to %s: sending", to)
        return
    if reason is None:
        return
    _alert(recipient, session_id, reason)
    raise OutboundLoopBlocked(reason)


def _outbound_reason(
    to: str, body: str, in_reply_to: int | None
) -> tuple[str | None, str, int | None]:
    recipient = address(to)
    if not settings.mail_guard_enabled or not _guarded(recipient):
        return None, recipient, None
    session_id = _session_of(in_reply_to)
    window = max(settings.outbound_burst_s, settings.outbound_duplicate_s)
    recent = _sent(recipient, window)
    now = time.time()
    burst = sum(1 for t, _, _ in recent if t >= now - settings.outbound_burst_s)
    reason: str | None = None
    if burst >= settings.outbound_burst_max:
        reason = (
            f"{burst} Mails an {recipient} in {settings.outbound_burst_s:g} s "
            f"(Grenze {settings.outbound_burst_max})"
        )
    elif session_id is not None:
        fp = _fingerprint(body)
        repeats = sum(1 for _, s, f in recent if s == session_id and f == fp)
        if repeats >= 2:  # one repetition is allowed (a question asked again), a third is a loop
            reason = f"gleicher Text an {recipient} in diesem Thread schon {repeats}x gesendet"
        elif (total := _session_total(recipient, session_id)) >= settings.outbound_session_max:
            reason = (
                f"{total} Mails an {recipient} in diesem Thread "
                f"(Grenze {settings.outbound_session_max})"
            )
    return reason, recipient, session_id


def record_sent(to: str, body: str, in_reply_to: int | None) -> None:
    """Remember a successful send until its caller has stored it."""
    recipient = address(to)
    if not _guarded(recipient):
        return
    try:
        session_id = _session_of(in_reply_to)
    except Exception:
        log.exception("mail guard: session of mail %s unknown", in_reply_to)
        session_id = None
    with _lock:
        _recent.append((time.time(), recipient, session_id, _fingerprint(body)))


def _alert(recipient: str, session_id: int | None, reason: str) -> None:
    key = f"guard.loop.{session_id}.{recipient}"
    log.error("mail guard refused a send to %s (session %s): %s", recipient, session_id, reason)
    try:
        if db.kv_get(key):
            return
        db.kv_set(key, str(int(time.time())))
    except Exception:
        log.exception("mail guard: kv unavailable")
    db.emit(
        "guard",
        f"Mail-Schleife gestoppt, nichts gesendet: {reason}. Bitte Session prüfen.",
        session_id,
        level="error",
        recipient=recipient,
        reason=reason,
    )


def _reset_for_tests() -> None:
    with _lock:
        _recent.clear()
