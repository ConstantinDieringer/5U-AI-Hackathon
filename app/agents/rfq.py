"""RFQ agent: customer request -> one quote in the customer's thread.

Flow (handle_rfq, runs in its own thread, started by the poller):
  1. extract_rfq      one LLM call through the gateway (schema-validated Rfq)
  2. bid requests     truckers for door-to-port pickup, email-only airlines on the lane
  3. prices           rate sheets + portal (B), then carrier bids as they arrive
  4. choose           best bid by price/speed 50/50, then cheapest/fastest over all air options;
                      door-to-port adds the best trucker bid
  5. claim + send     db.claim_quote_send (one quote per thread), reply with in_reply_to
Pricing is deterministic; the LLM only reads the mail.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app import db, hackloop, llm
from app.agents import bids
from app.config import settings
from app.mail import language, lots, parse
from app.models import Mail, RateOption, Rfq
from app.pricing import served_lanes
from app.pricing.lanes import airlines_for
from app.pricing.portal_sync import portal_result
from app.pricing.pricing import pick, resolve_origin, sheet_options, to_minor
from app.pricing.reference import (
    Carrier,
    carrier_by_email,
    truckers_for_zone,
    zone_for_zip,
)

log = logging.getLogger(__name__)

RFQ_LIFETIME_S = 360  # 5U expires a request 6 min after it was sent
TOO_OLD_S = (
    330  # do not start work on a request that cannot be answered in time any more
)

EXTRACT_PROMPT = (
    "Extract the air freight request from the email as JSON.\n"
    "origin: IATA code of the departure airport if the customer names one, else null.\n"
    "pickup_zip: postal code of the pickup site if they want trucking/pickup from a site, else null.\n"
    "destination: IATA code of the destination airport (a city -> its main cargo airport).\n"
    "weight_kg: total gross weight in kg (convert lb/t). volume_cbm: total volume in cbm; if only "
    "dimensions are given, compute pieces x L x W x H in metres.\n"
    "preference: 'fastest' if they ask for the fastest/quickest/express option, else 'cheapest'.\n"
    "ready_date: ISO date or null. language: ISO 639-1 code of the email language.\n"
    "customer_name: the sender's first name from the signature, or null."
)

_N = ["string", "null"]
RFQ_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "rfq",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "origin",
                "pickup_zip",
                "destination",
                "weight_kg",
                "volume_cbm",
                "pieces",
                "preference",
                "ready_date",
                "language",
                "customer_name",
            ],
            "properties": {
                "origin": {"type": _N},
                "pickup_zip": {"type": _N},
                "destination": {"type": "string"},
                "weight_kg": {"type": "number"},
                "volume_cbm": {"type": "number"},
                "pieces": {"type": ["integer", "null"]},
                "preference": {"type": "string", "enum": ["cheapest", "fastest"]},
                "ready_date": {"type": _N},
                "language": {"type": "string"},
                "customer_name": {"type": _N},
            },
        },
    },
}

_QUOTED = re.compile(r"^\s*>.*$", re.MULTILINE)


# ---------------------------------------------------------------- 1. extract


def _clean(body: str) -> str:
    return _QUOTED.sub("", body or "").strip()[:2500]


def extract_rfq(mail: Mail, session_id: int) -> Rfq | None:
    """One gateway call. None if the mail is not a usable air freight request."""
    text = llm.complete(
        "extract_rfq",
        [
            {"role": "system", "content": EXTRACT_PROMPT},
            {
                "role": "user",
                "content": f"Subject: {mail.subject}\n\n{_clean(mail.body)}",
            },
        ],
        session_id,
        max_tokens=200,
        response_format=RFQ_SCHEMA,
    )
    try:
        data = json.loads(text)
        totals = lots.parse_lots(_clean(mail.body))
        if isinstance(data, dict) and totals is not None:
            # lot sums in code: nano returned weight 0 for "each" lots (mail 1967)
            data["weight_kg"] = float(totals.weight_kg)
            data["volume_cbm"] = float(totals.volume_cbm)
            db.emit(
                "lots_summed",
                f"{totals.lots} Lots: {totals.weight_kg:.1f} kg, {totals.volume_cbm:.4f} cbm",
                session_id,
            )
        return Rfq.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        log.warning(
            "session %s: extraction not usable (%s): %r", session_id, e, text[:300]
        )
        return None


# ---------------------------------------------------------------- 4. choose


def best_air(
    air: list[RateOption], bid_opts: list[RateOption], preference: str
) -> RateOption | None:
    """Among carrier bids the balanced one (price/speed 50/50) counts; then the customer's
    preference over sheet, portal and that bid."""
    best_bid = pick(bid_opts, "balanced")
    return pick(air + ([best_bid] if best_bid else []), preference)


def best_truck(offers: list[bids.Offer]) -> bids.Offer | None:
    trucks = [o for o in offers if o.carrier.kind == "trucker"]
    if not trucks:
        return None
    if len(trucks) == 1 or any(o.transit_days is None for o in trucks):
        return min(trucks, key=lambda o: o.total_eur)
    lo_p, hi_p = min(o.total_eur for o in trucks), max(o.total_eur for o in trucks)
    lo_d = min(Decimal(str(o.transit_days)) for o in trucks)
    hi_d = max(Decimal(str(o.transit_days)) for o in trucks)

    def norm(v: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
        return Decimal(0) if hi == lo else (v - lo) / (hi - lo)

    return min(
        trucks,
        key=lambda o: (
            norm(o.total_eur, lo_p, hi_p) / 2
            + norm(Decimal(str(o.transit_days)), lo_d, hi_d) / 2,
            o.total_eur,
        ),
    )


# ---------------------------------------------------------------- 5. text


def _amount(minor: int, lang: str) -> str:
    whole, cents = divmod(minor, 100)
    if lang == "de":
        return f"{whole:,}".replace(",", ".") + f",{cents:02d} EUR"
    return f"EUR {whole:,}.{cents:02d}"


def _valid(option: RateOption) -> str | None:
    if not option.valid_until:
        return None
    try:
        return (
            datetime.fromisoformat(option.valid_until)
            .astimezone(UTC)
            .strftime("%H:%M UTC")
        )
    except ValueError:
        return None


_MONTHS_EN = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def schedule(
    rfq: Rfq, transit_days: int, today: date | None = None
) -> tuple[date, date] | None:
    """Earliest departure and arrival: departure on the ready date (one day later when we
    truck it to the airport first, never before today), arrival = departure + transit."""
    if not rfq.ready_date:
        return None
    try:
        ready = date.fromisoformat(rfq.ready_date[:10])
    except ValueError:
        return None
    dep = max(ready, today or datetime.now(UTC).date())
    if rfq.pickup_zip:
        dep += timedelta(days=1)
    return dep, dep + timedelta(days=transit_days)


def _date(d: date, lang: str) -> str:
    return (
        d.strftime("%d.%m.%Y")
        if lang == "de"
        else f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year}"
    )


def _schedule_line(
    rfq: Rfq, option: RateOption, lang: str, today: date | None
) -> str | None:
    when = schedule(rfq, option.transit_days, today)
    if when is None:
        return None
    dep, arr = (_date(d, lang) for d in when)
    if lang == "de":
        return (
            f"Frühester Abflug ab {option.origin} ist am {dep}, "
            f"Ankunft in {option.destination} am {arr}."
        )
    return f"Earliest departure from {option.origin} is {dep}, arriving in {option.destination} on {arr}."


def quote_text(
    rfq: Rfq,
    option: RateOption,
    total_eur_minor: int,
    truck: bids.Offer | None,
    variant: int,
    today: date | None = None,
) -> str:
    """The quote mail: one all-in total with its currency, in the customer's language."""
    lang = "de" if (rfq.language or "").lower().startswith("de") else "en"
    name = rfq.customer_name
    price = _amount(total_eur_minor, lang)
    days = option.transit_days
    valid = _valid(option)
    when = _schedule_line(rfq, option, lang, today)
    if lang == "de":
        hello = f"Hallo {name}," if name else "Guten Tag,"
        intro = (
            "vielen Dank für Ihre Anfrage, gerne machen wir Ihnen ein Angebot."
            if variant % 2 == 0
            else "danke für Ihre Anfrage, hier ist unser Angebot."
        )
        what = f"Luftfracht {option.origin} nach {option.destination}"
        if truck is not None and rfq.pickup_zip:
            what += f", inklusive Abholung in {rfq.pickup_zip} und Lkw-Transport zum Flughafen {option.origin}"
        lines = [
            hello,
            "",
            intro,
            "",
            f"{what}: {price} all-in.",
            f"Wir fliegen mit {option.airline}, die Laufzeit beträgt {days} Tage ab Abflug.",
        ]
        if when:
            lines.append(when)
        if valid:
            lines.append(f"Der Preis ist bis {valid} buchbar.")
        lines += [
            "",
            "Wenn das für Sie passt, antworten Sie einfach auf diese Mail und wir buchen sofort.",
            "",
            "Viele Grüße",
            "Ihr Team von Austrian Power Forwarding",
        ]
    else:
        hello = f"Hi {name}," if name else "Hello,"
        intro = (
            "thanks for your request, happy to help."
            if variant % 2 == 0
            else "thank you for reaching out, here is our offer."
        )
        what = f"Air freight {option.origin} to {option.destination}"
        if truck is not None and rfq.pickup_zip:
            what += f", including pickup at {rfq.pickup_zip} and trucking to {option.origin} airport"
        lines = [
            hello,
            "",
            intro,
            "",
            f"{what}: {price} all-in.",
            f"It flies with {option.airline}, transit time {days} days from departure.",
        ]
        if when:
            lines.append(when)
        if valid:
            lines.append(f"The rate can be booked until {valid}.")
        lines += [
            "",
            "If that works for you, just reply to this email and we will book it right away.",
            "",
            "Best regards",
            "Austrian Power Forwarding",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- flow


def _received(mail: Mail) -> float:
    try:
        return datetime.fromisoformat(mail.received_at).timestamp()
    except ValueError:
        return time.time()


def _bid_carriers(rfq: Rfq, origin: str) -> tuple[list[Carrier], list[Carrier]]:
    truckers = truckers_for_zone(zone_for_zip(rfq.pickup_zip)) if rfq.pickup_zip else []
    airlines: list[Carrier] = []
    for a in airlines_for(origin, rfq.destination):
        if a.channel != "email_only" or not a.email:
            continue
        # skip an airline whose own lane list (from an earlier refusal) lacks this lane
        if served_lanes.serves(a.email, origin, rfq.destination) is False:
            continue
        c = carrier_by_email(a.email)
        if c is not None:
            airlines.append(c)
    return truckers, airlines


def _stored_request(sid: int) -> Rfq | None:
    stored = (db.get_session(sid) or {}).get("request")
    if not isinstance(stored, dict):
        return None
    try:
        return Rfq.model_validate(stored)
    except ValidationError:
        return None


def catch_up() -> int:
    """RFQs left mid-flight by a restart (new/pricing, no quote, still answerable): run them
    again. Extraction and bid requests already done are reused, not repeated."""
    since = datetime.fromtimestamp(time.time() - TOO_OLD_S, UTC).isoformat()
    n = 0
    for r in db.resumable_rfqs(since):
        mail = (
            parse.mail_from_api(json.loads(r["raw_json"]))
            if r.get("raw_json")
            else Mail(
                id=int(r["id"]),
                thread_root=int(r["id"]),
                sender=str(r["sender"]),
                to=str(r["recipient"] or settings.team_mailbox),
                subject=str(r["subject"] or ""),
                body=str(r["body"] or ""),
                received_at=str(r["received_at"]),
            )
        )
        db.emit("resumed", "Nach Neustart fortgesetzt", int(r["session_id"]))
        threading.Thread(
            target=handle_rfq,
            args=(mail, int(r["session_id"])),
            name=f"rfq-{r['session_id']}",
            daemon=True,
        ).start()
        n += 1
    if n:
        log.info("resumed %d open RFQ(s) after restart", n)
    return n


def handle_rfq(mail: Mail, session_id: int) -> None:
    """Whole flow for one RFQ. Never raises (runs in a worker thread)."""
    try:
        _handle(mail, session_id)
    except Exception as e:
        log.exception("session %s: RFQ failed", session_id)
        db.set_status(session_id, "error", f"{type(e).__name__}: {e}")
    finally:
        bids.close(session_id)


def _handle(mail: Mail, sid: int) -> None:
    t0 = _received(mail)
    age = time.time() - t0
    if age > TOO_OLD_S:
        db.emit(
            "skipped",
            f"Anfrage ist {age:.0f} s alt, nicht mehr beantwortbar",
            sid,
            level="warn",
        )
        return
    db.set_status(sid, "pricing")

    start = time.monotonic()
    rfq = _stored_request(sid) or extract_rfq(
        mail, sid
    )  # stored = resumed after a restart
    if rfq is None:
        db.set_status(sid, "error", "Anfrage nicht lesbar")
        return
    # the LLM guesses the language from names/places (English mail from a German firm -> "de")
    rfq = rfq.model_copy(update={"language": language.detect(mail.body)})
    db.set_request(sid, rfq.model_dump())
    db.emit(
        "extract_done",
        "Anfrage ausgelesen",
        sid,
        duration_ms=int((time.monotonic() - start) * 1000),
    )

    origin = resolve_origin(rfq)
    if origin is None:
        db.set_status(sid, "error", f"kein Abflughafen (PLZ {rfq.pickup_zip})")
        return

    truckers, email_airlines = _bid_carriers(rfq, origin)
    if rfq.pickup_zip and not truckers:
        db.emit(
            "no_trucker",
            f"Kein Trucker für Zone {zone_for_zip(rfq.pickup_zip)}",
            sid,
            level="warn",
        )
    if not db.is_manual(sid) and not db.is_paused(sid):
        bids.request(sid, rfq, origin, truckers + email_airlines)

    portal = portal_result(
        rfq,
        deadline=time.monotonic() + max(0, t0 + settings.quote_target_s - time.time()),
    )
    fallback_codes = set(portal.email_fallbacks)
    fallback = []
    for airline in airlines_for(origin, rfq.destination):
        if airline.code in fallback_codes and airline.email:
            if served_lanes.serves(airline.email, origin, rfq.destination) is False:
                continue
            carrier = carrier_by_email(airline.email)
            if carrier is not None:
                fallback.append(carrier)
    if fallback:
        bids.request(sid, rfq, origin, fallback)
        email_airlines += fallback
    if portal.errors:
        db.emit(
            "portal_warning",
            "Portal options unavailable",
            sid,
            level="warn",
            errors=portal.errors,
        )
    air = sheet_options(rfq) + portal.options
    db.emit("priced", f"{len(air)} Preise aus Tarifblatt und Portal", sid)

    target = t0 + settings.quote_target_s
    hard = t0 + min(settings.bid_max_wait_s, TOO_OLD_S - 30)
    bids.wait(sid, target - time.time())
    need: set[str] = set()
    if truckers and best_truck(bids.offers(sid)) is None:
        need.add("trucker")
    if (
        email_airlines
        and not air
        and not any(o.carrier.kind == "airline" for o in bids.offers(sid))
    ):
        need.add("airline")
    if need:
        db.emit(
            "waiting",
            f"Warte bis {hard - t0:.0f} s auf: {', '.join(sorted(need))}",
            sid,
        )
        bids.wait(sid, hard - time.time(), until_kinds=need)

    offers = bids.offers(sid)

    # A rate or FX observation may expire while carrier replies are arriving.
    def current(expiry: str | None) -> bool:
        if expiry is None:
            return True
        try:
            stamp = datetime.fromisoformat(expiry)
            return stamp.tzinfo is not None and stamp > datetime.now(UTC)
        except ValueError:
            return False

    air = [o for o in air if current(o.valid_until) and current(o.fx_valid_until)]
    valid_offers = [o for o in offers if current(o.fx_valid_until)]
    if len(valid_offers) != len(offers):
        db.emit("fx_expired", "Carrier bid FX expired while waiting", sid, level="warn")
    offers = valid_offers
    chargeable = air[0].chargeable_kg if air else 0.0
    bid_opts = [
        bids.airline_option(o, rfq, origin, chargeable)
        for o in offers
        if o.carrier.kind == "airline"
    ]
    choice = best_air(air, bid_opts, rfq.preference)
    if choice is None:
        db.emit("no_price", "Kein Preis gefunden, kein Angebot", sid, level="error")
        db.set_status(sid, "error", "kein Preis")
        return
    truck = best_truck(offers) if rfq.pickup_zip else None
    if rfq.pickup_zip and truck is None:
        db.emit(
            "no_trucking", "Kein Trucker-Gebot: Angebot ohne Vorlauf", sid, level="warn"
        )
    total = choice.total_eur_minor + (to_minor(truck.total_eur) if truck else 0)

    body = quote_text(rfq, choice, total, truck, sid)
    from dataclasses import asdict

    db.emit(
        "pricing_audit",
        "Pricing inputs and selected option",
        sid,
        request=rfq.model_dump(),
        selected=asdict(choice),
        options=[asdict(o) for o in air + bid_opts],
        trucking=asdict(truck) if truck else None,
        use_breakpoints=settings.use_breakpoints,
    )
    if not db.claim_quote_send(sid):
        db.emit(
            "not_sent",
            "Nicht gesendet: pausiert, übernommen oder schon beantwortet",
            sid,
            level="warn",
        )
        return
    subject = (
        mail.subject
        if mail.subject.lower().startswith("re:")
        else f"Re: {mail.subject}"
    )
    sent = hackloop.send(mail.sender, subject, body, in_reply_to=mail.id)
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=mail.sender,
        subject=subject,
        body=body,
        category="quote",
        session_id=sid,
        references=[*mail.references, mail.id],
        in_reply_to=mail.id,
        raw=sent,
    )
    db.record_quote(
        sid,
        total_minor=total,
        currency="EUR",
        airline=choice.airline,
        origin=choice.origin,
        destination=choice.destination,
        transit_days=choice.transit_days,
        source=choice.source,  # trucking is listed in alternatives
        rate_id=choice.rate_id,
        valid_until=choice.valid_until,
        alternatives=[asdict(o) for o in air + bid_opts]
        + (
            [{"trucker": truck.carrier.name, "total_eur": str(truck.total_eur)}]
            if truck
            else []
        ),
        mail_id=int(sent["id"]),
    )
    log.info(
        "session %s: quoted %s EUR (%s) after %.0f s",
        sid,
        total / 100,
        choice.airline,
        time.time() - t0,
    )
