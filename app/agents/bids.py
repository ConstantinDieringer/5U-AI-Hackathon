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

from app import db, hackloop
from app.mail.router import address
from app.models import Mail, RateOption, Rfq
from app.pricing import served_lanes
from app.pricing.pricing import money, to_minor
from app.pricing.reference import Carrier, carrier_by_email

log = logging.getLogger(__name__)

TAG = re.compile(r"\[AP-(\d+)\]")
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


@dataclass
class _Request:
    asked: dict[str, Carrier] = field(default_factory=dict)  # email -> carrier
    offers: list[Offer] = field(default_factory=list)
    declined: set[str] = field(default_factory=set)  # emails that said "we do not serve"


_open: dict[int, _Request] = {}
_cond = threading.Condition()


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


# ---------------------------------------------------------------- requests


def _subject(session_id: int, rfq: Rfq, origin: str, kind: str) -> str:
    load = f"{rfq.weight_kg:g} kg / {rfq.volume_cbm:g} cbm"
    if kind == "trucker":
        return f"[AP-{session_id}] Pickup {rfq.pickup_zip} to {origin} airport, {load}"
    return f"[AP-{session_id}] Air freight {origin}-{rfq.destination}, {load}"


def _body(rfq: Rfq, origin: str, kind: str) -> str:
    pieces = f"{rfq.pieces} pieces, " if rfq.pieces else ""
    ready = f"Ready for pickup: {rfq.ready_date}.\n" if rfq.ready_date else ""
    if kind == "trucker":
        what = (
            f"Please quote road transport from postal code {rfq.pickup_zip} (Germany) "
            f"to {origin} airport."
        )
    else:
        what = f"Please quote air freight from {origin} to {rfq.destination}."
    return (
        f"Hello,\n\n{what}\n"
        f"Shipment: {pieces}{rfq.weight_kg:g} kg gross, {rfq.volume_cbm:g} cbm.\n{ready}\n"
        "Please reply with your all-in price in EUR and the transit time in days.\n\n"
        "Thank you,\nAustrian Power Forwarding\n"
    )


def request(session_id: int, rfq: Rfq, origin: str, carriers: list[Carrier]) -> int:
    """Send one bid request per carrier and register them. Returns how many were sent."""
    sent = 0
    with _cond:
        req = _open.setdefault(session_id, _Request())
    for c in carriers:
        if db.is_manual(session_id) or db.is_paused(session_id):
            break
        with _cond:
            if c.email in req.asked:
                continue
        subject, body = (
            _subject(session_id, rfq, origin, c.kind),
            _body(rfq, origin, c.kind),
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


# ---------------------------------------------------------------- replies


def session_of(mail: Mail, session_id: int | None) -> int | None:
    if session_id is not None:
        return session_id
    m = TAG.search(mail.subject or "") or TAG.search(mail.body or "")
    return int(m.group(1)) if m else None


def on_bid_mail(mail: Mail, session_id: int | None) -> None:
    """Called by the poller for every CARRIER_BID mail."""
    sid = session_of(mail, session_id)
    carrier = carrier_by_email(address(mail.sender))
    if sid is None or carrier is None:
        log.warning("bid mail %s: no session or unknown carrier, ignored", mail.id)
        return
    refusal = served_lanes.parse_refusal(mail.body or "")
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
    price = parse_price(mail.body or "")
    if price is None:
        db.emit(
            "bid_unreadable",
            f"Gebot von {carrier.name}: kein Preis erkannt",
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
        parse_transit_days(mail.body or ""),
        mail.id,
        amount,
        currency,
        str(fx),
        fx_source,
        fx_expiry,
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
    )


# ---------------------------------------------------------------- waiting


def offers(session_id: int) -> list[Offer]:
    with _cond:
        req = _open.get(session_id)
        return list(req.offers) if req else []


def wait(session_id: int, timeout_s: float, until_kinds: set[str] | None = None) -> list[Offer]:
    """Block until every asked carrier answered (or, with `until_kinds`, until at least one
    offer of each kind is in), or the timeout passes. Returns the offers so far."""

    def done() -> bool:
        req = _open.get(session_id)
        if req is None or not req.asked:
            return True
        if until_kinds is not None:
            have = {o.carrier.kind for o in req.offers}
            return until_kinds <= have
        answered = {o.carrier.email for o in req.offers} | req.declined
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
        billed_kg=chargeable_kg,
        fx_eur_per_unit=offer.fx_rate,
        fx_source=offer.fx_source,
        fx_valid_until=offer.fx_valid_until,
    )
