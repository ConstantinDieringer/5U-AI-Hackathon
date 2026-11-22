"""Carrier bids by email: ask truckers (door-to-port pickup) and email-only airlines for a
price, read their replies, and hand the offers to the waiting RFQ agent.

No LLM: replies are read with regexes. A reply whose price cannot be read is logged and
ignored (the RFQ is then quoted from sheet + portal).

Mapping a reply to its RFQ: db.ingest_mail maps threaded replies and subject-quoting replies
to the session; as a fallback the reference tag `[AP-<session>]` in subject or body is used.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from app import config, db, hackloop
from app.mail import documents, links
from app.mail.router import address
from app.mail.style import SIGNATURE
from app.models import Mail, RateOption, Rfq
from app.pricing import served_lanes
from app.pricing.amounts import AMOUNT as _AMOUNT
from app.pricing.amounts import MONEY as _MONEY
from app.pricing.amounts import SPACES as _SPACES
from app.pricing.amounts import mail_total, parse_decimal
from app.pricing.fx import CURRENCY as _CUR
from app.pricing.fx import currency_code
from app.pricing.lanes import airlines_for
from app.pricing.portal_sync import next_break
from app.pricing.pricing import chargeable_kg, money, to_minor
from app.pricing.reference import Carrier, carrier_by_email

log = logging.getLogger(__name__)

TAG = re.compile(r"\[AP-(\d+)\]")
_BILLED = re.compile(r"billed at (\d+(?:\.\d+)?) kg", re.IGNORECASE)
_DAYS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:business\s+|working\s+|calendar\s+)?(?:days?|tage?n?)\b",
    re.IGNORECASE,
)
_HOURS = re.compile(r"(\d+)\s*(?:h|hrs?|hours?|stunden|std)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Offer:
    carrier: Carrier
    total_eur: Decimal
    transit_days: float | None
    mail_id: int
    original_amount: Decimal | None = None
    currency: str = "EUR"
    fx_rate: str | None = None
    fx_source: str | None = None
    fx_valid_until: str | None = None
    timing_kind: str = "unspecified"
    billed_kg: float | None = None  # set for a reply to our next-weight-break request


@dataclass
class _Request:
    asked: dict[str, Carrier] = field(default_factory=dict)  # email -> carrier
    offers: list[Offer] = field(default_factory=list)
    declined: set[str] = field(default_factory=set)  # emails that said "we do not serve"
    # emails whose reply came but could not be priced: answered, nothing more to wait for
    unreadable: set[str] = field(default_factory=set)
    last_answer_at: float = 0.0  # monotonic time of the latest offer, decline or unreadable reply


_open: dict[int, _Request] = {}
_cond = threading.Condition()
_transcriptions: dict[int, int] = {}  # session -> model calls spent on bid attachments
MAX_BID_TRANSCRIPTIONS = 5  # one per carrier at most (MAX_BID_CARRIERS default 5)


# ---------------------------------------------------------------- bid slots
# A mail with several shipments (board reason one_of_two) prices each shipment with its own
# bids. The first shipment uses the session id as its slot; shipment k >= 1 uses the slot
# -(session * 10 + k). Slots only key the in-memory waits; db rows and events always carry
# the real session, and replies find their slot through the bid request they answer.


def slot_for(session_id: int, index: int) -> int:
    """Bid slot of shipment `index` (0 = the session's own request)."""
    if not 0 <= index <= 9:
        raise ValueError(f"shipment index out of range: {index}")
    return session_id if index == 0 else -(session_id * 10 + index)


def owner(slot: int) -> int:
    """Session a bid slot belongs to."""
    return slot if slot >= 0 else (-slot) // 10


def _slot_of_reply(mail: Mail, session_id: int) -> int:
    """Slot of the bid request this reply answers (its parent mail), else the session."""
    parents = [int(x) for x in (mail.references or [])[-1:]]
    raw_parent = (mail.raw or {}).get("in_reply_to")
    if isinstance(raw_parent, int):
        parents.insert(0, raw_parent)
    for parent in parents:
        stored = db.kv_get(f"bid.slot.{parent}", "")
        if stored:
            slot = int(stored)
            if owner(slot) == session_id:
                return slot
    return session_id


# ---------------------------------------------------------------- parsing


def _number(raw: str) -> Decimal | None:
    return parse_decimal(raw)


def parse_price(text: str) -> tuple[Decimal, str] | None:
    """The one all-in total a mail states, with its currency (see amounts.mail_total):
    per-unit, optional, waiting and insured-value amounts never count; several different
    totals give None (bid_unreadable), never the largest."""
    return mail_total(text)


def parse_transit_days(text: str) -> float | None:
    m = _DAYS.search(text)
    if m:
        return float(m.group(1).replace(",", "."))
    h = _HOURS.search(text)
    return int(h.group(1)) / 24 if h else None


# Labels of a document's final amount. Optional words between label and amount (amount, due,
# (EUR), incl. fuel, all-in, netto ...) may not contain digits. Sub-totals never match.
_TOTAL_LABEL = re.compile(
    r"^(?:(?:grand|net|overall|final|end)\s*[- ]?\s*)?"
    r"(?:total|sum|summe|gesamt(?:preis|betrag|summe|kosten)?|endpreis|endbetrag|"
    r"amount\s+due|to\s+pay|zu\s+zahlen)\b(?P<rest>[^\d]{0,60}?)[:=|]?\s*"
    rf"(?=(?:{_CUR}\s*)?\d|$)",
    re.IGNORECASE,
)
_PRICE_LABEL = re.compile(
    r"^(?:(?:our|unser(?:e|)?|the|der|das)\s+)?(?:all[- ]?in\s+|total\s+)?"
    r"(?:price|preis|rate|offer|angebot|quote|quotation|charge)\b"
    r"(?P<rest>[^\d]{0,60}?)(?:\bis\b|\bist\b|\bbeträgt\b|\bliegt\s+bei\b|:|\||=)\s*",
    re.IGNORECASE,
)
_REFERENCE = re.compile(
    r"\b(?:no|nr|number|nummer|ref(?:erence|erenz)?|id|date|datum|valid|gültig)\b|#",
    re.IGNORECASE,
)
_NOT_TOTAL = re.compile(
    r"sub[- ]?total|zwischen|\bweight\b|gewicht|volume|volumen|\bpieces?\b|stück|colli|"
    r"\bkg\b|cbm|\bm3\b",
    re.IGNORECASE,
)
_PER_UNIT = re.compile(
    r"\bper\b|\bpro\b|\bje\b|/\s*(?:kg|h|hr|hour|std|stunde|piece|pc|shipment)\b|\beach\b",
    re.IGNORECASE,
)
_EXCLUDED = re.compile(
    r"not\s+(?:part|included|incl)|nicht\s+(?:im|enthalten|inkl)|exclud|optional|extra|"
    r"additional|zusätzlich|zuzüglich|\bzzgl|surcharge\s+if|if\s+(?:required|applicable|needed)|"
    r"falls|wait(?:ing)?|warte|storage|lager|demurrage|cancell?ation|storno|discount|rabatt",
    re.IGNORECASE,
)


def _cells(line: str) -> list[str]:
    """Table cells of one line; empty and repeated (merged) cells dropped."""
    out: list[str] = []
    for cell in (c.strip() for c in line.split("|")):
        if cell and (not out or out[-1] != cell):
            out.append(cell)
    return out


def _money_at(text: str, currency: str | None) -> tuple[Decimal, str] | None:
    """The amount that `text` starts with (currency before or after it, or the given one)."""
    text = text.strip()
    m = _AMOUNT.match(text)
    if m:
        raw = m.group("num1") or m.group("num2") or ""
        cur = (m.group("cur1") or m.group("cur2") or "").upper()
        rest = text[m.end() :]
    else:
        n = re.match(_MONEY, text)
        if not n or not currency:
            return None
        raw, cur, rest = n.group(0), currency, text[n.end() :]
    if re.match(r"\s*(?:kg|cbm|m3|pcs|pieces?|h\b|hours?|days?|tage?|%)", rest, re.IGNORECASE):
        return None  # a weight, duration or percentage, not money
    num = _number(raw)
    if num is None or num <= 0:
        return None
    return money(num), currency_code(cur)


def _label_currency(rest: str) -> str | None:
    found = {currency_code(c) for c in re.findall(_CUR, rest, re.IGNORECASE)}
    return next(iter(found)) if len(found) == 1 else None


def document_price(text: str) -> tuple[Decimal, str] | None:
    """An explicit total, not the largest component or an incidental surcharge.

    Tier 1: lines labelled as the total (Total, Total amount, Total (EUR), Grand total,
    Gesamtpreis (netto), Summe, Total due, ...). Tier 2, only without any total line: lines
    labelled as the price/offer. Per-unit rates (per kg, /h), excluded or optional charges
    (waiting time, not included, extra, surcharge if) and sub-totals never count. The value
    stands after the label or alone in the next line. Its currency is written with it, in
    the label ("Total (EUR)"), in a column header, or is the document's only currency.
    Several different amounts in the deciding tier: no price (never guess)."""
    lines = [" | ".join(_cells(line)) for line in text.translate(_SPACES).splitlines()]
    lines = [line for line in lines if line]
    found = {
        c for m in re.findall(_CUR, "\n".join(lines), re.IGNORECASE) if (c := currency_code(m))
    }
    only = next(iter(found)) if len(found) == 1 else None
    header: str | None = None
    tiers: tuple[set, set] = (set(), set())
    for index, line in enumerate(lines):
        if re.search(rf"\({_CUR}\)|\bin\s+{_CUR}", line, re.IGNORECASE) and not re.search(
            r"\d", re.sub(rf"\({_CUR}\)", "", line, flags=re.IGNORECASE)
        ):
            header = _label_currency(line)  # a column header such as "Amount (EUR)"
        for tier, pattern in enumerate((_TOTAL_LABEL, _PRICE_LABEL)):
            label = pattern.match(line)
            if not label:
                continue
            rest = label.group("rest") or ""
            context = line[: label.end()]
            if tier == 0 and _NOT_TOTAL.search(context):
                break
            if _PER_UNIT.search(line) or _EXCLUDED.search(line):
                break
            if tier == 1 and _REFERENCE.search(context):
                break  # "Offer No: 12345", "Quotation number: 4711"
            currency = _label_currency(rest) or header or only
            tail = line[label.end() :].strip().lstrip("|:= ").strip()
            if not tail and index + 1 < len(lines):
                tail = lines[index + 1].strip()  # value in the next row
                if not re.fullmatch(rf"{_CUR}?\s*{_MONEY}\s*{_CUR}?", tail, re.IGNORECASE):
                    break
            # a price line (tier 2) needs its currency written next to the amount
            amount = _money_at(tail, currency if tier == 0 else None)
            if amount is not None:
                tiers[tier].add(amount)
            break
    deciding = tiers[0] or tiers[1]
    return next(iter(deciding)) if len(deciding) == 1 else None


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "twenty-four": 24,
    "forty-eight": 48, "eins": 1, "ein": 1, "einem": 1, "einen": 1, "zwei": 2, "drei": 3,
    "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "zwölf": 12, "vierundzwanzig": 24, "achtundvierzig": 48,
}  # fmt: skip
_WORD_DURATION = re.compile(
    r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\s+"
    r"(?=(?:business\s+|working\s+|calendar\s+)?(?:days?|tage?n?|h\b|hrs?|hours?|stunden|std))",
    re.IGNORECASE,
)
_TIMING_LINE = re.compile(
    r"transit|lead\s*time|collect|pick\s*-?up|abhol|laufzeit|transitzeit|fahrzeit|"
    r"transportdauer|delivery|lieferzeit|flight|flug|within|innerhalb|departure|ready",
    re.IGNORECASE,
)
_NOT_TIMING = re.compile(
    r"valid|gültig|payment|zahlung|storage|lager|free\s+time|wait|warte|cancel|storno",
    re.IGNORECASE,
)


def document_timing(text: str) -> str:
    """The line holding the offer's duration (transit or collection lead), with number words
    written as digits; never a validity, payment or storage period. Empty if none."""
    lines = [_WORD_DURATION.sub(lambda m: f"{_WORD_NUMBERS[m.group(1).lower()]} ", line)
             for line in text.translate(_SPACES).splitlines()]  # fmt: skip
    timed = [line for line in lines if (_DAYS.search(line) or _HOURS.search(line))]
    usable = [line for line in timed if not _NOT_TIMING.search(line)]
    preferred = [line for line in usable if _TIMING_LINE.search(line)]
    return (preferred or usable or [""])[0]


_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "eins": 1, "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6,
    "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12,
}  # fmt: skip
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}  # fmt: skip
_NUMBER_WORD = re.compile(
    rf"\b(?:(?P<tens>{'|'.join(_TENS)})(?:[- ](?P<unit>{'|'.join(_UNITS)}))?"
    rf"|(?P<only>{'|'.join(sorted(_UNITS, key=len, reverse=True))}))\b",
    re.IGNORECASE,
)
_SPOKEN_CENTS = re.compile(
    r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:euros?|EUR)\s*(?:and|und)?\s*(\d{1,2})\s*(?:cents?|ct)\b",
    re.IGNORECASE,
)
_SPOKEN_EURO = re.compile(r"(\d[\d,.]*\d|\d)\s*(?:euros?)\b", re.IGNORECASE)
_NOT_THE_PRICE = re.compile(
    r"each hour|per hour|an hour|pro stunde|je stunde|waiting|wartezeit|not part|"
    r"nicht (?:im|teil|enthalten)",
    re.IGNORECASE,
)
_SPOKEN_TOTAL = re.compile(r"all[- ]?in|total|gesamt|insgesamt|in summe", re.IGNORECASE)


def normalize_spoken(text: str) -> str:
    """A voice transcript in the form the bid parsers read: number words as digits ("four
    hours" -> "4 hours"), "358 euros and 64 cents" -> "358.64 EUR", "115 euros" -> "115 EUR"."""

    def word(m: re.Match) -> str:
        if m.group("only"):
            return str(_UNITS[m.group("only").lower()])
        unit = _UNITS[m.group("unit").lower()] if m.group("unit") else 0
        return str(_TENS[m.group("tens").lower()] + unit)

    text = _NUMBER_WORD.sub(word, text)
    text = _SPOKEN_CENTS.sub(lambda m: f"{m.group(1)}.{int(m.group(2)):02d} EUR", text)
    return _SPOKEN_EURO.sub(lambda m: f"{m.group(1)} EUR", text)


def spoken_price(text: str) -> tuple[Decimal, str] | None:
    """The all-in price of a (normalized) voice offer. Sentences naming the total or all-in
    win; amounts for waiting time or "not part of" are never the price. Without a total
    sentence, the one amount said is the price; several different amounts give None."""
    totals: set[tuple[Decimal, str]] = set()
    others: set[tuple[Decimal, str]] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if _NOT_THE_PRICE.search(sentence):
            continue
        for m in _AMOUNT.finditer(sentence):
            num = _number(m.group("num1") or m.group("num2") or "")
            cur = (m.group("cur1") or m.group("cur2") or "").upper()
            if num is None or num <= 0:
                continue
            found = (money(num), currency_code(cur))
            (totals if _SPOKEN_TOTAL.search(sentence) else others).add(found)
    pick = totals or others
    return next(iter(pick)) if len(pick) == 1 else None


def _bid_evidence(
    body: str, files: list[tuple[str, str]], spoken: list[tuple[str, str]]
) -> tuple[str, tuple[Decimal, str] | None, str | None]:
    """(evidence text, price, issue). `files` are document texts (PDF, Word, Excel, scans):
    only an explicit, unique total counts there. `spoken` are voice-message transcripts:
    read like mail text. Every source that names a price must agree with the others."""
    if not files and not spoken:
        return body, parse_price(body), None
    evidence: list[tuple[str, tuple[Decimal, str]]] = []
    for name, text in files:
        price = document_price(text)
        if price is None:
            return body, None, f"{name}: no unambiguous explicit total"
        evidence.append((document_timing(text), price))
    for name, text in spoken:
        text = normalize_spoken(text)
        price = spoken_price(text)
        if price is None:
            return body, None, f"{name}: no price in voice message"
        evidence.append((text, price))
    prices = {price for _, price in evidence}
    body_price = parse_price(body)
    if len(prices) != 1 or (body_price is not None and body_price not in prices):
        return body, None, "conflicting body or attachment totals"
    timings = {(parse_transit_days(t), timing_kind(t)) for t, _ in evidence}
    if len(timings) != 1:
        return body, None, "conflicting attachment durations"
    return evidence[0][0], evidence[0][1], None


def timing_kind(text: str) -> str:
    """Interpret only the clause containing the same duration used by the bid parser."""
    match = _DAYS.search(text) or _HOURS.search(text)
    if not match:
        return "unspecified"
    start = max(text.rfind(mark, 0, match.start()) for mark in ("\n", ".", "!", "?")) + 1
    end = re.search(r"[\n.!?]", text[match.end() :])
    stop = match.end() + end.start() if end else len(text)
    clause = text[start:stop].lower()
    road = bool(re.search(r"transit time|driving|fahrzeit|transportdauer", clause))
    collection = bool(re.search(r"pickup|pick up|collect|abhol|driver|fahrer", clause)) and not road
    return "road_transit" if road else "collection_lead" if collection else "unspecified"


# ---------------------------------------------------------------- requests


def _subject(session_id: int, rfq: Rfq, origin: str, kind: str, billed: float | None = None) -> str:
    load = f"{rfq.weight_kg:g} kg / {rfq.volume_cbm:g} cbm"
    if kind == "trucker":
        return f"[AP-{session_id}] Pickup {rfq.pickup_label} to {origin} airport, {load}"
    extra = f", billed at {billed:g} kg" if billed else ""
    return f"[AP-{session_id}] Air freight {origin} to {rfq.destination}, {load}{extra}"


def _next_break(carrier: Carrier, rfq: Rfq, origin: str) -> float | None:
    """BID_NEXT_BREAK: the airline's next portal weight break above the chargeable weight. The
    scorer prices the cheaper of exact weight and next break (round 27: Meridian billed 859 kg
    at 3.35 €/kg, the board's best was 1000 kg at 2.84 €/kg). Email-only airlines have no
    known brackets."""
    if carrier.kind != "airline":
        return None
    code = next(
        (a.code for a in airlines_for(origin, rfq.destination) if a.email == carrier.email),
        None,
    )
    return next_break(code, chargeable_kg(rfq.weight_kg, rfq.volume_cbm)) if code else None


def _body(rfq: Rfq, origin: str, kind: str, billed: float | None = None) -> str:
    pieces = f"{rfq.pieces} pieces, " if rfq.pieces else ""
    ready = f"Ready for pickup: {rfq.ready_date}.\n" if rfq.ready_date else ""
    if kind == "trucker":
        site = f"postal code {rfq.pickup_zip}" if rfq.pickup_zip else f"the {rfq.pickup_city} area"
        what = f"Please quote road transport from {site} (Germany) to {origin} airport."
    else:
        what = f"Please quote air freight from {origin} to {rfq.destination}."
    return (
        f"Hello,\n\n{what}\n"
        f"Shipment: {pieces}{rfq.weight_kg:g} kg gross, {rfq.volume_cbm:g} cbm.\n{ready}"
        + (f"Please quote it billed at {billed:g} kg, your next weight break.\n" if billed else "")
        + "\n"
        "Please reply with your all-in price in € and the transit time in days.\n\n"
        f"{SIGNATURE}\n"
    )


def request(
    session_id: int, rfq: Rfq, origin: str, carriers: list[Carrier], slot: int | None = None
) -> int:
    """Send one bid request per carrier and register them. Returns how many were sent.
    `slot` (see slot_for) keeps a further shipment of the same mail apart from the first."""
    slot = session_id if slot is None else slot
    sent = 0
    if slot == session_id:
        already_sent = db.bid_request_recipients(session_id)
    else:
        already_sent = set(json.loads(db.kv_get(f"bid.sent.{slot}", "[]")))
    with _cond:
        req = _open.setdefault(slot, _Request())
    before = _resume(session_id, req) if slot == session_id else set()
    for c in carriers:
        if c.email in before:
            continue  # asked before a restart: its reply is replayed, no second mail
        if db.is_manual(session_id) or db.is_paused(session_id):
            break
        with _cond:
            if c.email in req.asked:
                continue
            if c.email.lower() in already_sent:
                req.asked[c.email] = c
                continue
        billing = [None]
        if config.settings.bid_next_break and (nb := _next_break(c, rfq, origin)):
            billing.append(nb)
        for billed in billing:
            subject, body = (
                _subject(session_id, rfq, origin, c.kind, billed)
                + (
                    f" [RV-{state['request_version']}]"
                    if (state := db.reply_workflow(session_id)) and state["request_version"] > 1
                    else ""
                ),
                _body(rfq, origin, c.kind, billed),
            )
            try:
                out = hackloop.send(c.email, subject, body)
            except Exception as e:  # noqa: BLE001 - one carrier must not stop the others
                log.warning("bid request to %s failed: %s", c.email, e)
                continue
            db.record_outgoing(
                mail_id=int(out["id"]),
                recipient=c.email,
                subject=subject,
                body=body,
                category="bid_request",
                session_id=session_id,
                raw=out,
            )
            if slot != session_id:
                db.kv_set(f"bid.slot.{int(out['id'])}", str(slot))
                already_sent.add(c.email.lower())
                db.kv_set(f"bid.sent.{slot}", json.dumps(sorted(already_sent)))
            with _cond:
                req.asked[c.email] = c
            sent += 1
    if sent:
        db.emit("bid_request", f"Gebote angefragt bei {sent} Carrier(n)", session_id)
    return sent


def _resume(session_id: int, req: _Request) -> set[str]:
    """After a restart: re-register carriers we already asked for this session (from the db)
    and replay the replies they already sent. Returns their emails (empty on a fresh RFQ)."""
    s = db.get_session(session_id) or {}
    floor = db.reply_revision_floor(session_id)
    mails = [m for m in s.get("mails", []) if m["id"] > floor]
    asked: set[str] = set()
    for m in mails:
        if m["direction"] == "out" and m["category"] == "bid_request":
            c = carrier_by_email(str(m["recipient"] or ""))
            if c is not None:
                asked.add(c.email)
                with _cond:
                    req.asked[c.email] = c
    for m in mails:
        if m["direction"] == "in" and m["category"] == "carrier_bid":
            on_bid_mail(
                Mail(int(m["id"]), session_id, str(m["sender"] or ""), "", str(m["subject"] or ""),
                     str(m["body"] or ""), str(m["received_at"] or ""),
                     references=[m["in_reply_to"]] if m.get("in_reply_to") else []),
                session_id,
            )  # fmt: skip
    return asked


# ---------------------------------------------------------------- replies


def session_of(mail: Mail, session_id: int | None) -> int | None:
    if session_id is not None:
        return session_id
    m = TAG.search(mail.subject or "") or TAG.search(mail.body or "")
    return int(m.group(1)) if m else None


def _attachment_paths(mail: Mail, sid: int | None = None) -> list[Path]:
    """The bid's saved attachments plus files behind safety-checked links (app/mail/links.py).
    The poller already downloaded the attachments; a mail rebuilt from the db after a restart
    has no attachment list, so its inbox folder is read instead (saved link files included)."""
    if mail.attachments:
        from app.mail.poller import download_attachments

        paths = download_attachments(mail)
        return paths + [
            p
            for p in links.linked_files(mail, owner(sid) if sid is not None else None)
            if p not in paths
        ]
    saved = documents.inbox_files(config.settings.inbox_dir / str(mail.id))
    return saved + [
        p
        for p in links.linked_files(mail, owner(sid) if sid is not None else None)
        if p not in saved
    ]


def _spawn(fn, *args) -> None:  # tests replace this with a direct call
    threading.Thread(target=fn, args=args, name=f"bid-doc-{args[0].id}", daemon=True).start()


def _unreadable(carrier: Carrier, sid: int, issue: str, kind: str = "bid_unreadable") -> None:
    """The carrier answered but gave no usable price: log it and stop waiting for them
    (rounds 63/64 waited out the bid window for replies that would never become offers)."""
    with _cond:
        req = _open.setdefault(sid, _Request())
        req.unreadable.add(carrier.email)
        req.last_answer_at = time.monotonic()
        _cond.notify_all()
    db.emit(kind, f"Gebot von {carrier.name}: {issue}"[:300], owner(sid), level="warn")


def _usable_voice(text: str) -> bool:
    return (
        spoken_price(normalize_spoken(text)) is not None
        or served_lanes.parse_refusal(text) is not None
    )


def on_bid_mail(mail: Mail, session_id: int | None) -> None:
    """Called by the poller for every CARRIER_BID mail. The offer may be in the mail text or
    in attachments: documents are read in code (app/mail/documents.py), voice messages and
    scans are transcribed in a separate thread."""
    sid = session_of(mail, session_id)
    carrier = carrier_by_email(address(mail.sender))
    if sid is None or carrier is None:
        log.warning("bid mail %s: no session or unknown carrier, ignored", mail.id)
        return
    if not _current_bid(mail, sid):
        return
    sid = _slot_of_reply(mail, sid)  # below, sid is the bid slot; db/events use owner(sid)
    body = mail.body or ""
    if served_lanes.parse_refusal(body) is not None:
        _evaluate(mail, sid, carrier, body, [], [])
        return
    try:
        paths = _attachment_paths(mail, sid)
    except Exception as exc:  # noqa: BLE001 - reported as unreadable bid below
        log.warning("bid mail %s: attachments not downloaded: %s", mail.id, exc)
        _unreadable(carrier, sid, "Anhang nicht geladen")
        return
    files: list[tuple[str, str]] = []
    spoken: list[tuple[str, str]] = []
    pending: list[tuple[Path, documents.Document]] = []
    for path in paths:
        try:
            doc = documents.read(path)
            if doc.voice and not doc.audio and not _usable_voice(doc.text):
                # An old empty/unusable cached transcript must not prevent recovery.
                doc = documents.read_bytes(path.name, path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - a broken file must not price the bid
            _unreadable(carrier, sid, f"Anhang {path.name} nicht lesbar ({str(exc)[:80]})")
            return
        if doc.needs_model:
            pending.append((path, doc))
        elif doc.voice:
            spoken.append((path.name, doc.text))
        else:
            files.append((path.name, doc.text))
    if pending:
        if not config.settings.attachment_transcription:
            _unreadable(carrier, sid, "nur Audio/Scan, Transkription aus")
            return
        _spawn(_transcribe_then_evaluate, mail, sid, carrier, body, files, pending, spoken)
        return
    _evaluate(mail, sid, carrier, body, files, spoken)


def _transcribe_then_evaluate(
    mail: Mail,
    sid: int,
    carrier: Carrier,
    body: str,
    files: list,
    pending: list,
    spoken: list[tuple[str, str]] | None = None,
) -> None:
    """Recognition must not hold up mail intake. Voice uses its bounded provider chain;
    scans use gateway calls outside the quote budget, capped by MAX_BID_TRANSCRIPTIONS."""
    try:
        spoken = list(spoken or [])
        files = list(files)
        for path, doc in pending:
            cached = documents.cached_transcript(path)
            if doc.media and cached is None:
                with _cond:
                    used = _transcriptions.get(sid, 0)
                    if used >= MAX_BID_TRANSCRIPTIONS:
                        raise RuntimeError(f"{used} bid attachments already transcribed")
                    _transcriptions[sid] = used + 1
            text = documents.transcribe(
                doc,
                None,
                f"bid_attachment s{sid}",
                path,
                audit_session_id=owner(sid),
                accept_text=_usable_voice if doc.voice else None,
            )
            db.emit(
                "bid_transcribed",
                f"Gebot von {carrier.name}: {path.name} gelesen ({doc.kind.lstrip('.')})",
                owner(sid),
                transcript=text[:2000],
            )
            (spoken if doc.audio else files).append((path.name, text))
        _evaluate(mail, sid, carrier, body, files, spoken)
    except Exception as exc:  # noqa: BLE001 - budget, gateway or model refusal
        log.warning("bid mail %s: transcription failed: %s", mail.id, exc)
        _unreadable(carrier, sid, f"nicht transkribiert ({str(exc)[:80]})")


def _evaluate(
    mail: Mail,
    sid: int,
    carrier: Carrier,
    body: str,
    files: list[tuple[str, str]],
    spoken: list[tuple[str, str]],
) -> None:
    """Refusal or offer from the mail text plus everything read from its attachments."""
    if not _current_bid(mail, sid):
        return
    refusal = served_lanes.parse_refusal(
        "\n".join([body] + [t for _, t in spoken] + [t for _, t in files])
    )
    if refusal is not None:
        served_lanes.remember(carrier.email, refusal, at=mail.received_at or None)
        with _cond:
            req = _open.setdefault(sid, _Request())
            req.declined.add(carrier.email)
            req.last_answer_at = time.monotonic()
            _cond.notify_all()
        db.emit(
            "bid_declined",
            f"{carrier.name} fliegt {refusal.lane} nicht ({len(refusal.served)} Lanes gemerkt)",
            owner(sid),
        )
        return
    evidence, price, issue = _bid_evidence(body, files, spoken)
    if price is None:
        issue = issue or "kein Preis erkannt"
        # two sources naming different prices stay unreadable: the model must not pick one
        if config.settings.bid_llm_fallback and not issue.startswith("conflicting"):
            # the gateway call must not hold up the mail loop
            _spawn(_read_with_model, mail, sid, carrier, body, files, spoken, issue)
        else:
            _unreadable(carrier, sid, issue)
        return
    _add_offer(mail, sid, carrier, evidence, price, [n for n, _ in files + spoken])


def _read_with_model(
    mail: Mail,
    sid: int,
    carrier: Carrier,
    body: str,
    files: list[tuple[str, str]],
    spoken: list[tuple[str, str]],
    issue: str,
) -> None:
    """BID_LLM_FALLBACK: the rules found no unique total; one checked extraction call
    (app/agents/bid_reader.py). Only if that fails too is the bid unreadable."""
    from app.agents import bid_reader
    from app.pricing.fx import convertible

    text = "\n\n".join([body] + [f"[{n}]\n{t}" for n, t in files + spoken])
    try:
        reading = bid_reader.read(text, owner(sid), convertible())
    except Exception:
        log.exception("bid mail %s: model reading failed", mail.id)
        reading = None
    if reading is None:
        _unreadable(carrier, sid, f"{issue}; auch per Modell kein eindeutiger Gesamtpreis")
        return
    db.emit(
        "bid_model_read",
        f"Gebot von {carrier.name} per Modell gelesen ({issue[:80]})",
        owner(sid),
        level="warn",
    )
    evidence = text if reading.transit_days is None else f"transit {reading.transit_days:g} days"
    _add_offer(
        mail,
        sid,
        carrier,
        evidence,
        (money(reading.amount), reading.currency),
        [n for n, _ in files + spoken],
        via="model",
    )


def _add_offer(
    mail: Mail,
    sid: int,
    carrier: Carrier,
    evidence: str,
    price: tuple[Decimal, str],
    sources: list[str],
    via: str = "rules",
) -> None:
    if not _current_bid(mail, sid):
        return
    amount, currency = price
    from app.pricing.fx import eur_rate

    try:
        fx, fx_source, fx_expiry = eur_rate(currency)
    except ValueError as exc:
        _unreadable(carrier, sid, str(exc), kind="bid_skipped")
        return
    offer = Offer(
        carrier,
        money(amount * fx),
        parse_transit_days(evidence),
        mail.id,
        amount,
        currency,
        str(fx),
        fx_source,
        fx_expiry,
        timing_kind(evidence),
        float(billed[1]) if (billed := _BILLED.search(mail.subject or "")) else None,
    )
    with _cond:
        # FX/document reads may have overlapped a request revision.
        if not _current_bid(mail, sid):
            return
        req = _open.setdefault(sid, _Request())
        if any(previous.mail_id == mail.id for previous in req.offers):
            return
        req.offers.append(offer)
        req.last_answer_at = time.monotonic()
        _cond.notify_all()
    db.emit(
        "bid_received",
        f"Gebot {carrier.name}: {offer.total_eur} EUR"
        + (f", {offer.transit_days:g} Tage" if offer.transit_days is not None else ""),
        owner(sid),
        original_amount=str(amount),
        currency=currency,
        fx_rate=str(fx),
        fx_source=fx_source,
        fx_valid_until=fx_expiry,
        evidence_sources=sources or ["mail_body"],
        read_by=via,
    )


# ---------------------------------------------------------------- waiting


def offers(session_id: int) -> list[Offer]:
    with _cond:
        req = _open.get(session_id)
        return list(req.offers) if req else []


def wait(session_id: int, timeout_s: float, until_kinds: set[str] | None = None) -> list[Offer]:
    """Block until every asked carrier answered (or, with `until_kinds`, until each kind has
    an offer or nobody of that kind is left to answer), or the timeout passes. Returns the
    offers so far.

    With BID_GRACE_S > 0 the plain wait also ends once BID_QUORUM carriers answered, every
    asked kind has an offer, and no further reply came for BID_GRACE_S: one silent carrier no
    longer holds the quote to the deadline (replies cluster 34 to 45 s after our request)."""
    end = time.monotonic() + max(0.0, timeout_s)
    grace = config.settings.bid_grace_s

    def check() -> tuple[bool, float | None]:
        req = _open.get(session_id)
        if req is None or not req.asked:
            return True, None
        answered = {o.carrier.email for o in req.offers} | req.declined | req.unreadable
        have = {o.carrier.kind for o in req.offers}
        waiting = {c.kind for email, c in req.asked.items() if email not in answered}
        if until_kinds is not None:
            # round 11: every airline had declined, yet we waited out the full timeout
            return all(k in have or k not in waiting for k in until_kinds), None
        if answered >= set(req.asked):
            return True, None
        quorum = min(len(req.asked), max(1, config.settings.bid_quorum))
        kinds = {c.kind for c in req.asked.values()}
        if (
            grace > 0
            and len(answered & set(req.asked)) >= quorum
            and all(k in have or k not in waiting for k in kinds)
        ):
            return False, req.last_answer_at + grace
        return False, None

    released: tuple[int, int] | None = None
    with _cond:
        while True:
            done, release_at = check()
            now = time.monotonic()
            if done or now >= end:
                break
            if release_at is not None and now >= release_at:
                req = _open[session_id]
                answered = {o.carrier.email for o in req.offers} | req.declined | req.unreadable
                released = (len(answered & set(req.asked)), len(req.asked))
                break
            _cond.wait(timeout=(min(end, release_at) if release_at is not None else end) - now)
    if released is not None:
        db.emit(
            "bid_quorum",
            f"{released[0]} von {released[1]} Carriern geantwortet, Rest nach {grace:.0f} s"
            " Ruhe nicht mehr abgewartet",
            owner(session_id),
            answered=released[0],
            asked=released[1],
        )
    return offers(session_id)


def asked_kinds(session_id: int) -> set[str]:
    with _cond:
        req = _open.get(session_id)
        return {c.kind for c in req.asked.values()} if req else set()


def close(session_id: int) -> None:
    with _cond:
        _open.pop(session_id, None)


# ---------------------------------------------------------------- conversion


def airline_option(offer: Offer, rfq: Rfq, origin: str, chargeable_kg: float) -> RateOption:
    total = to_minor(offer.total_eur)
    days = offer.transit_days if offer.transit_days is not None else 99
    return RateOption(
        airline=offer.carrier.name,
        origin=origin,
        destination=rfq.destination,
        transit_days=max(1, round(days)),  # a same-day offer is never 0 days
        total_minor=to_minor(offer.original_amount) if offer.original_amount is not None else total,
        currency=offer.currency,
        total_eur_minor=total,
        source="bid",
        chargeable_kg=chargeable_kg,
        billed_kg=offer.billed_kg or chargeable_kg,
        fx_eur_per_unit=offer.fx_rate,
        fx_source=offer.fx_source,
        fx_valid_until=offer.fx_valid_until,
    )


def _current_bid(mail: Mail, sid: int) -> bool:
    references = list(mail.references)
    if mail.raw.get("in_reply_to"):
        references.append(int(mail.raw["in_reply_to"]))
    accepted = db.reply_accepts_bid(owner(sid), mail.id, mail.subject, references)
    if not accepted:
        db.emit(
            "stale_bid",
            "Veraltetes oder nicht zuordenbares Gebot ausgeschlossen",
            sid,
            mail_id=mail.id,
        )
    return accepted
