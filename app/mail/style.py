"""Presentation rules for automatically generated mail, independent of stored money."""

import re

SIGNATURE = "Your austrian Power Team"


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
