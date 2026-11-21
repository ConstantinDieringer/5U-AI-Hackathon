"""RFQ agent: customer request -> one quote in the customer's thread.

Flow (handle_rfq, runs in its own thread, started by the poller):
  1. extract_rfq      schema-validated Rfq, with one repair attempt when needed
  2. bid requests     truckers for door-to-port pickup, email-only airlines on the lane
  3. prices           rate sheets + portal (B), then carrier bids as they arrive
  4. choose           best bid by price/speed 50/50, then cheapest/fastest over all air options;
                      door-to-port adds the best trucker bid
  5. compose + send   fact-bounded answer, then db.claim_quote_send (one quote per thread)
Pricing and selection are deterministic; the LLM cannot invent quote facts.
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
from app.mail import language
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
COMPOSE_RESERVE_S = 15

EXTRACT_PROMPT = (
    "Extract the air freight request from the email as JSON.\n"
    "origin: IATA code of the departure airport if the customer names one, else null.\n"
    "pickup_zip: postal code of the pickup site if they want trucking/pickup from a site, else null.\n"
    "destination: IATA code of the destination airport (a city -> its main cargo airport).\n"
    "weight_kg: total gross weight in kg (convert lb/t). volume_cbm: total volume in cbm; if only "
    "dimensions are given, compute pieces x L x W x H in metres.\n"
    "preference: 'fastest' if they ask for the fastest/quickest/express option, else 'cheapest'.\n"
    "ready_date: ISO date or null. language: ISO 639-1 code of the email language.\n"
    "customer_name: the sender's first name from the signature, or null.\n"
    "customer_questions: every explicit question or requested detail, verbatim and without "
    "answering it (max 5). service_constraints: every other timing, handling, routing or service "
    "condition that the quote should respect (max 10)."
)

REPAIR_PROMPT = (
    "Repair the proposed RFQ JSON so that it matches the supplied JSON schema. Preserve every "
    "fact, explicit customer question and service constraint from the email. Do not guess missing "
    "commercial facts. Return JSON only."
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
                "customer_questions",
                "service_constraints",
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
                "customer_questions": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {"type": "string"},
                },
                "service_constraints": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string"},
                },
            },
        },
    },
}

_QUOTED = re.compile(r"^\s*>.*$", re.MULTILINE)


# ---------------------------------------------------------------- 1. extract


def _clean(body: str) -> str:
    return _QUOTED.sub("", body or "").strip()[:2500]


def extract_rfq(mail: Mail, session_id: int) -> Rfq | None:
    """Extract an RFQ, with one structured repair call after invalid model output."""
    source = f"Subject: {mail.subject}\n\n{_clean(mail.body)}"
    text = llm.complete(
        "extract_rfq",
        [
            {"role": "system", "content": EXTRACT_PROMPT},
            {
                "role": "user",
                "content": source,
            },
        ],
        session_id,
        max_tokens=350,
        response_format=RFQ_SCHEMA,
    )
    try:
        return Rfq.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as e:
        log.warning(
            "session %s: extraction not usable (%s): %r", session_id, e, text[:300]
        )
        try:
            repaired = llm.complete(
                "repair_rfq",
                [
                    {"role": "system", "content": REPAIR_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"EMAIL:\n{source}\n\nINVALID JSON:\n{text[:3000]}\n\n"
                            f"VALIDATION ERROR:\n{str(e)[:1000]}"
                        ),
                    },
                ],
                session_id,
                max_tokens=350,
                response_format=RFQ_SCHEMA,
            )
            return Rfq.model_validate(json.loads(repaired))
        except (
            json.JSONDecodeError,
            ValidationError,
            llm.LlmBudgetError,
        ) as repair_error:
            log.warning(
                "session %s: RFQ repair not usable (%s)", session_id, repair_error
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


COMPOSE_PROMPT = (
    "Write a concise freight quote email using only the supplied verified facts. Use the requested "
    "language. Include the exact all-in price string exactly once and no other monetary amount. "
    "Answer every indexed customer question when the facts support it; otherwise explicitly say "
    "that detail is not yet available. Do not promise services absent from the facts. Return JSON "
    "with question and constraint coverage lists; each pair of index lists must together cover "
    "every supplied customer question or service constraint exactly once."
)

COMPOSE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "quote_response",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "body",
                "answered_questions",
                "unavailable_questions",
                "addressed_constraints",
                "unavailable_constraints",
            ],
            "properties": {
                "body": {"type": "string"},
                "answered_questions": {"type": "array", "items": {"type": "integer"}},
                "unavailable_questions": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "addressed_constraints": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "unavailable_constraints": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
        },
    },
}


def _fallback_with_coverage(
    rfq: Rfq,
    option: RateOption,
    total_eur_minor: int,
    truck: bids.Offer | None,
    variant: int,
    today: date | None,
) -> str:
    body = quote_text(rfq, option, total_eur_minor, truck, variant, today)
    if not rfq.customer_questions and not rfq.service_constraints:
        return body
    lang = "de" if (rfq.language or "").lower().startswith("de") else "en"
    if lang == "de":
        note = (
            "Für alle angefragten Details, die oben nicht ausdrücklich bestätigt sind, "
            "liegen uns noch keine verifizierten Angaben vor."
        )
    else:
        note = (
            "Verified information is not yet available for any requested detail that is not "
            "explicitly confirmed above."
        )
    closing = "\nViele Grüße\n" if lang == "de" else "\nBest regards\n"
    return body.replace(closing, f"\n{note}\n{closing}", 1)


def compose_quote_text(
    rfq: Rfq,
    option: RateOption,
    total_eur_minor: int,
    truck: bids.Offer | None,
    session_id: int,
    today: date | None = None,
) -> str:
    """Generate and verify a general answer; deterministic copy is always available."""
    fallback = _fallback_with_coverage(
        rfq, option, total_eur_minor, truck, session_id, today
    )
    lang = "de" if (rfq.language or "").lower().startswith("de") else "en"
    price = _amount(total_eur_minor, lang)
    when = schedule(rfq, option.transit_days, today)
    facts = {
        "language": lang,
        "exact_all_in_price": price,
        "origin": option.origin,
        "destination": option.destination,
        "airline": option.airline,
        "transit_days_from_departure": option.transit_days,
        "gross_weight_kg": rfq.weight_kg,
        "volume_cbm": rfq.volume_cbm,
        "pieces": rfq.pieces,
        "chargeable_weight_kg": option.chargeable_kg,
        "billed_weight_kg": option.billed_kg,
        "pickup_zip": rfq.pickup_zip if truck else None,
        "pickup_included": truck is not None,
        "earliest_departure": when[0].isoformat() if when else None,
        "earliest_arrival": when[1].isoformat() if when else None,
        "bookable_until": option.valid_until,
        "customer_questions": [
            {"index": index, "question": question}
            for index, question in enumerate(rfq.customer_questions)
        ],
        "service_constraints": [
            {"index": index, "constraint": constraint}
            for index, constraint in enumerate(rfq.service_constraints)
        ],
    }
    try:
        raw = llm.complete(
            "compose_quote",
            [
                {"role": "system", "content": COMPOSE_PROMPT},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
            session_id,
            max_tokens=500,
            response_format=COMPOSE_SCHEMA,
        )
        result = json.loads(raw)
        body = result.get("body")
        answered = result.get("answered_questions")
        unavailable = result.get("unavailable_questions")
        addressed_constraints = result.get("addressed_constraints")
        unavailable_constraints = result.get("unavailable_constraints")
        expected = set(range(len(rfq.customer_questions)))
        expected_constraints = set(range(len(rfq.service_constraints)))
        if not isinstance(body, str) or not body.strip():
            raise ValueError("empty body")
        coverage_lists = (
            answered,
            unavailable,
            addressed_constraints,
            unavailable_constraints,
        )
        if not all(isinstance(items, list) for items in coverage_lists):
            raise TypeError("missing coverage lists")
        answered_set, unavailable_set = set(answered), set(unavailable)
        if answered_set & unavailable_set or answered_set | unavailable_set != expected:
            raise ValueError("question coverage is incomplete or overlapping")
        addressed_set = set(addressed_constraints)
        constraint_unavailable_set = set(unavailable_constraints)
        if (
            addressed_set & constraint_unavailable_set
            or addressed_set | constraint_unavailable_set != expected_constraints
        ):
            raise ValueError("constraint coverage is incomplete or overlapping")
        if body.count(price) != 1 or body.upper().count("EUR") != 1:
            raise ValueError("answer must contain exactly the verified EUR total")
        return body.rstrip() + "\n"
    except Exception as e:  # noqa: BLE001 - quote has a safe deterministic fallback
        log.warning("session %s: using deterministic quote copy (%s)", session_id, e)
        return fallback


# ---------------------------------------------------------------- flow


def _received(mail: Mail) -> float:
    try:
        return datetime.fromisoformat(mail.received_at).timestamp()
    except ValueError:
        return time.time()


def _bid_carriers(rfq: Rfq, origin: str) -> tuple[list[Carrier], list[Carrier]]:
    maximum = max(1, settings.max_bid_carriers)
    minimum = min(maximum, max(0, settings.min_bid_carriers))
    truckers = truckers_for_zone(zone_for_zip(rfq.pickup_zip)) if rfq.pickup_zip else []
    truckers = sorted(truckers, key=lambda carrier: carrier.name)[:maximum]
    airline_candidates: list[tuple[bool, Carrier]] = []
    for a in airlines_for(origin, rfq.destination):
        if a.channel != "email_only" or not a.email:
            continue
        # skip an airline whose own lane list (from an earlier refusal) lacks this lane
        observation = served_lanes.serves(a.email, origin, rfq.destination)
        if observation is False:
            continue
        c = carrier_by_email(a.email)
        if c is not None:
            airline_candidates.append((observation is True, c))
    airline_candidates.sort(key=lambda item: (not item[0], item[1].name))
    slots = max(0, maximum - len(truckers))
    known = [carrier for observed, carrier in airline_candidates if observed]
    unknown = [carrier for observed, carrier in airline_candidates if not observed]
    airlines = known[:slots]
    target = min(maximum, max(minimum, len(truckers) + len(airlines)))
    unknown_slots = max(0, target - len(truckers) - len(airlines))
    airlines.extend(unknown[:unknown_slots])
    return truckers, airlines


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
    rfq = extract_rfq(mail, sid)
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

    full_deadline = t0 + settings.quote_full_score_deadline_s
    final_deadline = t0 + min(settings.quote_final_deadline_s, TOO_OLD_S - 1)
    pricing_deadline = max(time.time(), full_deadline - COMPOSE_RESERVE_S)
    last_bid_deadline = max(pricing_deadline, final_deadline - COMPOSE_RESERVE_S)
    portal = portal_result(
        rfq, deadline=time.monotonic() + max(0, pricing_deadline - time.time())
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
    requested_emails = {carrier.email for carrier in truckers + email_airlines}
    remaining_slots = max(0, settings.max_bid_carriers - len(requested_emails))
    fallback = [
        carrier for carrier in fallback if carrier.email not in requested_emails
    ]
    fallback = fallback[:remaining_slots]
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

    bids.wait(sid, pricing_deadline - time.time())
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
            f"Warte bis {final_deadline - t0:.0f} s auf: {', '.join(sorted(need))}",
            sid,
        )
        bids.wait(sid, last_bid_deadline - time.time(), until_kinds=need)

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
        if not settings.allow_incomplete_door_quote:
            db.emit(
                "no_trucking",
                "Kein Trucker-Gebot: unvollständiges Door-to-Port-Angebot nicht gesendet",
                sid,
                level="error",
            )
            db.set_status(sid, "error", "kein vollständiger Door-to-Port-Preis")
            return
        db.emit(
            "no_trucking", "Kein Trucker-Gebot: Angebot ohne Vorlauf", sid, level="warn"
        )
    total = choice.total_eur_minor + (to_minor(truck.total_eur) if truck else 0)

    body = compose_quote_text(rfq, choice, total, truck, sid)
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


def _stored_mail(row: dict[str, Any], session_id: int) -> Mail:
    return Mail(
        id=int(row["id"]),
        thread_root=session_id,
        sender=str(row.get("sender") or ""),
        to=str(row.get("recipient") or settings.team_mailbox),
        subject=str(row.get("subject") or ""),
        body=str(row.get("body") or ""),
        received_at=str(row.get("received_at") or ""),
    )


def _resume(mail: Mail, session_id: int, replies: list[Mail]) -> None:
    for reply in replies:
        bids.on_bid_mail(reply, session_id)
    handle_rfq(mail, session_id)


def catch_up() -> None:
    """Resume recent, unanswered RFQs after restart without duplicating carrier requests."""
    seen: set[int] = set()
    for status in ("new", "pricing", "error"):
        for summary in db.list_sessions(status=status):
            sid = int(summary["id"])
            if (
                sid in seen
                or summary.get("first_reply_at")
                or summary.get("mode") == "manual"
            ):
                continue
            seen.add(sid)
            full = db.get_session(sid)
            if not full or full.get("quotes") or full.get("paused"):
                continue
            incoming = [m for m in full["mails"] if m["direction"] == "in"]
            originals = [m for m in incoming if m.get("category") == "rfq"]
            if not originals:
                continue
            mail = _stored_mail(originals[0], sid)
            if time.time() - _received(mail) > TOO_OLD_S:
                continue
            replies = [
                _stored_mail(row, sid)
                for row in incoming
                if row.get("category") == "carrier_bid"
            ]
            db.emit("recovery", "Offene Anfrage nach Neustart wieder aufgenommen", sid)
            threading.Thread(
                target=_resume,
                args=(mail, sid, replies),
                name=f"rfq-recover-{sid}",
                daemon=True,
            ).start()
