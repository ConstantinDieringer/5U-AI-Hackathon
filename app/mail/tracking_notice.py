"""Required, factual tracking instructions independent of the writing model."""

import re
from typing import Any

from app.mail.router import address
from app.mail.style import TEAM_LINE

NOTICES = {
    (
        "fr",
        False,
    ): "Une fois ton envoi réservé, tu peux demander des nouvelles de son suivi en répondant à ce message.",
    ("fr", True): "Tu peux demander des nouvelles du suivi de ton envoi en répondant à ce message.",
    (
        "es",
        False,
    ): "Cuando tu envío esté reservado, puedes pedir información de seguimiento respondiendo a este correo.",
    ("es", True): "Puedes pedir información de seguimiento de tu envío respondiendo a este correo.",
    (
        "it",
        False,
    ): "Quando la tua spedizione sarà prenotata, puoi chiedere aggiornamenti sul tracciamento rispondendo a questa email.",
    (
        "it",
        True,
    ): "Puoi chiedere aggiornamenti sul tracciamento della tua spedizione rispondendo a questa email.",
    (
        "nl",
        False,
    ): "Zodra je zending is geboekt, kun je ons om een update over de tracking vragen door op deze mail te antwoorden.",
    (
        "nl",
        True,
    ): "Je kunt ons om een update over de tracking van je zending vragen door op deze mail te antwoorden.",
    (
        "pl",
        False,
    ): "Gdy Twoja przesyłka zostanie zarezerwowana, możesz zapytać o jej status, odpowiadając na tę wiadomość.",
    ("pl", True): "Możesz zapytać o status swojej przesyłki, odpowiadając na tę wiadomość.",
    (
        "tr",
        False,
    ): "Gönderin rezerve edildiğinde bu mesajı yanıtlayarak takip bilgisi isteyebilirsin.",
    ("tr", True): "Bu mesajı yanıtlayarak gönderin için takip bilgisi isteyebilirsin.",
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


def has_notice(body: str) -> bool:
    """Recognize an actionable invitation, including valid older confirmations."""
    plain = " ".join(body.split())
    return bool(_LEGACY.search(plain) or any(n in plain for n in NOTICES.values()))


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


def ensure_notice(body: str, lang: str, booked: bool) -> str:
    """Insert one required paragraph before the signature, after any model editing."""
    language = lang.lower().split("-")[0]
    if (language, booked) not in NOTICES:
        language = "en"
    if language == "de" and re.search(r"\b(?:Sie|Ihnen|Ihre|Ihr)\b", body):
        language = "de_formal"
    for notice in NOTICES.values():
        body = body.replace(notice, "")
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
    parts.insert(position, NOTICES[(language, booked)])
    return "\n\n".join(parts) + "\n"
