"""Reply in the customer's own language when it is neither German nor English.

The composers only write de/en, so French, Spanish and Italian customers got English. Right
before transport, the finished subject and body are translated with one gateway call and
checked: every number, airport code and reference must survive and the result must read as
the target language. On any doubt the English original goes out unchanged (warn event).
The tracking invitation is kept verbatim (masked), so has_notice() and repair() still see it.
Rollback: REPLY_IN_CUSTOMER_LANGUAGE=false.
"""

from __future__ import annotations

import logging
import re
import time

from app.config import settings
from app.mail.language import NAMES, TEMPLATE_LANGUAGES

log = logging.getLogger(__name__)

# every language the detector can name except the two our copy is written in
LANGUAGE_NAMES = {code: name for code, name in NAMES.items() if code not in TEMPLATE_LANGUAGES}
PROMPT = (
    "Translate into {name}. Keep every number, date, price, currency, airport code, "
    "reference, name and line break exactly. Output only the translation."
    # the result still has to pass the customer prose contract (app/mail/outgoing.py)
    " Do not use colons, semicolons, parentheses, hyphens, dashes or underscores."
    " Keep the marker {mark} exactly as it is."
)
_MARK = "[[0]]"
_NUMBER = re.compile(r"\d+")
# lookarounds, not \b: CJK letters count as word characters ("发往LIM" has no boundary)
_AIRPORT = re.compile(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])")
_REFERENCE = re.compile(r"(?<![A-Za-z0-9-])(?:BK-[A-Za-z0-9]+|AP-\d+|R-[A-Za-z0-9_-]+)")
_AMOUNT = re.compile(r"\d[\d.,]*\s?€")
_TIMEOUT_S = 15.0
_MIN_LEFT_S = 3.0  # never let the translation run into the reply deadline


class _Rejected(ValueError):
    pass


def _target(session_id: int, in_reply_to: int | None, recipient: str) -> str | None:
    """Language of the customer's latest own words (quotes and forwarded history dropped)."""
    from app import db
    from app.mail.content import current_message
    from app.mail.language import detect

    # newest first; includes in_reply_to itself when it is the customer's mail
    mails = db.customer_mail_history(session_id, recipient, (in_reply_to or 2**62) + 1)
    if not mails:
        return None
    lang = detect(current_message(mails[0]["body"] or "").text, default="")
    return lang if lang in LANGUAGE_NAMES else None


def _check(original: str, translated: str, prose: str, target: str) -> None:
    """Every number, airport code and reference survives; `prose` reads as `target`."""
    from app.mail.language import detect

    for pattern, what in (
        (_NUMBER, "number"),
        (_AIRPORT, "airport code"),
        (_REFERENCE, "reference"),
    ):
        missing = set(pattern.findall(original)) - set(pattern.findall(translated))
        if missing:
            raise _Rejected(f"{what} lost: {', '.join(sorted(missing))}")
    # a € amount stays word for word: a locale's "1 475,00 €" or "1,475 €" can be misread
    amounts = {re.sub(r"\s", "", a) for a in _AMOUNT.findall(original)}
    if amounts - {re.sub(r"\s", "", a) for a in _AMOUNT.findall(translated)}:
        raise _Rejected("€ amount changed")
    if detect(prose, default="") != target:
        raise _Rejected(f"translation does not read as {target}")


def localize(
    subject: str,
    body: str,
    *,
    session_id: int | None,
    in_reply_to: int | None,
    recipient: str,
    deadline_at: float | None = None,
) -> tuple[str, str]:
    """(subject, body) in the customer's language, or the originals unchanged."""
    if not settings.reply_in_customer_language or session_id is None:
        return subject, body
    from app import llm
    from app.mail.language import detect
    from app.mail.outgoing import customer_body, customer_subject, validate_customer_text
    from app.mail.store import emit
    from app.mail.tracking_notice import NOTICES

    try:
        target = _target(session_id, in_reply_to, recipient)
    except Exception:
        log.exception("reply language lookup failed: sending as written")
        return subject, body
    if target is None or detect(body, default="") == target:
        return subject, body
    timeout = _TIMEOUT_S
    if deadline_at is not None:
        timeout = min(timeout, deadline_at - time.time() - _MIN_LEFT_S)
    if timeout <= 0:
        emit("reply_language", f"Keine Zeit für Übersetzung ({target}), Antwort auf Englisch", session_id, level="warn")
        return subject, body
    notice = next((n for n in NOTICES.values() if n in body), None)
    source = body.replace(notice, _MARK, 1) if notice else body
    try:
        raw = llm.complete(
            "reply_translation",
            [
                {"role": "system", "content": PROMPT.format(name=LANGUAGE_NAMES[target], mark=_MARK)},
                {"role": "user", "content": f"{subject}\n\n{source}"},
            ],
            session_id,
            max_tokens=4000,
            timeout_s=timeout,
        )
        head, _, rest = raw.strip().partition("\n")
        new_subject, new_body = head.strip(), rest.strip("\n")
        if not new_subject or not new_body:
            raise _Rejected("subject or body missing")
        if notice:
            if new_body.count(_MARK) != 1:
                raise _Rejected("tracking invitation marker lost")
            new_body = new_body.replace(_MARK, notice)
        new_subject = customer_subject(new_subject, target)
        new_body = customer_body(new_body, target)
        _check(
            f"{subject}\n{body}",
            f"{new_subject}\n{new_body}",
            new_body.replace(notice, "") if notice else new_body,
            target,
        )
        validate_customer_text(new_body)
        validate_customer_text(new_subject)
    except Exception as exc:
        log.warning("reply translation to %s rejected: %s", target, exc)
        emit(
            "reply_language",
            f"Übersetzung ({target}) verworfen, Antwort auf Englisch: {exc}"[:300],
            session_id,
            level="warn",
        )
        return subject, body
    emit("reply_language", f"Antwort auf {LANGUAGE_NAMES[target]} übersetzt", session_id)
    return new_subject, new_body
