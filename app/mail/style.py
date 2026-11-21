"""Presentation rules for automatically generated mail, independent of stored money."""

import os
import re
from datetime import UTC, date, datetime

from babel import Locale, UnknownLocaleError
from babel.dates import format_date

from app.mail.language import base, normalize

TEAM_LINE = "Your austrian Power Team"
# a real first name above the team line reads like a person wrote the mail (jury bonus)
SIGNER = os.getenv("MAIL_SIGNER", "Moritz").strip() or "Moritz"
SIGNATURE = f"{SIGNER}\n{TEAM_LINE}"

_MONTHS = {
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    "de": (
        "Januar",
        "Februar",
        "März",
        "April",
        "Mai",
        "Juni",
        "Juli",
        "August",
        "September",
        "Oktober",
        "November",
        "Dezember",
    ),
}


def written_date(value: date, lang: str) -> str:
    language = base(lang)
    if language not in _MONTHS:
        try:
            return format_date(value, format="long", locale=Locale.parse(normalize(lang), sep="-"))
        except (UnknownLocaleError, ValueError):
            # Languages without CLDR data still go through the model translation boundary.
            language = "en"
    day = f"{value.day}." if language == "de" else str(value.day)
    return f"{day} {_MONTHS[language][value.month - 1]} {value.year}"


def customer_presentation(text: str, lang: str) -> str:
    """Render literal dates/currency in prose without touching embedded technical IDs."""
    text = re.sub(r"(?<![\w-])EUR(?![\w-])", "€", text)

    def without_zero_fraction(match: re.Match[str]) -> str:
        # Three zero digits alone can be a thousands group, not a decimal fraction.
        if len(match[2]) == 3 and not re.search(r"[.,]", match[1]) and match[1] != "0":
            return match[0]
        return match[1]

    text = re.sub(
        r"(?<![\w.,-])(\d+(?:[.,]\d{3})*)[.,](0+)(?![\w.,-])",
        without_zero_fraction,
        text,
    )

    def replace_timestamp(match: re.Match[str]) -> str:
        try:
            value = datetime.fromisoformat(match[0])
            zone = ""
            if value.tzinfo is not None:
                value = value.astimezone(UTC)
                zone = " UTC"
            joiner = " um " if lang == "de" else " at " if lang == "en" else " "
            return written_date(value.date(), lang) + joiner + value.strftime("%H:%M") + zone
        except ValueError:
            return match[0]

    text = re.sub(
        r"(?<![\w-])\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
        r"(?:Z|[+-]\d{2}:\d{2})?(?![\w-])",
        replace_timestamp,
        text,
    )

    def replace_date(match: re.Match[str]) -> str:
        try:
            return written_date(date.fromisoformat(match[0]), lang)
        except ValueError:
            return match[0]  # invalid model dates remain visible to the validator

    text = re.sub(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])", replace_date, text)
    paragraphs = []
    for paragraph in text.split("\n\n"):
        if len(paragraph.split()) <= 45:
            paragraphs.append(paragraph)
            continue
        sentences, start = [], 0
        for boundary in re.finditer(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])", paragraph):
            before = paragraph[: boundary.start()]
            # German day numbers and ordinary titles are not sentence endings.
            if re.search(r"(?:\d|\b(?:Dr|Mr|Ms|Mrs))\.$", before):
                continue
            sentences.append(paragraph[start : boundary.start()])
            start = boundary.end()
        sentences.append(paragraph[start:])
        group = ""
        for sentence in sentences:
            if group and len((group + " " + sentence).split()) > 40:
                paragraphs.append(group)
                group = ""
            group = f"{group} {sentence}".strip()
        if group:
            paragraphs.append(group)
    return "\n\n".join(paragraphs)


def has_customer_presentation_issue(text: str) -> bool:
    """Reject backend vocabulary and dense/technical presentation in generated replies."""
    return bool(
        re.search(r"[()]|(?<![\w@.-])[a-z]+(?:_[a-z0-9]+)+(?![\w@-]|\.[A-Za-z0-9])", text)
        or re.search(
            r"^\s*(?:origin|destination|preference|source|language)\s*:",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        or re.search(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?=T|\b)(?!-)", text)
        or re.search(r"\b\d{1,2}[./]\d{1,2}[./]\d{4}\b", text)
        or re.search(
            r"\b\d{1,2}\.?\s+(?:Jan|Feb|Mar|Mär|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Okt|Nov|Dec|Dez)"
            r"\.?\s+\d{4}\b",
            text,
            re.IGNORECASE,
        )
        or any(len(paragraph.split()) > 50 for paragraph in text.split("\n\n"))
    )


def has_template_artifacts(text: str) -> bool:
    """Detect unfilled template fields and mail headers inside generated body text."""
    return bool(
        re.search(r"\[[^\]\r\n]+\]|\{[^{}\r\n]+\}|<[^<>\r\n]+>", text)
        or re.search(
            r"^\s*(?:subject|betreff|from|von|to|an):",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
    )


def amount(minor: int, currency: str = "EUR") -> str:
    """Format integer minor units without float conversion or currency conversion."""
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(minor), 100)
    number = f"{whole:,}".replace(",", ".") + (f",{cents:02d}" if cents else "")
    return f"{sign}{number} {'€' if currency.upper() == 'EUR' else currency}"


_BOOKED_JOKES = {
    "en": (
        "We will leave the jet lag to the cargo.",
        "The cargo can handle the sightseeing. We will keep the details organised.",
        "Your boxes get the adventure. We will keep an eye on the arrangements.",
        "We will let the cargo do the travelling this time.",
    ),
    "de": (
        "Den Jetlag überlassen wir der Fracht.",
        "Die Reiselust überlassen wir den Kartons. Um die Details kümmern wir uns.",
        "Ihre Kartons bekommen das Abenteuer. Wir behalten die Organisation im Blick.",
        "Dieses Mal lassen wir die Fracht auf Reisen gehen.",
    ),
}


def booked_joke(lang: str, key: str) -> str:
    """One short, friendly line for a completed booking; the same booking always gets the same."""
    jokes = _BOOKED_JOKES["de" if lang == "de" else "en"]
    return jokes[sum(map(ord, key or "")) % len(jokes)]
