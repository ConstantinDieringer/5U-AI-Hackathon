"""Separate a sender's current words from quoted history. Original mail is never changed."""

import re
from dataclasses import dataclass

_HISTORY = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|urspr.ngliche nachricht|forwarded message|weitergeleitete nachricht).*"
    r"|begin forwarded message:.*|on .+ wrote:|am .+ schrieb.*:|from:\s+.+|von:\s+.+"
    r"|le .+ [ée]crit\s*:|el .+ escribi[oó]\s*:|il .+ (?:scritto|scrive).*"
    r"|em .+ escreveu\s*:|op .+ schreef.*:|w dniu .+ napisa[lł].*:"
    r"|.+(?:писал[аи]?|пише)\s*:|在.+写道\s*[:：]|.+書きました\s*[:：]"
    r"|من\s*[:：].+|(?:de|da|van|от|差出人|发件人|寄件者)\s*[:：]\s+.+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SIGNATURE = re.compile(
    r"^\s*(?:best regards|kind regards|many greetings|viele gr[üu]sse|mit freundlichen|thanks a lot and have"
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
    boundary = _HISTORY.search(body or "")
    active = (body or "")[: boundary.start()] if boundary else (body or "")
    history = (body or "")[boundary.start() :] if boundary else ""
    active = "\n".join(line for line in active.splitlines() if not line.lstrip().startswith(">"))
    signature = _SIGNATURE.search(active)
    facts = active[: signature.start()] if signature else active
    safe_subject = (
        ""
        if history or re.match(r"\s*(?:(?:re|fw|fwd|aw|wg):\s*)+", subject, re.IGNORECASE)
        else subject
    )
    return CurrentMessage(active.strip(), history, facts.strip(), safe_subject)
