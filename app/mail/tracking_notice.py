"""Required, factual tracking instructions independent of the writing model."""

import re
from typing import Any

from app import db
from app.mail.language import base, normalize
from app.mail.router import address
from app.mail.style import TEAM_LINE

NOTICES = {
    (
        "en",
        False,
    ): "Once your shipment is booked, you can ask us for a tracking update by replying to this email.",
    ("en", True): "You can ask us for a tracking update by replying to this email.",
    (
        "de",
        False,
    ): "Sobald deine Sendung gebucht ist, kannst du für ein Update zur Sendungsverfolgung einfach auf diese Mail antworten.",
    (
        "de",
        True,
    ): "Für ein Update zur Sendungsverfolgung kannst du einfach auf diese Mail antworten.",
}
NOTICES.update(
    {
        (
            "de_formal",
            False,
        ): "Sobald Ihre Sendung gebucht ist, können Sie für ein Update zur Sendungsverfolgung einfach auf diese Mail antworten.",
        (
            "de_formal",
            True,
        ): "Für ein Update zur Sendungsverfolgung können Sie einfach auf diese Mail antworten.",
    }
)
_LEGACY = re.compile(
    r"(?:We(?:'ll| will) keep you posted\.\s*)?If you have any questions about the status,? "
    r"just reply to this mail\.|"
    r"(?:Wir melden uns, sobald es Neuigkeiten gibt\.\s*)?Bei Fragen zum Status "
    r"antworten Sie einfach auf diese Mail\.",
    re.IGNORECASE,
)

_LOCALIZED = "tracking.notice.localized.v1."


def known_notices() -> list[str]:
    return [*NOTICES.values(), *db.kv_values(_LOCALIZED)]


def notice_for(lang: str, booked: bool, sid: int | None = None) -> str:
    """A required tracking invitation in any gateway-supported language, cached durably."""
    if base(lang) in {"en", "de"}:
        return NOTICES[(base(lang), booked)]
    key = f"{_LOCALIZED}{normalize(lang)}.{int(booked)}"
    cached = db.kv_get(key)
    if cached:
        return cached
    from app.mail.multilingual import localize
    from app.mail.outgoing import customer_body

    _, translated = localize("", NOTICES[("en", booked)], lang, sid)
    translated = " ".join(customer_body(translated, lang).split())
    db.kv_set(key, translated)
    return translated


def has_notice(body: str) -> bool:
    """Recognize an actionable invitation, including valid older confirmations."""
    plain = " ".join(body.split())
    return bool(_LEGACY.search(plain) or any(n in plain for n in known_notices()))


def customer_context(session: dict[str, Any] | None, recipient: str) -> bool | None:
    if not session or session.get("kind") != "rfq":
        return None
    if not address(session.get("customer") or "") or address(recipient) != address(
        session["customer"]
    ):
        return None
    return any(
        b.get("status") in ("booked", "confirmed")
        and b.get("customer_state") in (None, "confirmed_sent")
        for b in session.get("bookings", [])
    )


def ensure_notice(body: str, lang: str, booked: bool, *, notice: str | None = None) -> str:
    """Insert one required paragraph before the signature, after any model editing."""
    language = base(lang)
    if language == "de" and re.search(r"\b(?:Sie|Ihnen|Ihre|Ihr)\b", body):
        language = "de_formal"
    required = (
        NOTICES[(language, booked)]
        if language == "de_formal"
        else (notice or notice_for(lang, booked))
    )
    for previous in known_notices():
        body = body.replace(previous, "")
    body = _LEGACY.sub("", body)
    parts = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    position = len(parts)
    if parts and parts[-1].endswith(TEAM_LINE):
        position -= 1
        if position and re.fullmatch(
            r"(?:Warm|Best|Kind) regards[,.]?|Cheers[,.]?|Viele Grüße[,.]?",
            parts[position - 1],
            re.IGNORECASE,
        ):
            position -= 1
    parts.insert(position, required)
    return "\n\n".join(parts) + "\n"
