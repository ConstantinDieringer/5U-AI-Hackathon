"""Durable tracking transport: recover misses, with one bounded final uncertainty fallback."""

import time

from app import db, hackloop
from app.config import settings
from app.mail.guard import OutboundLoopBlocked
from app.mail.outgoing import CustomerStyleError


def send(
    *,
    key: str,
    recipient: str,
    subject: str,
    body: str,
    parent_id: int,
    session_id: int | None,
    category: str,
    language_code: str,
    protected_terms: tuple[str, ...] = (),
    workflow_version: int | None = None,
    tracking_booked: bool | None = None,
) -> bool:
    claim = db.claim_tracking_mail(key, settings.tracking_notice_check_every_s)
    if claim is None:
        return False
    attempt, final = claim
    db.kv_set("tracking.notice.recovery.last_attempt", f"{key}:{time.time_ns()}")
    if final:
        line = (
            "Hier ist noch ein abschließender Trackinghinweis zu deiner Sendung."
            if language_code.startswith("de")
            else "Here is a final tracking note for your shipment."
        )
        greeting, separator, rest = body.partition("\n\n")
        body = greeting + "\n\n" + line + "\n\n" + rest if separator else line + "\n\n" + body

    def before_send() -> None:
        if workflow_version is not None and not db.reply_current(
            session_id, parent_id, workflow_version
        ):
            raise hackloop.CustomerSendBlocked("Tracking reply superseded")

    try:
        sent = hackloop.send_customer(
            recipient,
            subject,
            body,
            in_reply_to=parent_id,
            session_id=session_id,
            review=False,
            language_code=language_code,
            protected_terms=protected_terms,
            tracking_booked=tracking_booked,
            guard_transport=before_send,
        )
    except (
        hackloop.CustomerSendBlocked,
        hackloop.QuarantinedReply,
        OutboundLoopBlocked,
        CustomerStyleError,
    ):
        db.tracking_mail_result(key, attempt)
        return False
    except Exception as exc:  # noqa: BLE001 - persist uncertainty; at most one final send
        limited = isinstance(exc, hackloop.HackloopError) and exc.status == 429
        db.tracking_mail_result(key, attempt, uncertain=not limited)
        db.emit(
            "tracking_mail_deferred" if limited else "tracking_mail_unknown",
            "Trackingversand zurückgestellt"
            if limited
            else "Trackingversand unklar, Abschlussversuch begrenzt",
            session_id,
            level="warn",
            reason=type(exc).__name__,
            final=final,
            key=key,
        )
        return False
    # A receipt survives a crash during local mail recording and forbids another send.
    mid = int(sent["id"])
    db.tracking_mail_result(key, attempt, mid)
    db.record_outgoing(
        mail_id=mid,
        recipient=recipient,
        subject=sent["subject"],
        body=sent["body"],
        category=category,
        session_id=session_id,
        in_reply_to=parent_id,
        raw=sent,
        sender=settings.team_mailbox,
    )
    db.emit(
        "tracking_mail_sent",
        "Trackingmail versendet",
        session_id,
        mail_id=mid,
        final=final,
        category=category,
    )
    return True
