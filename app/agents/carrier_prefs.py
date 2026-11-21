"""Customer carrier preferences: airlines or truckers a customer refuses or asks for.

Round 10820 (reason excluded_airline): "Please do not offer Zephyr Steppe Cargo (ZS) for
this shipment or for any of our future inquiries." We had asked Zephyr for a bid. Now:
  - parse(text)  finds named carriers in the current mail and classifies each mention as
                 'exclude' or 'prefer' from the words around it (no LLM)
  - remember()   stores them per customer in the database (they hold for future requests)
  - excluded/preferred lookups used by the RFQ agent and the emergency draft: an excluded
    carrier is never asked for a bid and never quoted; a preferred one wins when it has a
    price
"""

from __future__ import annotations

import re
from functools import lru_cache

from app import db
from app.pricing.reference import carriers

_NEG = re.compile(
    r"\b(?:do not|don't|dont|not|never|avoid|exclude|excluding|without|no|except|other than"
    r"|nicht|kein(?:e|en)?|ohne|außer|ausser|vermeiden)\b",
    re.IGNORECASE,
)
_POS = re.compile(
    r"\b(?:prefer(?:red|ably)?|please use|would like|we'd like|only with|book with|go with"
    r"|bevorzug\w*|gerne mit|bitte mit|nur mit)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _names() -> dict[str, str]:
    """Lower-case spellings (full name, name without 'Airways/Cargo/...', 2-letter code in
    brackets is handled separately) -> canonical carrier name."""
    out: dict[str, str] = {}
    for c in carriers():
        out[c.name.lower()] = c.name
    return out


def parse(text: str) -> list[tuple[str, str]]:
    """[(carrier name, 'exclude'|'prefer')] for carriers named in the text."""
    found: dict[str, str] = {}
    for sentence in re.split(r"(?<=[.!?\n])\s+", text or ""):
        low = sentence.lower()
        for spelled, name in _names().items():
            i = low.find(spelled)
            if i < 0:
                continue
            before = sentence[max(0, i - 80) : i]
            after = sentence[i + len(spelled) : i + len(spelled) + 40]
            if _NEG.search(before) or re.search(
                r"\b(?:is not|are not|not acceptable|unacceptable|nicht|ausgeschlossen)\b",
                after,
                re.IGNORECASE,
            ):
                found[name] = "exclude"  # a refusal beats a preference in the same mail
            elif _POS.search(before) and found.get(name) != "exclude":
                found[name] = "prefer"
    return sorted(found.items())


def remember(
    customer: str, text: str, session_id: int | None = None
) -> list[tuple[str, str]]:
    prefs = parse(text)
    for name, kind in prefs:
        if db.add_carrier_pref(customer, name, kind, session_id):
            label = "nicht gewünscht" if kind == "exclude" else "bevorzugt"
            db.emit(
                "carrier_pref", f"{name}: vom Kunden {label} (gespeichert)", session_id
            )
    return prefs


def excluded(customer: str) -> set[str]:
    return {n.lower() for n, k in db.carrier_prefs(customer) if k == "exclude"}


def preferred(customer: str) -> set[str]:
    return {n.lower() for n, k in db.carrier_prefs(customer) if k == "prefer"}


def backfill() -> int:
    """Read every stored customer mail once at startup, so refusals made before this
    module existed (round 10820, Meyer: Zephyr Steppe Cargo) hold for the next request."""
    from app.mail.content import current_message
    from app.mail.router import address

    n = 0
    rows = db.conn().execute(
        "SELECT id, session_id, sender, body FROM mails WHERE direction = 'in'"
        " AND category IN ('rfq', 'customer')"
    ).fetchall()
    for mid, sid, sender, body in rows:
        for name, kind in parse(current_message(body or "").text):
            if db.add_carrier_pref(address(sender or ""), name, kind, sid or mid):
                n += 1
    return n
