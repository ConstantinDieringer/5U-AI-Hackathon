"""Language of the current message, independent of names, addresses and quoted history.

The LLM guessed from names and places: an English mail from a German company (Sonnberg
Kaffeeroesterei, Bad Homburg) came back as "de", and the quote went out in German.
Function words do not depend on names, signatures or addresses.
"""

from __future__ import annotations

import re
from functools import lru_cache

from lingua import LanguageDetector, LanguageDetectorBuilder

from app.mail.content import current_message

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
            "where",
            "when",
            "what",
            "how",
            "weight",
            "gross",
            "volume",
            "pieces",
            "shipment",
            "freight",
            "quote",
            "pickup",
            "booking",
            "request",
            "status",
            "origin",
            "destination",
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
            "trasporto",
            "aereo",
            "spedizione",
            "richiesta",
        ]
    ),
    # Portuguese scored as Spanish ("para", "por") and got Spanish replies: only words that
    # Spanish and Italian do not use
    "pt": frozenset(
        [
            "você",
            "vocês",
            "obrigado",
            "obrigada",
            "não",
            "uma",
            "os",
            "com",
            "em",
            "nós",
            "nosso",
            "nossa",
            "olá",
            "bom",
            "dia",
            "cumprimentos",
            "atenciosamente",
            "podem",
            "poderia",
            "pelo",
            "pela",
            "ao",
            "também",
            "são",
            "está",
        ]
    ),
}
_TOKEN = re.compile(r"[a-zà-ÿßäöü]+", re.IGNORECASE)
_QUOTED = re.compile(r"^\s*>.*$", re.MULTILINE)


def normalize(code: str | None, default: str = "en") -> str:
    """Accept language tags without a language allowlist; strip unsafe/invalid values."""
    tag = (code or "").strip().lower().replace("_", "-")
    return tag if re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", tag) else default


def base(code: str | None) -> str:
    return normalize(code).split("-", 1)[0]


@lru_cache(maxsize=1)
def _detector() -> LanguageDetector:
    # Trigrams keep the all-language detector small enough for the single live container.
    return LanguageDetectorBuilder.from_all_languages().with_low_accuracy_mode().build()


@lru_cache(maxsize=512)
def detect(text: str, default: str = "en") -> str:
    """Local detection; ambiguous short replies inherit the thread's language.

    `und` is available to callers that need model identification rather than an English
    fallback. The detector's language set never limits what the gateway can process.
    """
    active = current_message(text or "").facts
    active = re.sub(r"<[^>]*>", "", active)
    active = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+|\b[A-Z]{2,4}\d*\b", "", active)
    if len(re.sub(r"[\W\d_]", "", active)) < 3:
        return normalize(default)
    words = [w.lower() for w in _TOKEN.findall(_QUOTED.sub("", active))]
    scores = {lang: sum(w in vocab for w in words) for lang, vocab in _WORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    top = scores[best]
    runner_up = sorted(scores.values())[-2]
    if len(words) <= 5 and top > runner_up:
        return best
    if top >= 2 and top - runner_up >= 2:
        return best
    values = _detector().compute_language_confidence_values(active)
    if values and values[0].value >= 0.6 and values[0].value - values[1].value >= 0.15:
        return values[0].language.iso_code_639_1.name.lower()
    if top > 0 and top > runner_up:
        return best
    return normalize(default)
