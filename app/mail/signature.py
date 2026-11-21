"""The sender's first name, read from the lines after their sign-off, so a reply can greet
them personally. No LLM. Our own signature lives in app.mail.style.
"""

from __future__ import annotations

import re

_SIGN_OFF = re.compile(
    r"\b(regards|cheers|thanks|thank you|best|sincerely|greetings|"
    r"gr(?:ü|ue)(?:ß|ss)e\w*|gru(?:ß|ss)|danke\w*|"
    r"cordialement|merci|saludos|saluti|cordiali)\b",
    re.IGNORECASE,
)
_NAME_WORD = re.compile(r"[A-ZÄÖÜÉÈÁ][a-zäöüßéèáíóúñç'-]+")
_NOT_A_NAME = {
    "team", "the", "logistics", "customer", "service", "sales", "export", "import",
    "dispatch", "operations", "gmbh", "ag", "kg", "ltd", "inc", "co", "sent", "from",
}  # fmt: skip


def first_name(body: str) -> str | None:
    """'Cheers,\\nKlaus Weidner\\nWeidner Spielwaren GmbH' -> 'Klaus'. None when the line after
    the last sign-off does not look like a person's name (then greet neutrally)."""
    lines = [ln.strip() for ln in (body or "").splitlines() if not ln.strip().startswith(">")]
    sign_offs = [
        i for i, ln in enumerate(lines) if ln and len(ln.split()) <= 8 and _SIGN_OFF.search(ln)
    ]
    if not sign_offs:
        return None
    after = [ln for ln in lines[sign_offs[-1] + 1 :] if ln]
    if not after:
        return None
    words = after[0].replace(",", " ").split()
    if not 1 <= len(words) <= 4:
        return None
    if not all(_NAME_WORD.fullmatch(w) and w.lower() not in _NOT_A_NAME for w in words):
        return None
    return words[0]
