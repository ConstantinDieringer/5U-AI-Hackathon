"""One German address form per thread: du by default, Sie when the customer writes with Sie.

Templates are written in either form; the send gate (hackloop.send_customer) turns every
German customer mail into the thread's form, so quote, questions, follow-ups, booking and
tracking never switch between du and Sie. The form is fixed at the first German reply and
kept for the whole session.
"""

from __future__ import annotations

import re
from typing import Literal

from app import db
from app.mail.content import current_message
from app.mail.router import address

Form = Literal["du", "Sie"]
_KEY = "address.form."

# How the customer addresses us. Only their own words count (quoted history holds our mails).
_CUSTOMER_SIE = re.compile(
    r"\b(?:Ihnen|Ihre[nmrs]?|Ihr)\b|(?<![.!?\n]\s)(?<![.!?\n])(?<!^)\bSie\b|\bSehr geehrte",
)
_CUSTOMER_DU = re.compile(r"\b(?:[Dd]u|[Dd]ir|[Dd]ich|[Dd]ein\w*|[Ee]uch|[Ee]ue?r\w*)\b")

# Markers in OUR text. A sentence-initial "Sie ist/wird/..." is the shipment, not the reader.
_OUR_SIE = re.compile(
    r"\b(?:Ihnen|Ihre[nmrs]?|Ihr)\b|(?<=[a-zäöüß,] )Sie\b"
    r"|(?:^|(?<=[.!?] )|(?<=\n))Sie (?!ist|wird|war|hat|fliegt|steht|liegt|kommt|bleibt)\w+",
    re.MULTILINE,
)
_OUR_DU = re.compile(r"\b(?:[Dd]u|[Dd]ir|[Dd]ich|[Dd]ein(?:e[nmrs]?)?)\b")

# Exact phrases from our templates, longest first. A new template phrase needs a pair here;
# tests/test_address_form.py fails for any German template literal that does not convert.
_PAIRS: list[tuple[str, str]] = [
    (
        "Melde dich gern, wenn du noch etwas brauchst",
        "Melden Sie sich gern, wenn Sie noch etwas brauchen",
    ),
    (
        "Wenn du die Sendung später doch verschicken möchtest",
        "Wenn Sie die Sendung später doch verschicken möchten",
    ),
    ("welche Angabe du vorher klären möchtest", "welche Angabe Sie vorher klären möchten"),
    ("die du ausgeschlossen hast", "die Sie ausgeschlossen haben"),
    ("konntest du dir", "konnten Sie sich"),
    ("wie schön, dass du dich meldest", "wie schön, dass Sie sich melden"),
    ("Wenn du buchen möchtest", "Wenn Sie buchen möchten"),
    ("Möchtest du", "Möchten Sie"),
    ("Kannst du", "Können Sie"),
    ("kannst du", "können Sie"),
    ("Könntest du", "Könnten Sie"),
    ("könntest du", "könnten Sie"),
    ("Du hattest", "Sie hatten"),
    ("du hattest", "Sie hatten"),
    ("sag mir einfach Bescheid", "sagen Sie mir einfach Bescheid"),
    ("Sag mir einfach Bescheid", "Sagen Sie mir einfach Bescheid"),
    ("antworte einfach", "antworten Sie einfach"),
    ("Antworte einfach", "Antworten Sie einfach"),
    ("schick uns", "schicken Sie uns"),
    ("Schick uns", "Schicken Sie uns"),
    ("nenn uns", "nennen Sie uns"),
    ("Nenn uns", "Nennen Sie uns"),
    ("Danke dir", "Danke Ihnen"),
    ("danke dir", "danke Ihnen"),
    ("helfe ich dir", "helfe ich Ihnen"),
    ("frage ich dich", "frage ich Sie"),
    ("halte dich", "halte Sie"),
    ("bestätigen wir dir", "bestätigen wir Ihnen"),
    ("bestätige ich dir", "bestätige ich Ihnen"),
    ("schicke ich dir", "schicke ich Ihnen"),
    ("schicken wir dir", "schicken wir Ihnen"),
    ("machen wir dir", "machen wir Ihnen"),
    ("bieten wir dir", "bieten wir Ihnen"),
    ("Wir bieten dir", "Wir bieten Ihnen"),
    ("können wir dir", "können wir Ihnen"),
    ("damit wir dir", "damit wir Ihnen"),
    ("Damit wir dir", "Damit wir Ihnen"),
    ("bei dir", "bei Ihnen"),
    ("für dich", "für Sie"),
]
_POSSESSIVE = [
    ("deinen", "Ihren"),
    ("deinem", "Ihrem"),
    ("deiner", "Ihrer"),
    ("deines", "Ihres"),
    ("deine", "Ihre"),
    ("dein", "Ihr"),
]


def customer_form(text: str) -> Form | None:
    """The form the customer used in their own words, or None without a signal."""
    own = current_message(text or "").facts
    if _CUSTOMER_SIE.search(own):
        return "Sie"
    if _CUSTOMER_DU.search(own):
        return "du"
    return None


def form(session_id: int | None) -> Form:
    """The thread's form, fixed at the first decision so it never switches mid-thread."""
    if session_id is None:
        return "du"
    stored = db.kv_get(f"{_KEY}{session_id}")
    if stored in ("du", "Sie"):
        return stored  # type: ignore[return-value]
    session = db.get_session(session_id) or {}
    customer = address(session.get("customer") or "")
    chosen: Form = "du"
    for mail in session.get("mails", []):
        if mail.get("direction") != "in" or (customer and address(mail["sender"]) != customer):
            continue
        found = customer_form(mail.get("body") or "")
        if found:
            chosen = found
            break
    db.kv_set(f"{_KEY}{session_id}", chosen)
    return chosen


def _sentence_case(text: str) -> str:
    """Re-capitalize du forms that now open a sentence or paragraph."""
    return re.sub(
        r"(^|[.!?]\s+|\n\s*)(du|dir|dich|dein\w*)\b",
        lambda m: m[1] + m[2][0].upper() + m[2][1:],
        text,
        flags=re.MULTILINE,
    )


def convert(text: str, target: Form) -> str:
    """Our German template phrases in the target form. Unknown phrasing stays as written."""
    if target == "du":
        for du, sie in _PAIRS:
            text = text.replace(sie, du)
        text = re.sub(r"\bIhnen\b", "dir", text)
        for du, sie in _POSSESSIVE:
            text = re.sub(rf"\b{sie}\b", du, text)
        return _sentence_case(text)
    for du, sie in _PAIRS:
        text = text.replace(du, sie)
    for pronoun, polite in (("dir", "Ihnen"), ("Dir", "Ihnen"), ("dich", "Sie"), ("Dich", "Sie")):
        text = re.sub(rf"\b{pronoun}\b", polite, text)
    for du, sie in _POSSESSIVE:
        text = re.sub(rf"\b{du}\b", sie, text)
        text = re.sub(rf"\b{du.capitalize()}\b", sie, text)
    return text


def mixed(text: str, target: Form) -> list[str]:
    """Words of the other form left in our German text (empty when consistent)."""
    other = _OUR_DU if target == "Sie" else _OUR_SIE
    return [m[0] for m in other.finditer(text)]
