"""Does the customer ask for the quote as a PDF? Rules only, no LLM call.

Reads only the sender's current words (no quoted history, no signature) and the subject when
it is not a reply. A PDF the customer sends themselves ("attached PDF document", "here is the
packing list as a PDF") or declines ("no PDF needed") is not a request. A sentence that both
describes their own file and asks for ours ("Anbei die Packliste, bitte Angebot als PDF") is.
`session_asks_for_pdf` reads every customer mail of a session, so an ask in a clarification
answer or a later reply still counts. Used by `QUOTE_PDF_ON_REQUEST`.
"""

from __future__ import annotations

import re
from typing import Any

from app.mail.content import current_message
from app.mail.router import address

_SENTENCE = re.compile(r"[.!?;\n]+")
# "pdf" as a word, not a file name like "packing_list.pdf"
_PDF = re.compile(r"(?<![\w.])pdf\b", re.IGNORECASE)
# the quote wanted in PDF form, clear on its own
_ASK = re.compile(
    r"\b(?:as|in|via|per|als)\s+(?:an?\s+|einem?\s+)?pdf\b"
    r"|\bpdf[\s-]*(?:format|form|version|copy|file|document|quote|quotation|offer|angebot|datei"
    r"|dokument|kopie|fassung|offerte|please|pls|bitte)\b"
    r"|\b(?:en|em|in)\s+(?:formato?\s+)?pdf\b",
    re.IGNORECASE,
)
# "send me a PDF", "ein PDF des Angebots": a PDF, only a request with a request word
_SOME_PDF = re.compile(
    r"\b(?:an?|one|ein|eine[mns]?|un|una|um|uno)\s+pdf\b|\bpdf\s+(?:of|for|von|f[üu]r|zu[mr]?)\b",
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
    r"|allegato|see|siehe|voir|please find|here\s+(?:is|are)|here's|hier\s+(?:ist|sind)"
    r"|(?:i|we)\s+(?:am\s+|are\s+|'m\s+|'re\s+)?(?:send|sending|forward|forwarding|share|sharing)"
    r"|(?:ich|wir)\s+(?:sende|senden|schicke|schicken|übermittle|übermitteln))\b",
    re.IGNORECASE,
)
# the PDF meant is ours: the quote, or "yours too"
_OURS = re.compile(
    r"\b(?:quote|quotation|offer|proposal|price|pricing|angebot\w*|offerte|preis\w*|devis"
    r"|cotizaci[óo]n|preventivo|your|yours|ihr|ihre|euer|eure|dein|deine|back|zurück"
    r"|too|as well|also|auch|ebenfalls|same way|likewise)\b",
    re.IGNORECASE,
)
# a request addressed to us ("please find attached" is not one)
_ASK_YOU = re.compile(
    r"\b(?:could|can|would|will)\s+(?:you|u)\b"
    r"|\b(?:send|share|provide|attach|include|e-?mail|forward|give)\s+(?:me|us)\b"
    r"|\bplease\s+(?:\w+\s+){0,2}(?:send|share|provide|attach|include|reply|respond|return"
    r"|e-?mail|forward)\b"
    r"|\b(?:need|want|require|would like|'d like|prefer)\b"
    r"|\bbitte\b[^.!?\n]{0,40}\b(?:schick|send|zusend|übermittel|anh[äa]ng|beif[üu]g)\w*"
    r"|\b(?:schicken|senden|übermitteln)\s+sie\b|\b(?:schick|sende)\s+(?:mir|uns)\b"
    r"|\b(?:brauche\w*|ben[öo]tige\w*|h[äa]tten?|k[öo]nnt\w*|w[üu]rde\w*)\b"
    r"|\b(?:envoy\w*|envi\w*|invia\w*|necesit\w*|mand\w*|merci de|por favor|per favore|svp)\b",
    re.IGNORECASE,
)
_POLITE = re.compile(r"\b(?:please|pls|kindly|bitte)\b", re.IGNORECASE)


def _asks(sentence: str) -> bool:
    if not _PDF.search(sentence) or _DECLINED.search(sentence):
        return False
    request = bool(_ASK_YOU.search(sentence))
    if not _ASK.search(sentence) and not (
        _SOME_PDF.search(sentence) and (request or _POLITE.search(sentence))
    ):
        return False
    # their own file in the same sentence: only a request for ours counts
    return not _THEIRS.search(sentence) or request or bool(_OURS.search(sentence))


def asks_for_pdf(body: str, subject: str = "") -> bool:
    """True when the current mail asks for the offer as a PDF."""
    current = current_message(body, subject)
    text = f"{current.subject}\n{current.facts}"
    return any(_asks(sentence) for sentence in _SENTENCE.split(text))


def session_asks_for_pdf(session: dict[str, Any]) -> bool:
    """True when any mail the session's customer sent asks for the offer as a PDF."""
    customer = address(session.get("customer") or "")
    return bool(customer) and any(
        m.get("direction") == "in"
        and address(m.get("sender") or "") == customer
        and asks_for_pdf(m.get("body") or "", m.get("subject") or "")
        for m in session.get("mails") or []
    )
