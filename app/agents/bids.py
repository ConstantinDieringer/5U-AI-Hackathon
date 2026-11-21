"""Carrier bids by email: ask truckers (door-to-port pickup) and email-only airlines for a
price, read their replies, and hand the offers to the waiting RFQ agent.

No LLM: replies are read with regexes. A reply whose price cannot be read is logged and
ignored (the RFQ is then quoted from sheet + portal).

Mapping a reply to its RFQ: db.ingest_mail maps threaded replies and subject-quoting replies
to the session; as a fallback the reference tag `[AP-<session>]` in subject or body is used.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app import config, db, hackloop
from app.mail import documents
from app.mail.router import address
from app.mail.style import SIGNATURE
from app.models import Mail, RateOption, Rfq
from app.pricing import served_lanes
from app.pricing.lanes import airlines_for
from app.pricing.portal_sync import next_break
from app.pricing.pricing import chargeable_kg, money, to_minor
from app.pricing.reference import Carrier, carrier_by_email

log = logging.getLogger(__name__)

TAG = re.compile(r"\[AP-(\d+)\]")
_BILLED = re.compile(r"billed at (\d+(?:\.\d+)?) kg", re.IGNORECASE)
_MONEY = r"(?:\d{1,3}(?:[ ,.'’]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_AMOUNT = re.compile(
    rf"(?:(?P<cur1>EUR|USD|€|\$)\s*(?P<num1>{_MONEY}))"
    rf"|(?:(?P<num2>{_MONEY})\s*(?P<cur2>EUR|USD|€|\$))",
    re.IGNORECASE,
)
_DAYS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:business\s+|working\s+|calendar\s+)?(?:days?|tage?n?)\b",
    re.IGNORECASE,
)
_HOURS = re.compile(r"(\d+)\s*(?:h|hours?|stunden)\b", re.IGNORECASE)
_TOTAL_HINT = re.compile(r"all[- ]?in|total|gesamt|price|preis|rate|offer|angebot", re.IGNORECASE)


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


_open: dict[int, _Request] = {}
_cond = threading.Condition()
_transcriptions: dict[int, int] = {}  # session -> model calls spent on bid attachments
MAX_BID_TRANSCRIPTIONS = 5  # one per carrier at most (MAX_BID_CARRIERS default 5)


# ---------------------------------------------------------------- parsing


def _number(raw: str) -> Decimal | None:
    s = raw.replace(" ", "").replace("'", "")
    if "," in s and "." in s:  # the later separator is the decimal one
        s = (
            s.replace(",", "")
            if s.rfind(".") > s.rfind(",")
            else s.replace(".", "").replace(",", ".")
        )
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.rpartition(".")[2]) == 3):
        s = s.replace(".", "")  # 1.234 / 1.234.567 = thousands
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_price(text: str) -> tuple[Decimal, str] | None:
    """The quoted price and its currency. Prefers an amount on a line naming a total/price;
    among several such amounts the largest (an all-in total beats its components)."""
    best: tuple[int, Decimal, str] | None = None
    for line in text.splitlines():
        hint = 1 if _TOTAL_HINT.search(line) else 0
        for m in _AMOUNT.finditer(line):
            num = _number(m.group("num1") or m.group("num2") or "")
            cur = (m.group("cur1") or m.group("cur2") or "").upper()
            cur = {"€": "EUR", "$": "USD"}.get(cur, cur)
            if num is None or num <= 0:
                continue
            key = (hint, num)
            if best is None or key > (best[0], best[1]):
                best = (hint, num, cur)
    return (money(best[1]), best[2]) if best else None


def parse_transit_days(text: str) -> float | None:
    m = _DAYS.search(text)
    if m:
        return float(m.group(1).replace(",", "."))
    h = _HOURS.search(text)
    return int(h.group(1)) / 24 if h else None


def document_price(text: str) -> tuple[Decimal, str] | None:
    """An explicit total, not the largest component or an incidental surcharge."""
    currencies = {
        {"€": "EUR", "$": "USD"}.get(m.upper(), m.upper())
        for m in re.findall(r"\bEUR\b|\bUSD\b|€|\$", text, re.IGNORECASE)
    }
    found: set[tuple[Decimal, str]] = set()
    # The shared document reader separates table cells with pipes. Empty quantity/unit
    # cells must not hide a total label or split an adjacent currency and amount.
    lines = [
        " | ".join(cell.strip() for cell in line.split("|") if cell.strip())
        for line in text.splitlines()
    ]
    for index, line in enumerate(lines):
        label = re.match(
            r"\s*(?:(?:grand\s+)?total|gesamt(?:preis|betrag|summe)?)\b"
            r"(?:\s*,?\s*all[- ]?in\b)?\s*[:|=]?\s*",
            line,
            re.IGNORECASE,
        )
        if not label:
            label = re.match(
                r"\s*(?:(?:our|unser)\s+)?(?:all[- ]?in\s+)?(?:price|preis)\b"
                r".{0,60}?(?:\bis\b|\bist\b|\bbeträgt\b|:|\|)\s*",
                line,
                re.IGNORECASE,
            )
        if not label:
            continue
        tail = line[label.end() :].strip()
        if not tail and index + 1 < len(lines):
            tail = lines[index + 1].strip()
        tail = tail.replace("|", " ").strip()
        amount = _AMOUNT.match(tail)
        if amount:
            raw = amount.group("num1") or amount.group("num2") or ""
            cur = (amount.group("cur1") or amount.group("cur2") or "").upper()
            cur = {"€": "EUR", "$": "USD"}.get(cur, cur)
        elif re.fullmatch(_MONEY, tail) and len(currencies) == 1:
            raw, cur = tail, next(iter(currencies))
        else:
            continue
        num = _number(raw)
        if num is not None and num > 0:
            found.add((money(num), cur))
    return next(iter(found)) if len(found) == 1 else None


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
        evidence.append((text, price))
    for name, text in spoken:
        price = parse_price(text)
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
    collection = bool(re.search(r"pickup|pick up|collect|abhol", clause)) and not road
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


def request(session_id: int, rfq: Rfq, origin: str, carriers: list[Carrier]) -> int:
    """Send one bid request per carrier and register them. Returns how many were sent."""
    sent = 0
    already_sent = db.bid_request_recipients(session_id)
    with _cond:
        req = _open.setdefault(session_id, _Request())
    before = _resume(session_id, req)
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
                _subject(session_id, rfq, origin, c.kind, billed),
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
    mails = s.get("mails") or []
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
                     str(m["body"] or ""), str(m["received_at"] or "")),
                session_id,
            )  # fmt: skip
    return asked


# ---------------------------------------------------------------- replies


def session_of(mail: Mail, session_id: int | None) -> int | None:
    if session_id is not None:
        return session_id
    m = TAG.search(mail.subject or "") or TAG.search(mail.body or "")
    return int(m.group(1)) if m else None


def _attachment_paths(mail: Mail) -> list[Path]:
    """The bid's saved attachments. The poller already downloaded them; a mail rebuilt from
    the db after a restart has no attachment list, so its inbox folder is read instead."""
    if mail.attachments:
        from app.mail.poller import download_attachments

        return download_attachments(mail)
    return documents.inbox_files(config.settings.inbox_dir / str(mail.id))


def _spawn(fn, *args) -> None:  # tests replace this with a direct call
    threading.Thread(target=fn, args=args, name=f"bid-doc-{args[0].id}", daemon=True).start()


def _unreadable(carrier: Carrier, sid: int, issue: str) -> None:
    db.emit("bid_unreadable", f"Gebot von {carrier.name}: {issue}"[:300], sid, level="warn")


def on_bid_mail(mail: Mail, session_id: int | None) -> None:
    """Called by the poller for every CARRIER_BID mail. The offer may be in the mail text or
    in attachments: documents are read in code (app/mail/documents.py), voice messages and
    scans are transcribed in a separate thread."""
    sid = session_of(mail, session_id)
    carrier = carrier_by_email(address(mail.sender))
    if sid is None or carrier is None:
        log.warning("bid mail %s: no session or unknown carrier, ignored", mail.id)
        return
    body = mail.body or ""
    if served_lanes.parse_refusal(body) is not None:
        _evaluate(mail, sid, carrier, body, [], [])
        return
    try:
        paths = _attachment_paths(mail)
    except Exception as exc:  # noqa: BLE001 - reported as unreadable bid below
        log.warning("bid mail %s: attachments not downloaded: %s", mail.id, exc)
        _unreadable(carrier, sid, "Anhang nicht geladen")
        return
    files: list[tuple[str, str]] = []
    pending: list[tuple[Path, documents.Document]] = []
    for path in paths:
        try:
            doc = documents.read(path)
        except Exception as exc:  # noqa: BLE001 - a broken file must not price the bid
            _unreadable(carrier, sid, f"Anhang {path.name} nicht lesbar ({str(exc)[:80]})")
            return
        if doc.needs_model:
            pending.append((path, doc))
        else:
            files.append((path.name, doc.text))
    if pending:
        if not config.settings.attachment_transcription:
            _unreadable(carrier, sid, "nur Audio/Scan, Transkription aus")
            return
        _spawn(_transcribe_then_evaluate, mail, sid, carrier, body, files, pending)
        return
    _evaluate(mail, sid, carrier, body, files, [])


def _transcribe_then_evaluate(
    mail: Mail, sid: int, carrier: Carrier, body: str, files: list, pending: list
) -> None:
    """Runs in its own thread: a gateway call must not hold up the mail loop. The calls do
    not use the session's LLM budget (5 voice bids would leave none for extraction and the
    quote mail); MAX_BID_TRANSCRIPTIONS per session bounds their cost instead."""
    try:
        spoken: list[tuple[str, str]] = []
        files = list(files)
        for path, doc in pending:
            if documents.cached_transcript(path) is None:
                with _cond:
                    used = _transcriptions.get(sid, 0)
                    if used >= MAX_BID_TRANSCRIPTIONS:
                        raise RuntimeError(f"{used} bid attachments already transcribed")
                    _transcriptions[sid] = used + 1
            text = documents.transcribe(doc, None, f"bid_attachment s{sid}", path)
            db.emit(
                "bid_transcribed",
                f"Gebot von {carrier.name}: {path.name} gelesen ({doc.kind.lstrip('.')})",
                sid,
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
    refusal = served_lanes.parse_refusal(
        "\n".join([body] + [t for _, t in spoken] + [t for _, t in files])
    )
    if refusal is not None:
        served_lanes.remember(carrier.email, refusal, at=mail.received_at or None)
        with _cond:
            _open.setdefault(sid, _Request()).declined.add(carrier.email)
            _cond.notify_all()
        db.emit(
            "bid_declined",
            f"{carrier.name} fliegt {refusal.lane} nicht ({len(refusal.served)} Lanes gemerkt)",
            sid,
        )
        return
    evidence, price, issue = _bid_evidence(body, files, spoken)
    if price is None:
        db.emit(
            "bid_unreadable",
            f"Gebot von {carrier.name}: {issue or 'kein Preis erkannt'}",
            sid,
            level="warn",
        )
        return
    amount, currency = price
    from app.pricing.fx import eur_rate

    try:
        fx, fx_source, fx_expiry = eur_rate(currency)
    except ValueError as exc:
        db.emit(
            "bid_skipped",
            f"Gebot von {carrier.name}: {exc}",
            sid,
            level="warn",
        )
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
        _open.setdefault(sid, _Request()).offers.append(offer)
        _cond.notify_all()
    db.emit(
        "bid_received",
        f"Gebot {carrier.name}: {offer.total_eur} EUR"
        + (f", {offer.transit_days:g} Tage" if offer.transit_days is not None else ""),
        sid,
        original_amount=str(amount),
        currency=currency,
        fx_rate=str(fx),
        fx_source=fx_source,
        fx_valid_until=fx_expiry,
        evidence_sources=[n for n, _ in files + spoken] or ["mail_body"],
    )


# ---------------------------------------------------------------- waiting


def offers(session_id: int) -> list[Offer]:
    with _cond:
        req = _open.get(session_id)
        return list(req.offers) if req else []


def wait(session_id: int, timeout_s: float, until_kinds: set[str] | None = None) -> list[Offer]:
    """Block until every asked carrier answered (or, with `until_kinds`, until each kind has
    an offer or nobody of that kind is left to answer), or the timeout passes. Returns the
    offers so far."""

    def done() -> bool:
        req = _open.get(session_id)
        if req is None or not req.asked:
            return True
        answered = {o.carrier.email for o in req.offers} | req.declined
        if until_kinds is not None:
            have = {o.carrier.kind for o in req.offers}
            # round 11: every airline had declined, yet we waited out the full timeout
            waiting = {c.kind for email, c in req.asked.items() if email not in answered}
            return all(k in have or k not in waiting for k in until_kinds)
        return answered >= set(req.asked)

    with _cond:
        _cond.wait_for(done, timeout=max(0.0, timeout_s))
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
        transit_days=round(days),
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
