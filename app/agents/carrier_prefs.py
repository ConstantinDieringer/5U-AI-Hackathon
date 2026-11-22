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


# "no objection to X", "we do not mind which airline": a negation that refuses nothing
_NEUTRAL = re.compile(
    r"\b(?:no objection|no problem|nothing against|(?:do not|don't|dont) mind|not mind"
    r"|nichts gegen|kein problem|keine einw\w+)\b",
    re.IGNORECASE,
)
# a negation only reaches the carrier inside its own clause ("we do not mind ..., but we
# prefer X" must not refuse X)
_CLAUSE = re.compile(r"[,;:]|\b(?:but|however|though|aber|jedoch|sondern)\b", re.IGNORECASE)
_GENERIC = {"air", "airways", "airlines", "airline", "cargo", "freight", "lines", "logistics"}


@lru_cache(maxsize=1)
def _names() -> dict[str, str]:
    """Lower-case spellings -> canonical carrier name: the full name, and the name without its
    generic last words ("Zephyr Steppe" for "Zephyr Steppe Cargo") when that is still two
    words and belongs to one carrier only."""
    out: dict[str, str] = {}
    short: dict[str, set[str]] = {}
    for c in carriers():
        out[c.name.lower()] = c.name
        words = c.name.lower().split()
        while len(words) > 2 and words[-1] in _GENERIC:
            words = words[:-1]
            short.setdefault(" ".join(words), set()).add(c.name)
    for spelled, owners in short.items():
        if len(owners) == 1 and spelled not in out:
            out[spelled] = next(iter(owners))
    return out


@lru_cache(maxsize=1)
def _codes() -> dict[str, str]:
    """Two-letter code -> canonical name: the portal's codes, and for carriers the portal does
    not list (round 10820: "ZS") the initials of the first two words, when no other carrier
    shares them."""
    from app.pricing.portal_sync import load_portal_lanes

    out: dict[str, str] = {}
    known = {c.name for c in carriers()}
    try:
        for lane in load_portal_lanes().get("lanes", []):
            for a in lane.get("airlines", []):
                if a.get("name") in known and a.get("code"):
                    out[str(a["code"]).upper()] = a["name"]
    except Exception:  # noqa: BLE001 - without a snapshot the names still work
        out = {}
    coded = set(out.values())
    initials: dict[str, set[str]] = {}
    for c in carriers():
        words = c.name.split()
        if c.kind == "airline" and c.name not in coded and len(words) >= 2:
            initials.setdefault((words[0][0] + words[1][0]).upper(), set()).add(c.name)
    for code, owners in initials.items():
        if len(owners) == 1 and code not in out:
            out[code] = next(iter(owners))
    return out


@lru_cache(maxsize=1)
def _first_words() -> dict[str, str]:
    """Capitalised first word -> canonical name ("Juniper" for "Juniper Cargo Lines"), when
    no other carrier starts with or is spelled as that word."""
    owners: dict[str, set[str]] = {}
    for c in carriers():
        words = c.name.split()
        if len(words) >= 2 and len(words[0]) >= 4 and words[0].lower() not in _GENERIC:
            owners.setdefault(words[0], set()).add(c.name)
    full = {c.name.lower() for c in carriers()}
    return {w: next(iter(n)) for w, n in owners.items() if len(n) == 1 and w.lower() not in full}


# A bare two-letter code is a carrier only in a carrier context: "(ZS)", "ZS flights",
# "airline ZS", "avoid ZS", "not via MD". Otherwise "QA", "CS", "HA" are ordinary
# abbreviations (certificates, customs, paperwork), and a refusal is stored for good.
_CODE_BEFORE = re.compile(
    r"(?:\b(?:airlines?|carriers?|airways|fluggesellschaft|fluglinie|via|with|mit|über|ueber"
    r"|avoid|exclude|excluding|use|using|fly|flying|book|vermeiden|ausschließen)\s+"
    r"(?:the\s+|die\s+)?|\b(?:and|or|und|oder|nor)\s+|,\s*)$",
    re.IGNORECASE,
)
_CODE_AFTER = re.compile(
    r"^\s*(?:airlines?|carriers?|airways|flights?|cargo|flug\w*|fluggesellschaft)\b",
    re.IGNORECASE,
)


def _code_in_carrier_context(sentence: str, start: int, end: int) -> bool:
    if sentence[start - 1 : start] == "(" and sentence[end : end + 1] == ")":
        return True
    before, after = sentence[max(0, start - 40) : start], sentence[end : end + 30]
    if _CODE_AFTER.search(after):
        return True
    if not _CODE_BEFORE.search(before):
        return False
    joint = re.search(r"(?:\b(?:and|or|und|oder|nor)\s+|,\s*)$", before, re.IGNORECASE)
    if joint:
        # a list item counts only right after a carrier code in context: "avoid ZS, MD or QA"
        head = start - (len(before) - joint.start())
        prev = re.search(r"\b[A-Z]{2}\b\s*$", sentence[:head])
        return bool(prev) and _code_in_carrier_context(sentence, prev.start(), prev.start() + 2)
    return True


def _mentions(sentence: str) -> list[tuple[int, int, str]]:
    """(start, end, canonical name) of every carrier named in the sentence, longest spelling
    first so "Zephyr Steppe Cargo" is not also counted as "Zephyr Steppe"."""
    low = sentence.lower()
    hits: list[tuple[int, int, str]] = []
    taken: set[str] = set()
    for spelled, name in sorted(_names().items(), key=lambda kv: -len(kv[0])):
        i = low.find(spelled)
        if i >= 0 and name not in taken:
            hits.append((i, i + len(spelled), name))
            taken.add(name)
    for word, name in _first_words().items():  # "Please avoid Juniper." (capitalised only)
        m = re.search(rf"\b{re.escape(word)}\b", sentence)
        if m and name not in taken:
            hits.append((m.start(), m.end(), name))
            taken.add(name)
    for m in re.finditer(r"\b[A-Z]{2}\b", sentence):  # codes in capitals only
        if re.search(r",\s*$", sentence[: m.start()]) and sentence[m.end() : m.end() + 1] == ")":
            continue  # "(Miami, US)": a country code, not a carrier
        if not _code_in_carrier_context(sentence, m.start(), m.end()):
            continue  # "No CS required", "QA certificate", "HA paperwork": not airlines
        name = _codes().get(m.group(0))
        if name and name not in taken:
            hits.append((m.start(), m.end(), name))
            taken.add(name)
    return hits


def parse(text: str) -> list[tuple[str, str]]:
    """[(carrier name, 'exclude'|'prefer')] for carriers named in the text."""
    found: dict[str, str] = {}
    for sentence in re.split(r"(?<=[.!?\n])\s+", text or ""):
        mentions = _mentions(sentence)
        for start, end, name in mentions:
            # the clause the carrier stands in: cut at the last clause break before it, unless
            # only other carriers stand in between ("do not use A, B or C" stays one clause)
            base = max(0, start - 80)
            before = sentence[base:start]
            cut = 0
            for brk in _CLAUSE.finditer(before):
                gap = _strip_carriers(before[brk.end() :], sentence, mentions)
                if re.search(r"[a-zäöü]{3,}", gap):
                    cut = brk.end()
            clause = _NEUTRAL.sub(" ", before[cut:])
            after = sentence[end : end + 40]
            if _NEG.search(clause) or re.search(
                r"\b(?:is not|are not|not acceptable|unacceptable|nicht|ausgeschlossen)\b",
                after,
                re.IGNORECASE,
            ):
                found[name] = "exclude"  # a refusal beats a preference in the same mail
            elif _POS.search(clause) and found.get(name) != "exclude":
                found[name] = "prefer"
    return sorted(found.items())


def _strip_carriers(gap: str, sentence: str, mentions: list[tuple[int, int, str]]) -> str:
    """The text between a clause break and the carrier, without other carrier names and the
    words that join a list, so "A, B or C" leaves nothing and ", but we prefer" leaves words."""
    out = gap.lower()
    for s, e, _ in mentions:
        out = out.replace(sentence[s:e].lower(), " ")
    return re.sub(r"\b(?:or|and|nor|oder|und|noch|the|with|mit)\b|\([A-Z]{2}\)", " ", out)


def remember(customer: str, text: str, session_id: int | None = None) -> list[tuple[str, str]]:
    prefs = parse(text)
    for name, kind in prefs:
        if db.add_carrier_pref(customer, name, kind, session_id):
            label = "nicht gewünscht" if kind == "exclude" else "bevorzugt"
            db.emit("carrier_pref", f"{name}: vom Kunden {label} (gespeichert)", session_id)
    return prefs


def excluded(customer: str) -> set[str]:
    return {n.lower() for n, k in db.carrier_prefs(customer) if k == "exclude"}


def preferred(customer: str) -> set[str]:
    return {n.lower() for n, k in db.carrier_prefs(customer) if k == "prefer"}


def mentions_excluded(text: str, names: list[str]) -> bool:
    """Customer offers should name the selected carrier and acknowledge exclusions generically."""
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) for name in names)


def backfill() -> int:
    """Read every stored customer mail once at startup, so refusals made before this
    module existed (round 10820, Meyer: Zephyr Steppe Cargo) hold for the next request."""
    from app.mail.content import current_message
    from app.mail.router import address

    n = 0
    rows = (
        db.conn()
        .execute(
            "SELECT id, session_id, sender, body FROM mails WHERE direction = 'in'"
            " AND category IN ('rfq', 'customer')"
        )
        .fetchall()
    )
    for mid, sid, sender, body in rows:
        for name, kind in parse(current_message(body or "").text):
            if db.add_carrier_pref(address(sender or ""), name, kind, sid or mid):
                n += 1
    return n
