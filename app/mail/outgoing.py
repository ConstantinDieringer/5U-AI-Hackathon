"""Final deterministic customer-mail presentation, immediately before sending."""

import re
import unicodedata
from datetime import date
from html import unescape

from app.mail.style import customer_presentation, written_date

EMAIL_STYLE_VERSION = "strict-customer-prose-v4"
_PUNCTUATION = str.maketrans(
    {
        "：": ":",
        "﹕": ":",
        "∶": ":",
        "꞉": ":",
        "；": ";",
        "﹔": ";",
        ";": ";",
        "（": "(",
        "）": ")",
        "﹙": "(",
        "﹚": ")",
        "＿": "_",
    }
)
_REFERENCES = re.compile(r"\b(?:BK-[A-Za-z0-9]+|AP-\d+|R-[A-Za-z0-9_-]+)\b")
_INTERNAL = re.compile(
    r"\b(?:road_transit|collection_lead|carrier_timing_kind|road_transit_hours|carrier_reported_lead_hours|confirmed_collection_slot|pickup_included|weight_kg|volume_cbm|ready_date|selection_explanation)\b",
    re.IGNORECASE,
)


class CustomerStyleError(ValueError):
    pass


def _plain_punctuation(text: str) -> str:
    # Encoded and visually identical punctuation must not bypass the prose contract.
    for _ in range(3):
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    return "".join(
        "-" if unicodedata.category(c) == "Pd" else c for c in text.translate(_PUNCTUATION)
    )


def _protect(text: str) -> tuple[str, list[str]]:
    references: list[str] = []

    def replace(match: re.Match[str]) -> str:
        references.append(match[0])
        return f"\ue000{len(references) - 1}\ue001"

    return _REFERENCES.sub(replace, text), references


def customer_body(text: str, lang: str) -> str:
    """Change display only; retain amounts, dates, words and real reference values."""
    masked, references = _protect(_plain_punctuation(text))
    for contact in re.findall(r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", masked):
        if re.search(r"[:;_()\-–—]", contact):
            raise CustomerStyleError("Do not alter a contact or URL to satisfy prose style")
    if _INTERNAL.search(masked):
        raise CustomerStyleError("Internal field names must be expressed in ordinary language")
    masked = customer_presentation(masked, lang)
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", masked):
        raise CustomerStyleError("Invalid or unresolved customer date")

    def numeric_date(match: re.Match[str]) -> str:
        try:
            return written_date(date(int(match[3]), int(match[2]), int(match[1])), lang)
        except ValueError as exc:
            raise CustomerStyleError("Invalid customer date") from exc

    masked = re.sub(r"\b(\d{1,2})[-./](\d{1,2})[-./](\d{4})\b", numeric_date, masked)

    def clock(match: re.Match[str]) -> str:
        hour, minute = int(match[1]), int(match[2])
        if hour > 23 or minute > 59:
            raise CustomerStyleError("Invalid clock time")
        if lang == "de":
            return f"{hour} Uhr" + (f" {minute}" if minute else "")
        return (
            f"{hour % 12 or 12}"
            + (f".{minute:02d}" if minute else "")
            + (" am" if hour < 12 else " pm")
        )

    masked = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, masked)

    labels = {
        "booking reference": "Your booking reference is",
        "buchungsnummer": "Ihre Buchungsnummer lautet",
        "route": "Your shipment travels from" if lang != "de" else "Ihre Sendung reist von",
        "strecke": "Ihre Sendung reist von",
        "price": "The total is",
        "preis": "Der Gesamtpreis beträgt",
    }
    masked = re.sub(
        r"(?im)^(booking reference|buchungsnummer|route|strecke|price|preis)\s*:\s*([^\n]+)",
        lambda m: labels[m[1].lower()] + " " + m[2].rstrip(".") + ".\n",
        masked,
    )
    while re.search(r"\([^()]*\)", masked):
        masked = re.sub(r"\s*\(([^()]*)\)", r", \1", masked)
    masked = re.sub(r"[:;]\s*([A-Za-zÄÖÜäöüß])", lambda m: ". " + m[1].upper(), masked)
    masked = re.sub(r"[:;]", ".", masked)
    masked = re.sub(r"[-–—_]", " ", masked)
    masked = re.sub(r",\s*([.!?,])", r"\1", masked)
    masked = re.sub(r"[ \t]{2,}", " ", masked)
    masked = customer_presentation(masked, lang).strip()
    if re.search(r"[:;_()\-–—]", masked):
        raise CustomerStyleError("Customer prose still contains forbidden punctuation")
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", masked):
        raise CustomerStyleError("Customer prose still contains an ISO date")
    for index, reference in enumerate(references):
        masked = masked.replace(f"\ue000{index}\ue001", reference)
    validate_customer_text(masked)
    return masked + "\n"


def validate_customer_text(text: str) -> None:
    """Independent assertion on the final wire text, including Unicode and encoded forms."""
    masked, _ = _protect(_plain_punctuation(text))
    if re.search(r"[:;_()\-–—]", masked):
        raise CustomerStyleError("Forbidden punctuation in final customer text")
    if _INTERNAL.search(masked):
        raise CustomerStyleError("Internal field in final customer text")
    if re.search(r"\bEUR\b|\b\d{4}[-/]\d{2}[-/]\d{2}\b|\b\d{1,2}[-./]\d{1,2}[-./]\d{4}\b", masked):
        raise CustomerStyleError("Unresolved currency or date presentation")
    for match in re.finditer(r"(?<![\w.,-])(\d+(?:[.,]\d{3})*)[.,](0+)(?![\w.,-])", masked):
        if len(match[2]) != 3 or re.search(r"[.,]", match[1]) or match[1] == "0":
            raise CustomerStyleError("Unnecessary zero decimal fraction")


def customer_subject(subject: str, lang: str) -> str:
    """Keep threading in reply IDs rather than an unchecked Re: header prefix."""
    if re.search(r"[\r\n]", subject):
        raise CustomerStyleError("Subject must be a single line")
    subject = _plain_punctuation(subject)
    subject = re.sub(r"^(?:(?:re|fw|fwd|aw|wg)\s*:\s*)+", "", subject, flags=re.IGNORECASE)
    subject = re.sub(
        r"\b([A-Z]{3})\s*[-–—]\s*([A-Z]{3})\b",
        r"\1 to \2" if lang != "de" else r"\1 nach \2",
        subject,
    )
    rendered = " ".join(customer_body(subject, lang).split()).strip()
    return rendered or ("Ihre Sendung" if lang == "de" else "Your shipment")
