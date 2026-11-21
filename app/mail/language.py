"""Which language a mail is written in, by script and common function words (no LLM).

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
# More Latin-script languages: mostly words that are distinctive for the language, so a mail
# in one of them does not tie with a neighbour (es/pt, da/nb/sv share a lot).
_MORE: dict[str, tuple[str, ...]] = {
    "nl": ("het", "een", "en", "van", "voor", "wij", "jullie", "graag", "niet", "zijn", "bedankt",
           "groeten", "met", "vriendelijke", "kunnen", "zending", "offerte", "ons", "onze"),
    "pt": ("não", "você", "vocês", "obrigado", "obrigada", "olá", "nós", "uma", "com", "para",
           "envio", "cotação", "atenciosamente", "podem", "nossa", "nosso", "são", "também"),
    "pl": ("i", "nie", "się", "jest", "dla", "proszę", "dziękuję", "pozdrawiam", "oraz", "nasz",
           "nasza", "czy", "przesyłka", "wycenę", "mogą", "od", "na", "dzień", "dobry"),
    "sv": ("och", "att", "är", "inte", "för", "med", "vi", "ni", "tack", "hälsningar", "vänliga",
           "kan", "från", "vår", "frakt", "offert", "hej", "också"),
    "da": ("og", "ikke", "jeg", "vi", "har", "til", "med", "venlig", "hilsen", "tak", "kan",
           "vores", "jer", "hvad", "også", "forsendelse", "tilbud"),
    "nb": ("og", "ikke", "jeg", "vi", "har", "til", "med", "vennlig", "hilsen", "takk", "kan",
           "vår", "dere", "hva", "også", "forsendelse", "tilbud"),
    "cs": ("a", "je", "se", "na", "pro", "prosím", "děkuji", "dobrý", "den", "zásilka", "nabídku",
           "můžete", "naše", "které", "není", "pozdravem", "s", "pozdravy"),
    "tr": ("ve", "bir", "bu", "için", "ile", "teşekkür", "ederiz", "lütfen", "merhaba", "değil",
           "gönderi", "teklif", "saygılarımızla", "bizim", "olarak", "kg'lık"),
    "ro": ("și", "este", "pentru", "cu", "vă", "rugăm", "mulțumim", "bună", "ziua", "nostru",
           "noastră", "ofertă", "expediere", "sunt", "nu", "dumneavoastră"),
    "hu": ("és", "a", "az", "hogy", "nem", "kérjük", "köszönöm", "köszönjük", "üdvözlettel",
           "jó", "napot", "ajánlatot", "szállítmány", "vagyunk", "egy"),
    "fi": ("ja", "on", "ei", "että", "kiitos", "terveisin", "ystävällisin", "hei", "tarjous",
           "lähetys", "voitteko", "meidän", "olemme", "myös"),
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
    "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
    "nl": "Dutch", "pt": "Portuguese", "pl": "Polish", "sv": "Swedish", "da": "Danish",
    "nb": "Norwegian", "cs": "Czech", "tr": "Turkish", "ro": "Romanian", "hu": "Hungarian",
    "fi": "Finnish", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ru": "Russian",
    "uk": "Ukrainian", "bg": "Bulgarian", "el": "Greek", "ar": "Arabic", "fa": "Persian",
    "he": "Hebrew", "th": "Thai", "hi": "Hindi",
}
TEMPLATE_LANGUAGES = frozenset({"en", "de"})  # languages our fixed mail copy is written in


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
    """ISO 639-1 code of the mail's language, or None when the words do not decide it.
    Non-Latin scripts decide by script; Latin text by counting function words. `hint`
    (e.g. the extraction LLM's guess) only breaks a tie or fills in a language we have no
    word list for, it never overrides a clear word count (names and places mislead it)."""
    own = _QUOTED.sub("", text or "")
    script = _script(own)
    if script:
        return script
    words = [w.lower() for w in _TOKEN.findall(own)]
    scores = {lang: sum(w in vocab for w in words) for lang, vocab in _WORDS.items()}
    top = max(scores.values(), default=0)
    hint = (hint or "").strip().lower()[:2] or None
    if top == 0:
        return hint
    best = [lang for lang, score in scores.items() if score == top]
    if len(best) == 1:
        return best[0]
    return hint if hint in best else None


def detect(text: str, default: str = "en", hint: str | None = None) -> str:
    """ISO 639-1 code of the language; `default` when it cannot be decided."""
    return detect_or_none(text, hint) or default
