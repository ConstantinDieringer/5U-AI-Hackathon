"""Validate mailbox API objects at the boundary and turn them into `Mail`.

Field names confirmed on real mail (A3): context/docs/mail-format.md.
"""

from __future__ import annotations

from typing import Any

from app.models import Mail

# Filled from the real API response in A3.
MESSAGES_KEY: str | None = "messages"
SENDER_KEY: str | None = "from"
TIMESTAMP_KEY: str | None = "received_at"


class MailFormatError(ValueError):
    """A mailbox object does not have the shape documented in mail-format.md."""


def _int(raw: dict[str, Any], key: str) -> int:
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise MailFormatError(f"{key!r} is not an int: {v!r}")
    return v


def _str(raw: dict[str, Any], key: str | None) -> str:
    if key is None:
        raise MailFormatError("field name not confirmed yet (A3)")
    v = raw.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise MailFormatError(f"{key!r} is not a string: {type(v).__name__}")
    return v


def messages(batch: dict[str, Any]) -> list[dict[str, Any]]:
    if MESSAGES_KEY is None:
        raise MailFormatError("poll list key not confirmed yet (A3)")
    items = batch.get(MESSAGES_KEY)
    if not isinstance(items, list) or not all(isinstance(m, dict) for m in items):
        raise MailFormatError(
            f"poll response has no list of objects under {MESSAGES_KEY!r}"
        )
    return items


def next_since_id(batch: dict[str, Any]) -> int | None:
    v = batch.get("next_since_id")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def message_id(raw: dict[str, Any]) -> int:
    return _int(raw, "id")


def in_reply_to(raw: dict[str, Any]) -> int | None:
    v = raw.get("in_reply_to")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def references(raw: dict[str, Any]) -> list[int]:
    v = raw.get("references") or []
    if not isinstance(v, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in v
    ):
        raise MailFormatError(f"'references' is not a list of ints: {v!r}")
    return v


def attachments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    v = raw.get("attachments") or []
    if not isinstance(v, list) or len(v) > 12 or not all(
        isinstance(a, dict) for a in v
    ):
        raise MailFormatError("'attachments' is not a list of objects with an id")
    for item in v:
        att_id = item.get("id")
        filename = item.get("filename", "")
        if (
            isinstance(att_id, bool)
            or not isinstance(att_id, int)
            or att_id <= 0
            or not isinstance(filename, str)
            or len(filename) > 255
        ):
            raise MailFormatError("invalid attachment metadata")
    return v


def mail_from_api(raw: dict[str, Any]) -> Mail:
    mid = message_id(raw)
    refs = references(raw)
    return Mail(
        id=mid,
        thread_root=refs[0] if refs else mid,
        sender=_str(raw, SENDER_KEY),
        to=_str(raw, "to"),
        subject=_str(raw, "subject"),
        body=_str(raw, "body"),
        received_at=_str(raw, TIMESTAMP_KEY),
        attachments=attachments(raw),
        references=refs,
        raw=raw,
    )
