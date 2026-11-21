"""Mail classification by rules only (no LLM). First matching rule wins."""

from __future__ import annotations

import logging
import re
from email.utils import parseaddr

from app.config import settings
from app.models import Category, Mail
from app.pricing.reference import carrier_by_email

log = logging.getLogger(__name__)

_BID_TAG = re.compile(r"\[AP-\d+\]")  # same tag as app/agents/bids.py puts in our requests


def address(raw: str) -> str:
    """'Name <a@b>' or 'a@b' -> 'a@b', lower case."""
    return parseaddr(raw or "")[1].strip().lower()


def classify(mail: Mail, parent: Mail | None = None) -> tuple[Category, str]:
    """Return (category, rule). `parent` is the mail `in_reply_to` points at, if known."""
    sender = address(mail.sender)

    if sender == settings.team_mailbox:
        return Category.IGNORE, "own address"
    if sender == settings.ratedesk_address:
        return Category.PORTAL_REPLY, "ratedesk sender"
    if sender == settings.tracking_address:
        return Category.TRACKING, "tracking sender"

    if parent is not None and address(parent.sender) == settings.team_mailbox:
        recipient = carrier_by_email(address(parent.to))
        if recipient is not None:
            return (
                Category.CARRIER_BID,
                f"reply to our mail to {recipient.kind} {recipient.name}",
            )

    carrier = carrier_by_email(sender)
    if carrier is not None:
        # a tariff is a new mail; an answer to our bid request (threaded or carrying our
        # [AP-n] tag) with its offer as PDF/Word/audio stays a bid
        is_answer = bool(mail.references) or bool(
            _BID_TAG.search(mail.subject or "") or _BID_TAG.search(mail.body or "")
        )
        if carrier.kind == "airline" and mail.attachments and not is_answer:
            return Category.TARIFF, f"airline {carrier.name} with attachment"
        return Category.CARRIER_BID, f"{carrier.kind} {carrier.name} in carrier list"

    # Unknown sender = a customer. A new thread is a request (RFQ); a reply in an existing
    # thread is a follow-up (acceptance, question) for that session.
    if mail.references or parent is not None:
        return Category.CUSTOMER, "customer reply in a thread"
    return Category.RFQ, "customer, new thread"


def route(mail: Mail, parent: Mail | None = None) -> Category:
    return classify(mail, parent)[0]
