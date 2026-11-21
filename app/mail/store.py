"""Adapter between the poller and C's app/db.py. The poller uses only these functions, so a
change in db.py's interface is a one-line change here."""

from __future__ import annotations

from dataclasses import dataclass

from app import db
from app.models import Category, Mail

SINCE_KEY = "mail.next_since_id"
LAST_SAVED_KEY = "mail.last_saved_id"


@dataclass(frozen=True)
class Saved:
    is_new: bool
    session_id: int | None


def init() -> None:
    db.init_db()


def get_since_id() -> int:
    return int(db.kv_get(SINCE_KEY, "0") or 0)


def set_since_id(since_id: int) -> None:
    db.kv_set(SINCE_KEY, str(since_id))


def save_mail(mail: Mail, category: Category, in_reply_to: int | None) -> Saved:
    """Insert the mail (idempotent by id). `is_new` is True only on the first insert.

    db.ingest_mail does not report whether the insert was new (open question for C), so until
    then `is_new` uses a high-water mark: mailbox ids only grow and the poller saves in id
    order, so an id at or below the last saved one was inserted before.
    """
    is_new = mail.id > int(db.kv_get(LAST_SAVED_KEY, "0") or 0)
    session_id = db.ingest_mail(
        mail_id=mail.id,
        sender=mail.sender,
        recipient=mail.to,
        subject=mail.subject,
        body=mail.body,
        category=category.value,
        received_at=mail.received_at,
        references=mail.references,
        in_reply_to=in_reply_to,
        raw=mail.raw,
    )
    if is_new:
        db.kv_set(LAST_SAVED_KEY, str(mail.id))
    return Saved(is_new, session_id)


def emit(
    kind: str, message: str, session_id: int | None = None, level: str = "info"
) -> None:
    db.emit(kind, message, session_id, level=level)
