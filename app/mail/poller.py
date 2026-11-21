"""Poll loop: fetch new mail, route it, store it once, download attachments.

Guarantees
- Each mail is processed at most once: it is inserted first and processed only if new.
- `next_since_id` is saved after every mail, so a restart resumes where it stopped.
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
        log.warning(
            "parent mail %s not readable (%s); routing by sender only", in_reply_to, e
        )
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


def handle(raw: dict) -> None:
    """Route, store and (if new) process one mail from the poll response."""
    mail = parse.mail_from_api(raw)
    in_reply_to = parse.in_reply_to(raw)
    category, rule = router.classify(mail, _parent(in_reply_to))
    if category == Category.TARIFF:
        from app.agents import tariff

        tariff.enqueue(mail)
    saved = store.save_mail(mail, category, in_reply_to)
    if not saved.is_new:
        log.info("mail %s already stored, skipped", mail.id)
        return
    files = [] if category == Category.TARIFF else download_attachments(mail)
    note = f", {len(files)} attachment(s) saved" if files else ""
    log.info(
        "mail %s from %s -> %s (%s)%s", mail.id, mail.sender, category.value, rule, note
    )
    store.emit("routed", f"Routed as {category.value} ({rule}){note}", saved.session_id)
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
        from app.agents import clarification, tracking

        def tracking_reply():
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
    batch = hackloop.poll(since_id, limit=POLL_LIMIT)
    messages = parse.messages(batch)
    for raw in messages:
        mail_id = parse.message_id(raw)
        try:
            handle(raw)
        except Exception as e:
            log.exception("mail %s failed", mail_id)
            store.emit(
                "error",
                f"Mail {mail_id} failed: {type(e).__name__}: {e}",
                level="error",
            )
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
    log.info(
        "poller started at since_id=%s, every %.1fs", since_id, settings.poll_interval_s
    )
    while not stop.is_set():
        count = 0
        try:
            since_id, count = poll_once(since_id)
        except Exception:
            log.exception("poll failed")
        if count < POLL_LIMIT:  # a full batch means more is waiting: poll again at once
            stop.wait(settings.poll_interval_s)
