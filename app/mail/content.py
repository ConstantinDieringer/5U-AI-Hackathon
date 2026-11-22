"""Separate a sender's current words from quoted history. Original mail is never changed."""

import re
from dataclasses import dataclass

_HISTORY = re.compile(
    r"^[ \t]*+(?:-{2,}[ \t]*(?:original message|urspr.ngliche nachricht|forwarded message|weitergeleitete nachricht).*"
    r"|begin forwarded message:.*|on .+ wrote:|am .+ schrieb.*:"
    r"|le .+ [ée]crit\s*:|el .+ escribi[oó]\s*:|il .+ (?:scritto|scrive).*"
    r"|em .+ escreveu\s*:|op .+ schreef.*:|w dniu .+ napisa[lł].*:"
    r"|.+(?:писал[аи]?|пише)\s*:|在.+写道\s*[:：]|.+書きました\s*[:：]"
    r"|من\s*[:：].+|(?:от|差出人|发件人|寄件者)[ \t]*[:：][ \t]+.+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# "From:" / "Von:" / "De :" also label an RFQ's origin ("From: Munich (MUC)"). Such a line
# starts quoted history only when it names a mail address or a header block follows it.
_FROM_LINE = re.compile(
    r"^[ \t]*(?:from|von|de|da|van)[ \t]*:[ \t]*\S.*$", re.IGNORECASE | re.MULTILINE
)
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\[mailto:", re.IGNORECASE)
_HEADER_NEXT = re.compile(
    r"^[ \t]*(?:sent|date|subject|gesendet|datum|betreff|envoy[ée]|objet|enviado|fecha"
    r"|asunto|inviato|data|oggetto|verzonden|onderwerp)[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)
_SCAN_LIMIT = 20_000  # "From:" header detection reads only the start of very long bodies


def _history_start(body: str) -> int | None:
    """Offset where quoted history begins, or None."""
    found = _HISTORY.search(body)
    head = body[:_SCAN_LIMIT]
    end = min(found.start(), len(head)) if found else len(head)
    for line in _FROM_LINE.finditer(head, 0, end):
        following = "\n".join(head[line.end() : line.end() + 600].splitlines()[1:4])
        if _ADDRESS.search(line.group(0)) or _HEADER_NEXT.search(following):
            return line.start()
    return found.start() if found else None


_SIGNATURE = re.compile(
    r"^[ \t]*+(?:best regards|kind regards|many greetings|viele gr[üu]sse|mit freundlichen|thanks a lot and have"
    r"|cordialement|bien cordialement|saludos cordiales|atentamente|cordiali saluti"
    r"|с уважением|此致|敬具|Your austrian Power Team).*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class CurrentMessage:
    text: str
    history: str
    facts: str
    subject: str


def current_message(body: str, subject: str = "") -> CurrentMessage:
    text = body or ""
    start = _history_start(text)
    active = text[:start] if start is not None else text
    history = text[start:] if start is not None else ""
    active = "\n".join(line for line in active.splitlines() if not line.lstrip().startswith(">"))
    signature = _SIGNATURE.search(active)
    facts = active[: signature.start()] if signature else active
    safe_subject = (
        ""
        if history or re.match(r"\s*(?:(?:re|fw|fwd|aw|wg):\s*)+", subject, re.IGNORECASE)
        else subject
    )
    return CurrentMessage(active.strip(), history, facts.strip(), safe_subject)
