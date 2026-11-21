"""Poll loop: fetch new mail, route it, store it once, download attachments.

Guarantees
- Each mail is queued durably by exact ID, shared with signed webhook deliveries.
- The cursor advances only after enqueue commits, independently of agent processing.
- An error in one mail is logged (and shown in the UI) and the loop goes on.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from app import db, hackloop
from app.config import settings
from app.mail import parse, router, store
from app.models import Category, Mail

log = logging.getLogger(__name__)

POLL_LIMIT = 200
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _parent(in_reply_to: int | None) -> Mail | None:
    """The mail we answer to (any mail we sent or received), or None if unknown."""
    if in_reply_to is None:
        return None
    try:
        return parse.mail_from_api(hackloop.get_message(in_reply_to))
    except (hackloop.HackloopError, parse.MailFormatError) as e:
        log.warning("parent mail %s not readable (%s); routing by sender only", in_reply_to, e)
        return None


def _safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name or "").name).strip("._")
    return cleaned[:120] or "attachment"


def download_attachments(mail: Mail) -> list[Path]:
    """Save every attachment to data/inbox/<mail_id>/<attachment_id>-<filename>."""
    target = settings.inbox_dir / str(mail.id)
    paths: list[Path] = []
    for att in mail.attachments:
        att_id = int(att["id"])
        path = target / f"{att_id}-{_safe_filename(str(att.get('filename', '')))}"
        if not path.exists():
            target.mkdir(parents=True, exist_ok=True)
            path.write_bytes(hackloop.get_attachment(mail.id, att_id))
        paths.append(path)
    return paths


def handle(raw: dict, *, recover: bool = False) -> None:
    """Route, store and (if new) process one mail from the poll response."""
    mail = parse.mail_from_api(raw)
    in_reply_to = parse.in_reply_to(raw)
    category, rule = router.classify(mail, _parent(in_reply_to))
    if category == Category.TARIFF:
        from app.agents import tariff

        tariff.enqueue(mail)
    saved = store.save_mail(mail, category, in_reply_to)
    if not saved.is_new and not recover:
        log.info("mail %s already stored, skipped", mail.id)
        return
    db.delivery_state(mail.id, "stored")
    files = [] if category == Category.TARIFF else download_attachments(mail)
    note = f", {len(files)} attachment(s) saved" if files else ""
    log.info("mail %s from %s -> %s (%s)%s", mail.id, mail.sender, category.value, rule, note)
    store.emit("routed", f"Routed as {category.value} ({rule}){note}", saved.session_id)
    db.delivery_state(mail.id, "dispatching")
    dispatch(mail, category, saved.session_id)


def dispatch(mail: Mail, category: Category, session_id: int | None) -> None:
    """Hand a new mail to its agent. RFQs run in their own thread so polling goes on (bid
    replies for a waiting RFQ arrive through this same loop)."""
    if category == Category.RFQ and session_id is not None:
        from app.agents import rfq

        threading.Thread(
            target=rfq.handle_rfq,
            args=(mail, session_id),
            name=f"rfq-{session_id}",
            daemon=True,
        ).start()
    elif category == Category.TRACKING:
        from app.agents import tracking

        threading.Thread(
            target=tracking.handle,
            args=(mail, session_id),
            name=f"track-{mail.id}",
            daemon=True,
        ).start()
    elif category == Category.CUSTOMER and session_id is not None:
        from app.agents import booking, clarification

        def customer_reply():
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
        bids.on_bid_mail(mail, session_id)


def poll_once(since_id: int) -> tuple[int, int]:
    """One poll call. Returns (new since_id, number of mails in the batch)."""
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
    # Also drain here for poll-only callers; the concurrent worker uses the same atomic claims.
    for _ in range(POLL_LIMIT):
        if not webhook.process_once():
            break
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
            backoff = min(300, max(30, backoff * 2))
            delay = max(delay, backoff, getattr(exc, "retry_after", 0))
            log.warning("mailbox poll failed (%s), next attempt in %.1fs", exc.status, delay)
        except Exception:
            log.exception("poll failed")
            delay = max(delay, 30)
        stop.wait(delay)  # full batches also respect the mailbox rate limit
