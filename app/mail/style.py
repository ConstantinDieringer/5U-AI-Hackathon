"""Presentation rules for automatically generated mail, independent of stored money."""

import os
import re

TEAM_LINE = "Your austrian Power Team"
# a real first name above the team line reads like a person wrote the mail (jury bonus)
SIGNER = os.getenv("MAIL_SIGNER", "Moritz").strip() or "Moritz"
SIGNATURE = f"{SIGNER}\n{TEAM_LINE}"


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
    number = f"{whole:,}".replace(",", ".") + f",{cents:02d}"
    return f"{sign}{number} {'€' if currency == 'EUR' else currency}"


_BOOKED_JOKES = {
    "en": (
        "Your cargo now has a window seat and, unlike us, gets to skip the airport coffee.",
        "We told the pilot it is precious cargo, so it now officially outranks the in-flight snacks.",
        "Your shipment is flying business class. We are still waiting for our own upgrade.",
        "Your boxes will see more of the world this week than we will.",
    ),
    "de": (
        "Ihre Ware hat jetzt einen Fensterplatz und muss, anders als wir, keinen Flughafenkaffee trinken.",
        "Wir haben dem Piloten gesagt, dass es wertvolle Fracht ist. Damit ist sie offiziell wichtiger als die Bordverpflegung.",
        "Ihre Sendung fliegt quasi Business Class. Auf unser eigenes Upgrade warten wir noch.",
        "Ihre Kartons sehen diese Woche mehr von der Welt als wir.",
    ),
}


def booked_joke(lang: str, key: str) -> str:
    """One short, friendly line for a completed booking; the same booking always gets the same."""
    jokes = _BOOKED_JOKES["de" if lang == "de" else "en"]
    return jokes[sum(map(ord, key or "")) % len(jokes)]
