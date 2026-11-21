"""Which language a mail is written in, by counting common function words (no LLM).

The LLM guessed from names and places: an English mail from a German company (Sonnberg
Kaffeeroesterei, Bad Homburg) came back as "de", and the quote went out in German.
Function words do not depend on names, signatures or addresses.
"""

from __future__ import annotations

import re

_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        [
            "the",
            "and",
            "you",
            "your",
            "please",
            "could",
            "would",
            "for",
            "with",
            "our",
            "we",
            "is",
            "are",
            "this",
            "that",
            "of",
            "to",
            "be",
            "can",
            "from",
            "will",
            "an",
            "it",
            "at",
            "on",
            "by",
            "thank",
            "thanks",
            "kind",
            "regards",
            "dear",
            "hello",
            "hi",
        ]
    ),
    "de": frozenset(
        [
            "und",
            "der",
            "die",
            "das",
            "bitte",
            "sie",
            "ihr",
            "ihre",
            "ihnen",
            "wir",
            "ist",
            "sind",
            "für",
            "mit",
            "von",
            "zu",
            "ein",
            "eine",
            "einen",
            "nicht",
            "auf",
            "aus",
            "bei",
            "uns",
            "unser",
            "unsere",
            "vielen",
            "dank",
            "grüße",
            "gruesse",
            "sehr",
            "geehrte",
            "hallo",
            "können",
            "könnten",
            "würden",
            "wäre",
        ]
    ),
    "fr": frozenset(
        [
            "le",
            "la",
            "les",
            "et",
            "vous",
            "votre",
            "nous",
            "est",
            "pour",
            "avec",
            "une",
            "des",
            "du",
            "au",
            "merci",
            "bonjour",
            "pouvez",
            "cordialement",
        ]
    ),
    "es": frozenset(
        [
            "el",
            "la",
            "los",
            "las",
            "y",
            "usted",
            "su",
            "nosotros",
            "es",
            "para",
            "con",
            "una",
            "del",
            "por",
            "gracias",
            "hola",
            "podría",
            "saludos",
        ]
    ),
    "it": frozenset(
        [
            "il",
            "lo",
            "la",
            "gli",
            "e",
            "voi",
            "vostro",
            "noi",
            "è",
            "per",
            "con",
            "una",
            "del",
            "grazie",
            "buongiorno",
            "potreste",
            "cordiali",
            "saluti",
        ]
    ),
}
_TOKEN = re.compile(r"[a-zà-ÿßäöü]+", re.IGNORECASE)
_QUOTED = re.compile(r"^\s*>.*$", re.MULTILINE)


def detect(text: str, default: str = "en") -> str:
    """ISO 639-1 code of the language with the most function words; `default` on a tie/none."""
    words = [w.lower() for w in _TOKEN.findall(_QUOTED.sub("", text or ""))]
    scores = {lang: sum(w in vocab for w in words) for lang, vocab in _WORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    top = scores[best]
    if top == 0 or list(scores.values()).count(top) > 1:
        return default
    return best
