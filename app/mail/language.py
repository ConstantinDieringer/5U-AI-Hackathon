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
            "i",
            "agree",
            "dangerous",
            "goods",
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
            "oui",
            "allez",
            "réservez",
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
            "sí",
            "adelante",
            "reserven",
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
            "sì",
            "procedete",
            "prenotate",
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
# More Latin-script languages: mostly words that are distinctive for the language, so a mail
# in one of them does not tie with a neighbour (da/nb/sv share a lot). pt is curated above.
_MORE: dict[str, tuple[str, ...]] = {
    "nl": (
        "het",
        "een",
        "en",
        "van",
        "voor",
        "wij",
        "jullie",
        "graag",
        "niet",
        "zijn",
        "bedankt",
        "groeten",
        "met",
        "vriendelijke",
        "kunnen",
        "zending",
        "offerte",
        "ons",
        "onze",
    ),
    "pl": (
        "i",
        "nie",
        "się",
        "jest",
        "dla",
        "proszę",
        "dziękuję",
        "pozdrawiam",
        "oraz",
        "nasz",
        "nasza",
        "czy",
        "przesyłka",
        "wycenę",
        "mogą",
        "od",
        "na",
        "dzień",
        "dobry",
    ),
    "sv": (
        "och",
        "att",
        "är",
        "inte",
        "för",
        "med",
        "vi",
        "ni",
        "tack",
        "hälsningar",
        "vänliga",
        "kan",
        "från",
        "vår",
        "frakt",
        "offert",
        "hej",
        "också",
    ),
    "da": (
        "og",
        "ikke",
        "jeg",
        "vi",
        "har",
        "til",
        "med",
        "venlig",
        "hilsen",
        "tak",
        "kan",
        "vores",
        "jer",
        "hvad",
        "også",
        "forsendelse",
        "tilbud",
    ),
    "nb": (
        "og",
        "ikke",
        "jeg",
        "vi",
        "har",
        "til",
        "med",
        "vennlig",
        "hilsen",
        "takk",
        "kan",
        "vår",
        "dere",
        "hva",
        "også",
        "forsendelse",
        "tilbud",
    ),
    "cs": (
        "a",
        "je",
        "se",
        "na",
        "pro",
        "prosím",
        "děkuji",
        "dobrý",
        "den",
        "zásilka",
        "nabídku",
        "můžete",
        "naše",
        "které",
        "není",
        "pozdravem",
        "s",
        "pozdravy",
    ),
    "tr": (
        "ve",
        "bir",
        "bu",
        "için",
        "ile",
        "teşekkür",
        "ederiz",
        "lütfen",
        "merhaba",
        "değil",
        "gönderi",
        "teklif",
        "saygılarımızla",
        "bizim",
        "olarak",
        "kg'lık",
    ),
    "ro": (
        "și",
        "este",
        "pentru",
        "cu",
        "vă",
        "rugăm",
        "mulțumim",
        "bună",
        "ziua",
        "nostru",
        "noastră",
        "ofertă",
        "expediere",
        "sunt",
        "nu",
        "dumneavoastră",
    ),
    "hu": (
        "és",
        "a",
        "az",
        "hogy",
        "nem",
        "kérjük",
        "köszönöm",
        "köszönjük",
        "üdvözlettel",
        "jó",
        "napot",
        "ajánlatot",
        "szállítmány",
        "vagyunk",
        "egy",
    ),
    "fi": (
        "ja",
        "on",
        "ei",
        "että",
        "kiitos",
        "terveisin",
        "ystävällisin",
        "hei",
        "tarjous",
        "lähetys",
        "voitteko",
        "meidän",
        "olemme",
        "myös",
    ),
}
for _lang, _words in _MORE.items():
    _WORDS[_lang] = frozenset(_words)

# Non-Latin scripts decide the language on their own: one script, one (default) language.
_SCRIPTS: tuple[tuple[str, str], ...] = (
    ("ja", "぀-ヿ"),  # hiragana / katakana (checked before Han)
    ("ko", "가-힯ᄀ-ᇿ"),
    ("zh", "一-鿿㐀-䶿"),
    ("ru", "Ѐ-ӿ"),
    ("el", "Ͱ-Ͽ"),
    ("ar", "؀-ۿ"),
    ("he", "֐-׿"),
    ("th", "฀-๿"),
    ("hi", "ऀ-ॿ"),
)
_SCRIPT_RE = {lang: re.compile(f"[{chars}]") for lang, chars in _SCRIPTS}
_LETTER = re.compile(r"[^\W\d_]")
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
_QUOTED = re.compile(r"^\s*>.*$", re.MULTILINE)

NAMES = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
    "pl": "Polish",
    "sv": "Swedish",
    "da": "Danish",
    "nb": "Norwegian",
    "cs": "Czech",
    "tr": "Turkish",
    "ro": "Romanian",
    "hu": "Hungarian",
    "fi": "Finnish",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ru": "Russian",
    "uk": "Ukrainian",
    "bg": "Bulgarian",
    "el": "Greek",
    "ar": "Arabic",
    "fa": "Persian",
    "he": "Hebrew",
    "th": "Thai",
    "hi": "Hindi",
}
TEMPLATE_LANGUAGES = frozenset({"en", "de"})  # languages our fixed mail copy is written in


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
def detect(text: str, default: str = "en", hint: str | None = None) -> str:
    """Local detection; ambiguous short replies inherit the thread's language.

    `und` is available to callers that need model identification rather than an English
    fallback. The detector's language set never limits what the gateway can process.
    """
    active = current_message(text or "").facts
    active = re.sub(r"<[^>]*>", "", active)
    active = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+|\b[A-Z]{2,4}\d*\b", "", active)
    fallback = normalize(hint, default) if hint else default
    if len(re.sub(r"[\W\d_]", "", active)) < 3:
        return fallback
    script = _script(active)
    if script:
        values = _detector().compute_language_confidence_values(active)
        if values and values[0].value >= 0.6 and values[0].value - values[1].value >= 0.15:
            return values[0].language.iso_code_639_1.name.lower()
        return script
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
    return fallback


def name(code: str) -> str:
    """English name of a language code, for prompts ('fr' -> 'French')."""
    return NAMES.get(code, code)


def _decides(hits: int, letters: int) -> bool:
    """A script decides with a third of all letters, or with 20 characters: an English
    sign-off under a Chinese mail (real RFQ 20150) or IATA codes must not outvote it."""
    return hits >= 3 and (hits * 3 >= letters or hits >= 20)


def _script(text: str) -> str | None:
    letters = _LETTER.findall(text)
    if not letters:
        return None
    kana = len(_SCRIPT_RE["ja"].findall(text))
    han = len(_SCRIPT_RE["zh"].findall(text))
    if kana >= 2 and _decides(kana + han, len(letters)):  # Japanese mixes kana with kanji
        return "ja"
    for lang, pattern in _SCRIPT_RE.items():
        hits = len(pattern.findall(text))
        if _decides(hits, len(letters)):
            if lang == "ru":
                if re.search("[ієїґ]", text, re.IGNORECASE):
                    return "uk"
                return "ru"
            if lang == "ar" and re.search("[پچژگ]", text):
                return "fa"
            return lang
    return None


def detect_or_none(text: str, hint: str | None = None) -> str | None:
    code = detect(text, default="und", hint=hint)
    return None if code == "und" else code
