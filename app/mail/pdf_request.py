"""Does the customer ask for the quote as a PDF? Rules only, no LLM call.

Reads only the sender's current words (no quoted history, no signature) and the subject when
it is not a reply. A PDF the customer attaches themselves ("attached PDF document", "see the
PDF") or declines ("no PDF needed") is not a request. Used by `QUOTE_PDF_ON_REQUEST`.
"""

from __future__ import annotations

import re

from app.mail.content import current_message

_SENTENCE = re.compile(r"[.!?;\n]+")
# "pdf" as a word, not a file name like "packing_list.pdf"
_PDF = re.compile(r"(?<![\w.])pdf\b", re.IGNORECASE)
# the quote wanted in PDF form
_ASK = re.compile(
    r"\b(?:as|in|via|per|als)\s+(?:an?\s+|einem?\s+)?pdf\b"
    r"|\bpdf[\s-]*(?:format|version|copy|file|document|quote|quotation|offer|angebot|datei"
    r"|dokument|kopie|fassung|offerte)\b"
    r"|\b(?:en|em|in)\s+(?:formato?\s+)?pdf\b",
    re.IGNORECASE,
)
_DECLINED = re.compile(
    r"\b(?:no|not|without|keine?n?|ohne|nicht|sans|sin|senza|don'?t)\b[^.!?\n]{0,30}pdf\b"
    r"|\bpdf\b[^.!?\n]{0,30}\b(?:not needed|not necessary|nicht n[öo]tig|nicht notwendig"
    r"|unnecessary)\b",
    re.IGNORECASE,
)
# the customer's own document
_THEIRS = re.compile(
    r"\b(?:attached|enclosed|anbei|anhang|beigef[üu]gt|beiliegend|ci-joint|adjunto"
    r"|allegato|see|siehe|voir)\b",
    re.IGNORECASE,
)
# a real request verb ("please find attached" is not one)
_REQUEST = re.compile(
    r"\b(?:send|share|provide|need|want|would like|could you|can you|schick\w*|sende\w*"
    r"|brauche\w*|ben[öo]tige\w*|h[äa]tten?\b|envoy\w*|envi\w*|invia\w*|necesit\w*|mand\w*)",
    re.IGNORECASE,
)


def asks_for_pdf(body: str, subject: str = "") -> bool:
    """True when the current mail asks for the offer as a PDF."""
    current = current_message(body, subject)
    text = f"{current.subject}\n{current.facts}"
    for sentence in _SENTENCE.split(text):
        if not _PDF.search(sentence) or _DECLINED.search(sentence):
            continue
        if not _ASK.search(sentence):
            continue
        if _THEIRS.search(sentence) and not _REQUEST.search(sentence):
            continue
        return True
    return False
