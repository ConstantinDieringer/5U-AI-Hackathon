"""Poll loop: fetch new mail, route it, store it once, download attachments.

Guarantees
- Each mail is queued durably by exact ID, shared with signed webhook deliveries.
- The cursor advances only after enqueue commits, independently of agent processing.
- The poller only enqueues. One consumer (`webhook.run`) processes the queue in mail-id
  order, so a reply is never handled before its root mail is stored.
- Carrier bids run in a bounded pool so a slow bid document never stalls intake.
- An error in one mail is logged (and shown in the UI) and the loop goes on.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import db, hackloop
from app.config import settings
from app.mail import address_changes, guard, parse, rate_limit_watch, router, screening, store
from app.models import Category, Mail

log = logging.getLogger(__name__)

POLL_LIMIT = 200
BID_WORKERS = 4  # carrier bids handled in parallel; the rest wait in the pool's queue
_bid_pool = ThreadPoolExecutor(max_workers=BID_WORKERS, thread_name_prefix="bid")
MAX_MAIL_ATTACHMENT_BYTES = 20_000_000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _parent(in_reply_to: int | None) -> Mail | None:
    """The mail we answer to (any mail we sent or received), or None if unknown. Our own DB
    first: every mail we sent or received is stored, so most lookups need no API call."""
    if in_reply_to is None:
        return None
    row = db.mail_header(in_reply_to)
    if row is not None:
        try:
            raw = json.loads(row["raw_json"]) if row.get("raw_json") else None
            if isinstance(raw, dict) and raw.get("id") == in_reply_to:
                return parse.mail_from_api(raw)
        except (ValueError, parse.MailFormatError):
            pass
        return Mail(
            id=row["id"],
            thread_root=row["id"],
            # outgoing mails are often stored without a sender: they are ours
            sender=row["sender"] or (settings.team_mailbox if row["direction"] == "out" else ""),
            to=row["recipient"] or "",
            subject=row["subject"] or "",
            body="",
            received_at=row["received_at"] or "",
        )
    try:
        return parse.mail_from_api(hackloop.get_message(in_reply_to))
    except (hackloop.HackloopError, parse.MailFormatError) as e:
        log.warning("parent mail %s not readable (%s); routing by sender only", in_reply_to, e)
        return None


def _safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name or "").name).strip("._")
    return cleaned[:120] or "attachment"


def download_attachments(mail: Mail) -> list[Path]:
    """Save a bounded set of attachments to data/inbox/<mail_id>/..."""
    if len(mail.attachments) > 12:
        raise ValueError("mail exceeds 12 attachments")
    target = settings.inbox_dir / str(mail.id)
    paths: list[Path] = []
    pending: list[tuple[Path, bytes]] = []
    total_bytes = 0
    for att in mail.attachments:
        att_id = int(att["id"])
        path = target / f"{att_id}-{_safe_filename(str(att.get('filename', '')))}"
        if not path.exists():
            content = hackloop.get_attachment(mail.id, att_id)
            total_bytes += len(content)
            if total_bytes > MAX_MAIL_ATTACHMENT_BYTES:
                raise ValueError("mail attachments exceed 20 MB total")
            pending.append((path, content))
        else:
            total_bytes += path.stat().st_size
            if total_bytes > MAX_MAIL_ATTACHMENT_BYTES:
                raise ValueError("mail attachments exceed 20 MB total")
        paths.append(path)
    if pending:
        target.mkdir(parents=True, exist_ok=True)
        for path, content in pending:
            path.write_bytes(content)
    return paths


def handle(raw: dict, *, recover: bool = False) -> None:
    """Route, screen, store and (if new) process one mail from the poll response. Every
    delivery path (webhook, poll, retry after restart) comes through here, so the phishing
    gate runs before anything is queued, downloaded, stored as a tariff or answered."""
    mail = parse.mail_from_api(raw)
    in_reply_to = parse.in_reply_to(raw)
    category, rule = router.classify(mail, _parent(in_reply_to))
    session_hint = None
    if category == Category.RFQ and in_reply_to is None and not mail.references:
        # 5U's customer agent sometimes replies without headers: same sender, "Re:" subject
        session_hint = db.session_for_headerless_reply(mail.sender, mail.subject)
        if session_hint is not None:
            category, rule = Category.CUSTOMER, f"header-less reply to session {session_hint}"
        else:
            # a booking approval sent as a new mail to the customer's open quote (case 6)
            session_hint = db.session_for_unthreaded_acceptance(
                mail.sender, mail.subject, mail.body
            )
            if session_hint is not None:
                category, rule = Category.CUSTOMER, f"new-mail approval for {session_hint}"
    try:  # empty or automatic mail: held below, never worth a phishing-gate LLM call
        early = guard.precheck(mail, category)
    except Exception:
        log.exception("mail guard precheck failed on mail %s", mail.id)
        early = None
    try:
        check = None if early else screening.screen(mail, category)
    except Exception:
        log.exception("phishing gate failed on mail %s: treated as not screened", mail.id)
        check = None
    if check is not None and check.blocked:
        _quarantine(mail, category, in_reply_to, check)
        return
    if category == Category.TARIFF:
        from app.agents import tariff

        tariff.enqueue(mail)
    try:  # before storing: a mail must not make its own sender a verified customer
        verified = address_changes.verified_addresses()
    except Exception:
        log.exception("verified addresses unavailable on mail %s", mail.id)
        verified = frozenset()
    saved = store.save_mail(mail, category, in_reply_to, session_hint)
    if not saved.is_new and not recover:
        log.info("mail %s already stored, skipped", mail.id)
        return
    if saved.is_new and check is not None and check.verdict is not screening.Verdict.GENUINE:
        _flag(mail, check, saved.session_id)
    if saved.is_new and check is not None and check.verdict is not screening.Verdict.PHISHING:
        try:  # a verified party announcing its new address: the new one is verified too
            address_changes.record(mail, verified, saved.session_id)
        except Exception:
            log.exception("address change check failed on mail %s", mail.id)
    db.delivery_state(mail.id, "stored")
    try:
        held = guard.inbound(mail, category, saved.session_id, early)
    except Exception:
        log.exception("mail guard failed on mail %s: processed normally", mail.id)
        held = None
    if held is not None:
        db.delivery_state(mail.id, "done")
        return
    _deliver(mail, category, rule, saved.session_id)


def _deliver(mail: Mail, category: Category, rule: str, session_id: int | None) -> None:
    files = [] if category == Category.TARIFF else download_attachments(mail)
    note = f", {len(files)} attachment(s) saved" if files else ""
    log.info("mail %s from %s -> %s (%s)%s", mail.id, mail.sender, category.value, rule, note)
    store.emit("routed", f"Routed as {category.value} ({rule}){note}", session_id)
    db.delivery_state(mail.id, "dispatching")
    dispatch(mail, category, session_id)


def _quarantine(
    mail: Mail, category: Category, in_reply_to: int | None, check: screening.Screening
) -> None:
    """Store only: no attachment download, no tariff job, no agent, no reply."""
    saved = store.save_mail(mail, Category.PHISHING, in_reply_to)
    if not saved.is_new:
        return
    log.warning("mail %s from %s quarantined: %s", mail.id, mail.sender, screening.summary(check))
    db.emit(
        "phishing",
        f"Phishing-Verdacht, keine Antwort: {mail.sender} – {mail.subject[:80]}. "
        f"Gründe: {'; '.join(check.reasons())}"[:900],
        saved.session_id,
        level="error",
        mail_id=mail.id,
        routed_as=category.value,
        layer=check.layer,
        score=check.score,
    )


def _flag(mail: Mail, check: screening.Screening, session_id: int | None) -> None:
    """Unsure (or phishing in shadow mode): the normal flow goes on, a person can look."""
    shadow = check.verdict is screening.Verdict.PHISHING
    db.emit(
        "screening",
        ("Phishing-Verdacht (nur protokolliert)" if shadow else "Absender unklar")
        + f", normale Verarbeitung läuft: {screening.summary(check)}",
        session_id,
        level="warn",
        mail_id=mail.id,
        verdict=check.verdict.value,
        layer=check.layer,
    )


def release(mail_id: int) -> dict[str, object]:
    """A person released a quarantined mail: run the normal flow it was kept from."""
    if not settings.agents_enabled:
        raise RuntimeError("agents are disabled: nothing may be processed or sent")
    released = db.release_quarantined(mail_id)
    raw = released["raw"]
    if not isinstance(raw, dict):
        raise TypeError(f"mail {mail_id} has no stored API copy to process")
    mail = parse.mail_from_api(raw)
    category = Category(str(released["category"]))
    session_id = released["session_id"]
    if category == Category.TARIFF:
        from app.agents import tariff

        tariff.enqueue(mail)
    _deliver(mail, category, "released by a person", session_id)
    db.delivery_state(mail.id, "done")
    return {"mail_id": mail_id, "category": category.value, "session_id": session_id}


def dispatch(mail: Mail, category: Category, session_id: int | None) -> None:
    """Hand a new mail to its agent. RFQs run in their own thread so polling goes on (bid
    replies for a waiting RFQ arrive through this same loop)."""
    if category == Category.TRACKING and session_id is not None:
        from app.agents import customer_reply

        if (db.get_session(session_id) or {}).get("kind") == "rfq" and customer_reply.enqueue(
            mail, session_id
        ):
            return
    if category == Category.RFQ and session_id is not None:
        from app.agents import rfq

        threading.Thread(
            target=rfq.handle_rfq,
            args=(mail, session_id),
            name=f"rfq-{session_id}",
            daemon=True,
        ).start()
    elif category == Category.TRACKING:
        from app.agents import clarification, rescue, tracking

        def tracking_reply():
            if session_id is not None and rescue.is_owner(session_id):
                rescue.take_over(mail, session_id)
                return
            pending = db.validation_get(session_id) if session_id is not None else None
            workflow = (pending or {}).get("draft", {}).get("evidence", {}).get("workflow")
            if workflow == "tracking" and clarification.handle_reply(mail, session_id):
                return
            tracking.handle(mail, session_id)

        threading.Thread(
            target=tracking_reply,
            name=f"track-{mail.id}",
            daemon=True,
        ).start()
    elif category == Category.CUSTOMER and session_id is not None:
        from app.agents import booking, clarification, customer_reply, rescue

        if customer_reply.enqueue(mail, session_id):
            return

        def customer_reply():
            if rescue.is_owner(session_id):
                rescue.take_over(mail, session_id)
                return
            if not clarification.handle_reply(mail, session_id):
                booking.handle_reply(mail, session_id)

        threading.Thread(
            target=customer_reply,
            name=f"book-{session_id}",
            daemon=True,
        ).start()
    elif category == Category.CARRIER_BID:
        from app.agents import bids, booking

        if booking.on_airline_mail(mail):  # "Booking BK-… confirmed" is not a bid
            return
        _bid_pool.submit(_bid, bids.on_bid_mail, mail, session_id)


def _bid(handler, mail: Mail, session_id: int | None) -> None:
    """Pool task: a failing bid is logged and shown, never lost silently."""
    try:
        handler(mail, session_id)
    except Exception as exc:
        log.exception("bid mail %s failed", mail.id)
        db.emit(
            "error",
            f"Gebot {mail.id} nicht verarbeitet: {type(exc).__name__}",
            session_id,
            level="error",
            mail_id=mail.id,
        )


RATE_LIMIT_RETRY_S = 6.0  # just over the mailbox limit of one poll per 5 s


def poll_once(since_id: int) -> tuple[int, int]:
    """One poll call: enqueue only, `webhook.run` processes. Returns (new since_id, number of
    mails in the batch)."""
    from app.mail import webhook

    batch = hackloop.poll(since_id, limit=POLL_LIMIT)
    messages = parse.messages(batch)
    for raw in messages:
        mail_id = parse.message_id(raw)
        webhook.enqueue(raw, "poll")
        since_id = max(since_id, mail_id)
        store.set_since_id(since_id)
    next_id = parse.next_since_id(batch)
    if next_id is not None and next_id > since_id:
        since_id = next_id
        store.set_since_id(since_id)
    return since_id, len(messages)


def run(stop: threading.Event | None = None) -> None:
    """Poll forever (until `stop` is set). Never raises."""
    stop = stop or threading.Event()
    store.init()
    since_id = store.get_since_id()
    log.info("poller started at since_id=%s, every %.1fs", since_id, settings.poll_interval_s)
    backoff = 0.0
    while not stop.is_set():
        from app.mail.webhook import signing_secret

        delay = max(
            settings.poll_interval_s, settings.webhook_poll_interval_s if signing_secret() else 5.0
        )
        try:
            since_id, _ = poll_once(since_id)
            backoff = 0
        except hackloop.HackloopError as exc:
            if exc.status == 429:
                # Rate limit (one poll per 5 s per team key): another reader of our mailbox
                # used the slot. Try again soon and never grow the wait: an RFQ lives 6 min,
                # and a doubling backoff slept through rounds 47 and 48.
                backoff = 0
                delay = max(delay, getattr(exc, "retry_after", RATE_LIMIT_RETRY_S))
                rate_limit_watch.record()
            else:
                backoff = min(300, max(30, backoff * 2))
                delay = max(delay, backoff, getattr(exc, "retry_after", 0))
            log.warning("mailbox poll failed (%s), next attempt in %.1fs", exc.status, delay)
        except Exception:
            log.exception("poll failed")
            delay = max(delay, 30)
        stop.wait(delay)  # full batches also respect the mailbox rate limit
