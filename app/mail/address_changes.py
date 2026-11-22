"""Address changes outside the fixed-contact challenge simulation.

SIMULATION_STATIC_CONTACTS (on by default) prevents recording address announcements and
using historical mappings. In the simulation such notices are phishing, even from a known
party. The behavior below is available only when that simulation policy is explicitly off.

A mail sent *exactly from* a verified address (carrier list, rate desk, tracking desk, a known
customer, or an address verified this way before) that announces a new address makes the new
address verified too: it is no longer a lookalike, inbound mail from it counts as the original
party, and outbound mail to that party goes to the new address.

A lookalike sender can never announce a change: it is quarantined by the phishing gate first,
and only an exact verified sender is accepted here. Deterministic, no LLM call.
"""

from __future__ import annotations

import logging
import re

from app.config import settings
from app.models import Mail

log = logging.getLogger(__name__)

_FLAGS = re.IGNORECASE
_CHANGE = re.compile(
    r"\b(?:new|changed|updated|different)\s+(?:e-?mail\s+)?(?:address|contact address|mailbox)\b|"
    r"\b(?:changed|moved|updated|switched)\s+(?:our|my)\s+(?:e-?mail\s+)?(?:address|mailbox)\b|"
    r"\b(?:address|mailbox)\s+(?:has|have)\s+changed\b|"
    r"\bplease\s+(?:use|write\s+to|contact\s+us\s+at|send\s+(?:all\s+|any\s+|future\s+)?"
    r"(?:mails?|e-?mails?|requests?|correspondence)\s+to)\b|"
    r"\bneue[nrs]?\s+(?:e-?mail-?)?(?:adresse|postfach)\b|"
    r"\b(?:e-?mail-?)?adresse\s+(?:hat\s+sich\s+ge(?:ä|ae)ndert|ge(?:ä|ae)ndert|lautet\s+jetzt)\b|"
    r"\bbitte\s+(?:schreiben|senden|wenden)\s+sie\s+(?:sich\s+)?(?:ab\s+sofort\s+|k(?:ü|ue)nftig\s+)?"
    r"(?:an|uns\s+an)\b",
    _FLAGS,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)


def announced_address(mail: Mail, sender: str, known: frozenset[str]) -> str | None:
    """The one new address a change notice in `mail` points to, or None.
    Another party's known address is never accepted as someone's new address."""
    text = f"{mail.subject}\n{mail.body}"
    if not _CHANGE.search(text):
        return None
    found = {a.lower().rstrip(".") for a in _EMAIL.findall(text)}
    found -= {sender, settings.team_mailbox}
    if len(found) != 1:
        return None  # none or ambiguous: a person decides
    [new] = found
    if new in known:
        return None
    from app.mail.screening import lookalike

    others = known - {sender}
    return None if lookalike(new, others) else new  # never a new face for another party


def verified_addresses() -> frozenset[str]:
    """Carrier list, desks, known customers and every verified changed address."""
    from app import db
    from app.mail.screening import address, fixed_addresses

    customers = {address(c) for c in db.known_customers()}
    changed = (
        frozenset() if settings.simulation_static_contacts else frozenset(db.address_changes())
    )
    return fixed_addresses() | frozenset(customers) | changed


def record(mail: Mail, verified: frozenset[str], session_id: int | None = None) -> str | None:
    """Call only for a mail the phishing gate let through, with `verified` taken before this
    mail was stored (it must not verify its own sender). Returns the newly verified address."""
    if settings.simulation_static_contacts:
        return None
    from app import db
    from app.mail.screening import address

    sender = address(mail.sender)
    if sender not in verified:
        return None
    new = announced_address(mail, sender, verified)
    if new is None:
        return None
    original = db.address_change_add(sender, new, mail.id)
    log.warning("address change: %s (verified as %s) now writes from %s", sender, original, new)
    db.emit(
        "address_change",
        f"Adressänderung von {sender}: {new} gilt jetzt als verifiziert (für {original})",
        session_id,
        level="warn",
        old_address=original,
        new_address=new,
        mail_id=mail.id,
    )
    return new


def canonical(addr: str) -> str:
    """The original verified address behind a changed address, else `addr` itself."""
    if settings.simulation_static_contacts:
        return addr
    try:
        from app import db

        return db.address_changes().get(addr, addr)
    except Exception:  # no database (pure pricing/router use): no changes known
        log.debug("address change lookup failed for %s", addr, exc_info=True)
        return addr


def current(addr: str) -> str:
    """Where mail to the party behind `addr` goes now: its latest announced address."""
    if settings.simulation_static_contacts:
        return addr
    try:
        from app import db

        changes = db.address_changes()
    except Exception:
        log.exception("address change lookup failed for %s", addr)
        return addr
    original = changes.get(addr, addr)
    latest = [new for new, old in changes.items() if old == original]
    return latest[-1] if latest else addr
