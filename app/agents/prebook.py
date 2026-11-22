"""Pre-booking questions: the customer answers our quote with questions instead of a yes.

Round 54 asked "Before we book, could you confirm whether this is the fastest option to DEL
with pickup from 22842 on 28 November? What's the earliest arrival with this option?" and got
only "shall we book?" back. With PREBOOK_ANSWER on we answer every question from what the
quote stored and ask for the go-ahead in the same mail. Rules only, no LLM, and the mail skips
the LLM style reviewer: nothing can alter a fact between this module and the wire.

- `parse` reads the questions. Every question sentence must name a known topic; one we cannot
  place leaves the whole reply to today's clarification path.
- `facts_for` reads the stored quote: airline, transit days, all-in total, validity, the
  schedule from `rfq.schedule` (the function the quote used, as of the quote day), the pickup
  site and the trucker's lead time from the quote's pricing audit, and the alternatives.
- `answer` writes the reply, or says why it cannot. It never offers a second quote, another
  price or another option; the all-in total appears at most once and no other amount at all.
  A question we hold no fact for (cutoff, routing) is promised at booking, never invented.

A question is never consent: booking still needs a clear yes (booking.is_acceptance).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from app.mail.style import SIGNATURE, amount, written_date
from app.models import Rfq
from app.quotes import active

CATEGORY = "prebook_answer"

Topic = Literal[
    "fastest",
    "cheapest",
    "schedule",
    "transit",
    "airline",
    "pickup",
    "validity",
    "includes",
    "price",
    "cutoff",
    "routing",
]

# Order matters only for overlaps inside one sentence; the answer follows the customer's order.
_TOPICS: tuple[tuple[Topic, re.Pattern[str]], ...] = (
    (
        "fastest",
        re.compile(
            r"\b(?:fastest|quickest|faster|quicker|schnellst\w*|schneller\w*)\b", re.IGNORECASE
        ),
    ),
    (
        "cheapest",
        re.compile(
            r"\b(?:cheapest|cheaper|lowest\s+(?:price|rate|cost|option)|least\s+expensive|"
            r"best\s+price|günstigst\w*|günstiger\w*|billigst\w*|billiger\w*|preiswert\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "schedule",
        re.compile(
            r"\b(?:arriv\w*|eta|etd|depart\w*|leaves?|leaving|fly\s+out|flight\s+date\w*|"
            r"lands?|landing|ankunft\w*|ankommen|ankommt|eintreff\w*|abflug\w*|abfliegen|"
            r"abfliegt|landet|frühest\w*|wann\s+(?:kommt|käme|fliegt|geht|landet))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "transit",
        re.compile(
            r"\b(?:transit|how\s+long|how\s+many\s+days|laufzeit\w*|transitzeit\w*|"
            r"wie\s+lange|wie\s+viele\s+tage)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "airline",
        re.compile(r"\b(?:airlines?|carriers?|fluggesellschaft\w*|fluglinie\w*)\b", re.IGNORECASE),
    ),
    ("pickup", re.compile(r"\b(?:pick\s*-?\s*up|collect\w*|abhol\w*|abgeholt)\b", re.IGNORECASE)),
    (
        "validity",
        re.compile(
            r"\b(?:valid\w*|expir\w*|hold\s+(?:the|this)\s+(?:price|rate|offer)|"
            r"how\s+long\s+(?:is|will|does)\s+(?:the|this)\s+(?:price|rate|offer|quote)|"
            r"gültig\w*|bis\s+wann\s+gilt|gilt\s+(?:das|der|dieser|dieses)\s+(?:angebot|preis))\b",
            re.IGNORECASE,
        ),
    ),
    (
        # round 144: "Could you match their rate?" then "does your price stay as offered, or
        # what is your new all-in total?" got our generic "shall we book?" twice and was lost
        "price",
        re.compile(
            r"\bmatch\w*\s+(?:their|the|this|that|his|her|a|its)?\s*(?:rate|price|offer|quote)|"
            # round 157: "another forwarder is 7% cheaper. Can you match that? Please let me
            # know where your price stands." ("match that date?" names a date, not a price)
            r"\bmatch(?:es|ing)?\s+(?:that|it|this|them)\s*[?.!]*\s*$|"
            r"\b(?:price|rate|offer|quote)\s+stands\b|"
            r"\b(?:price|rate|offer|total)\s+(?:stays?|remains?)\b|\bstays?\s+as\s+offered|"
            r"\bnew\s+(?:all\s*-?\s*in\s+)?(?:total|price|rate)|"
            r"\b(?:better|lower|reduced?)\s+(?:price|rate|total)|\bdiscount\w*|"
            r"\brabatt\w*|\bnachlass\w*|\bpreis\s+(?:halten|senken|anpassen|bleibt)|"
            r"\bpreisnachlass\w*",
            re.IGNORECASE,
        ),
    ),
    (
        "includes",
        re.compile(
            r"\b(?:includ\w*|inclusive|all\s*-?\s*in|surcharges?|fuel|final\s+price|covers?|"
            r"inklusive|enthalten|inbegriffen|beinhalt\w*|zuschläge?|zuschlag|endpreis)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "cutoff",
        re.compile(
            r"cut\s*-?\s*off|annahmeschluss|anlieferzeit|"
            r"\b(?:latest|last|spätest\w*|letzte\w*)\b[^.?!\n]{0,30}"
            r"\b(?:drop\s*-?\s*off|delivery|hand\s*-?\s*over|anliefer\w*|abgabe\w*|übergabe\w*)|"
            r"\bdrop\s*-?\s*off\s+(?:time|deadline)",
            re.IGNORECASE,
        ),
    ),
    (
        "routing",
        re.compile(
            r"\b(?:direct\s+flight|non\s*-?\s*stop|stopovers?|layovers?|routing|flight\s+number|"
            r"direktflug|umsteig\w*|zwischenstopp\w*|flugnummer|flugroute)\b",
            re.IGNORECASE,
        ),
    ),
)
# "Is pickup included?" asks about the pickup, not about what the price covers
_INCLUDED_WORD = re.compile(r"^(?:includ\w*|inklusive|enthalten|inbegriffen)$", re.IGNORECASE)
_HOW_LONG = re.compile(r"^(?:how\s+long|wie\s+lange)$", re.IGNORECASE)
_ASK = re.compile(
    r"\b(?:could|can|would)\s+you\s+(?:please\s+)?(?:confirm|tell|let\s+(?:me|us)\s+know|check|"
    r"advise)|\bplease\s+(?:confirm|advise|let\s+(?:me|us)\s+know|tell)|"
    r"\b(?:können|könnten|würden)\s+sie\s+(?:mir\s+|uns\s+)?(?:bitte\s+)?"
    r"(?:bestätigen|sagen|mitteilen)|\bbitte\s+(?:bestätigen|teilen)\s+sie|"
    r"\bteilen\s+sie\s+(?:mir|uns)\s+(?:bitte\s+)?mit",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])(?<!\d\.)\s+|\n+")  # "am 28. November" goes on
_ZIP = re.compile(r"(?<![\d.,])\d{5}(?![\d.,])(?!\s*(?:kg|kilo))", re.IGNORECASE)
_CODE = re.compile(r"\b[A-Z]{3}\b")
_NOT_AIRPORTS = {"ETA", "ETD", "EUR", "USD", "UTC", "CET", "ZIP", "AWB", "PDF", "DGR", "SDS"}
_MONTHS = {
    "january": 1, "januar": 1, "jan": 1, "february": 2, "februar": 2, "feb": 2, "march": 3,
    "märz": 3, "maerz": 3, "mar": 3, "april": 4, "apr": 4, "may": 5, "mai": 5, "june": 6,
    "juni": 6, "jun": 6, "july": 7, "juli": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oktober": 10, "oct": 10, "okt": 10,
    "november": 11, "nov": 11, "december": 12, "dezember": 12, "dec": 12, "dez": 12,
}  # fmt: skip
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})\.?\s+({_MONTH})\b\.?", re.IGNORECASE)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


@dataclass(frozen=True)
class Asked:
    """What the customer asked, in their order. `pickup_dates` are dates named in a pickup
    question; `zips` and `airports` are every postcode and airport code they named."""

    topics: tuple[Topic, ...]
    zips: tuple[str, ...]
    airports: tuple[str, ...]
    pickup_dates: tuple[tuple[int, int, int | None], ...]  # (day, month, year or None)


def _sentence_topics(sentence: str) -> list[tuple[int, Topic]]:
    found: list[tuple[int, Topic]] = []
    hits: dict[Topic, list[re.Match[str]]] = {}
    for topic, pattern in _TOPICS:
        matches = list(pattern.finditer(sentence))
        if topic == "includes" and "pickup" in hits:
            matches = [m for m in matches if not _INCLUDED_WORD.match(m.group(0))]
        if matches:
            hits[topic] = matches
            found.append((matches[0].start(), topic))
    # "what is your new all-in total?" asks about the price, not what the price covers
    if "price" in hits:
        found = [(pos, t) for pos, t in found if t != "includes"]
    # "how long is it valid?" asks about the validity, not the transit time
    if "validity" in hits and all(_HOW_LONG.match(m.group(0)) for m in hits.get("transit", [])):
        found = [(pos, t) for pos, t in found if t != "transit"]
    return sorted(found)


def _dates(sentence: str) -> list[tuple[int, int, int | None]]:
    out: list[tuple[int, int, int | None]] = []
    for m in _ISO_DAY.finditer(sentence):
        out.append((int(m[3]), int(m[2]), int(m[1])))
    for m in _DAY_MONTH.finditer(sentence):
        out.append((int(m[1]), _MONTHS[m[2].lower()], None))
    for m in _MONTH_DAY.finditer(sentence):
        out.append((int(m[2]), _MONTHS[m[1].lower()], None))
    return out


def is_question(sentence: str) -> bool:
    return sentence.rstrip().endswith("?") or bool(_ASK.search(sentence))


def parse(text: str) -> Asked | None:
    """The customer's questions, or None: no question at all, or a question sentence without
    a topic we can answer (a price request, a change, small talk). None keeps today's path."""
    topics: list[tuple[int, Topic]] = []
    pickup_dates: list[tuple[int, int, int | None]] = []
    offset = 0
    questions = 0
    for raw in _SENTENCE.split(text):
        sentence = raw.strip()
        at = text.find(sentence, offset) if sentence else offset
        offset = max(offset, at + len(sentence))
        if not sentence or not is_question(sentence):
            continue
        questions += 1
        found = _sentence_topics(sentence)
        if not found:
            return None
        topics += [(at + pos, t) for pos, t in found]
        if any(t == "pickup" for _, t in found):
            pickup_dates += _dates(sentence)
    if not questions:
        return None
    ordered: list[Topic] = []
    for _, t in sorted(topics):
        if t not in ordered:
            ordered.append(t)
    airports = tuple(c for c in dict.fromkeys(_CODE.findall(text)) if c not in _NOT_AIRPORTS)
    return Asked(
        topics=tuple(ordered),
        zips=tuple(dict.fromkeys(_ZIP.findall(text))),
        airports=airports,
        pickup_dates=tuple(pickup_dates),
    )


# ---------------------------------------------------------------- facts


@dataclass(frozen=True)
class Option:
    airline: str
    transit_days: int
    total_eur_minor: int


@dataclass(frozen=True)
class Facts:
    """Everything the answer may state, read from the stored quote of the thread."""

    airline: str
    origin: str
    destination: str
    transit_days: int
    total_minor: int
    currency: str
    preference: str
    valid_until: datetime | None
    departure: date | None
    arrival: date | None
    door: bool
    pickup_label: str | None
    pickup_zip: str | None
    pickup_day: date | None
    ready_day: date | None
    truck_lead_hours: int | None
    chosen: Option | None  # the quoted air option among `options`, None if not identifiable
    options: tuple[Option, ...]  # every air option the quote was chosen from


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _stamp(raw: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _options(quote: dict[str, Any]) -> tuple[tuple[Option, ...], Option | None]:
    options: list[Option] = []
    chosen: Option | None = None
    for alt in quote.get("alternatives") or []:
        if not isinstance(alt, dict) or "trucker" in alt:
            continue
        try:
            option = Option(
                str(alt["airline"]), int(alt["transit_days"]), int(alt["total_eur_minor"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        options.append(option)
        air_total = quote.get("air_total_minor")
        same_price = air_total is not None and int(alt.get("total_minor") or -1) == int(air_total)
        # legacy quotes froze no air price: the airline and transit time identify it
        same_legacy = air_total is None and option.transit_days == quote.get("transit_days")
        if (
            chosen is None
            and option.airline == quote.get("airline")
            and (same_price or same_legacy)
        ):
            chosen = option
    return tuple(options), chosen


def _truck_lead_hours(session: dict[str, Any]) -> int | None:
    audits = [e for e in session.get("events") or [] if e.get("kind") == "pricing_audit"]
    trucking = ((audits[-1].get("payload") or {}).get("trucking") or {}) if audits else {}
    days = trucking.get("transit_days") if isinstance(trucking, dict) else None
    return round(float(days) * 24) if days is not None else None


def facts_for(session: dict[str, Any]) -> Facts | None:
    """Facts of the thread's first quote (the only one that counts), or None without one."""
    from app.agents.rfq import schedule

    quotes = session.get("quotes") or []
    if not quotes:
        return None
    quote = active(session)
    if quote is None:
        return None
    request = quote.get("request_snapshot") or session.get("request") or {}
    if not quote.get("airline") or quote.get("transit_days") is None:
        return None
    rfq = Rfq.model_construct(
        ready_date=request.get("ready_date"),
        pickup_zip=request.get("pickup_zip"),
        pickup_city=request.get("pickup_city"),
    )
    quoted_on = _day(quote.get("sent_at")) or datetime.now(UTC).date()
    when = schedule(rfq, int(quote["transit_days"]), quoted_on)
    departure, arrival = when if when else (None, None)
    door = rfq.door_to_port
    options, chosen = _options(quote)
    return Facts(
        airline=str(quote["airline"]),
        origin=str(quote.get("origin") or ""),
        destination=str(quote.get("destination") or ""),
        transit_days=int(quote["transit_days"]),
        total_minor=int(quote["total_minor"]),
        currency=str(quote.get("currency") or "EUR"),
        preference=str(request.get("preference") or "cheapest"),
        valid_until=_stamp(quote.get("valid_until")) if quote.get("valid_until") else None,
        departure=departure,
        arrival=arrival,
        door=door,
        pickup_label=rfq.pickup_label,
        pickup_zip=request.get("pickup_zip"),
        pickup_day=departure - timedelta(days=1) if door and departure else None,
        ready_day=_day(request.get("ready_date")) if request.get("ready_date") else None,
        truck_lead_hours=_truck_lead_hours(session) if door else None,
        chosen=chosen,
        options=options,
    )


# ---------------------------------------------------------------- answer


@dataclass(frozen=True)
class Answer:
    body: str | None  # the mail, or None: leave the reply to today's path
    reason: str  # why not, for the timeline; "answered" when body is set
    topics: tuple[Topic, ...] = ()


def _days(n: int, lang: str) -> str:
    if lang == "de":
        return f"{n} {'Tag' if n == 1 else 'Tage'}"
    return f"{n} {'day' if n == 1 else 'days'}"


def _days_dative(n: int) -> str:
    return f"{n} {'Tag' if n == 1 else 'Tagen'}"


def _fits(asked: Asked, f: Facts) -> str | None:
    """Why the questions name something other than the quoted shipment, or None."""
    for code in asked.airports:
        if code not in (f.origin, f.destination):
            return f"other airport {code}"
    for zip_ in asked.zips:
        if not f.door or zip_ != (f.pickup_zip or ""):
            return f"other pickup postcode {zip_}"
    for day, month, year in asked.pickup_dates:
        targets = [d for d in (f.pickup_day, f.ready_day) if d is not None]
        if not any(
            d.day == day and d.month == month and (year is None or d.year == year) for d in targets
        ):
            return f"other pickup date {day}.{month}."
    return None


def _compare(f: Facts, topic: Topic, lang: str) -> tuple[str | None, str]:
    """(sentence, reason) for 'is this the fastest / cheapest?'."""
    de = lang == "de"
    if f.chosen is None or not f.options:
        return None, "quoted option not found among stored alternatives"
    others = [o for o in f.options if o != f.chosen]
    n = f.transit_days
    if not others:
        if de:
            return (
                f"Es ist die einzige Verbindung, die wir nach {f.destination} kalkulieren "
                f"konnten, mit {_days_dative(n)} Laufzeit."
            ), "answered"
        return (
            f"It is the only connection we could price to {f.destination}, with "
            f"{_days(n, lang)} in transit."
        ), "answered"
    if topic == "fastest":
        best = min(o.transit_days for o in f.options)
        if f.chosen.transit_days <= best:
            if de:
                return (
                    f"Ja, das ist die schnellste Verbindung, die wir nach {f.destination} "
                    f"gefunden haben, mit {_days_dative(n)} Laufzeit."
                ), "answered"
            return (
                f"Yes, this is the fastest option we found to {f.destination}, with "
                f"{_days(n, lang)} in transit."
            ), "answered"
        if f.preference == "fastest":
            return None, "asked fastest, quoted for fastest, yet a faster option was stored"
        if de:
            return (
                "Nicht ganz. Sie hatten nach der günstigsten Option gefragt, deshalb habe ich "
                f"{f.airline} als günstigsten Preis angeboten, mit {_days_dative(n)} Laufzeit. "
                f"Die schnellste Verbindung, die wir gesehen haben, braucht {_days(best, lang)}."
            ), "answered"
        return (
            "Not quite. You asked for the cheapest option, so I quoted "
            f"{f.airline} as the lowest price we found, with {_days(n, lang)} in transit. "
            f"The fastest connection we saw takes {_days(best, lang)}."
        ), "answered"
    cheapest = min(o.total_eur_minor for o in f.options)
    if f.chosen.total_eur_minor <= cheapest:
        if de:
            return (
                f"Ja, {f.airline} ist die günstigste Option, die wir für diese Sendung "
                "gefunden haben."
            ), "answered"
        return f"Yes, {f.airline} is the cheapest option we found for this shipment.", "answered"
    if f.preference == "cheapest":
        return None, "asked cheapest, quoted for cheapest, yet a cheaper option was stored"
    slower = min(o.transit_days for o in f.options if o.total_eur_minor == cheapest)
    if de:
        return (
            "Nicht ganz. Sie hatten nach der schnellsten Option gefragt, deshalb habe ich "
            f"{f.airline} mit {_days_dative(n)} Laufzeit angeboten, die schnellste, die wir "
            f"gefunden haben. Eine günstigere Verbindung gibt es, sie braucht aber "
            f"{_days(slower, lang)}."
        ), "answered"
    return (
        "Not quite. You asked for the fastest option, so I quoted "
        f"{f.airline} with {_days(n, lang)} in transit, the quickest we found. A cheaper "
        f"connection exists, but it takes {_days(slower, lang)}."
    ), "answered"


# Round 147: "Could you please confirm that this is the fastest option from STR to MEL? If
# so, please go ahead with the booking." A yes on a condition the stored quote proves.
_IF_SO = re.compile(
    r"\b(?:if\s+(?:so|yes|that\s+is\s+(?:so|the\s+case|correct|right)|"
    r"that's\s+(?:so|the\s+case|correct|right)|this\s+is\s+(?:so|the\s+case|correct)|"
    r"it\s+is|confirmed)|(?:wenn|falls)\s+(?:ja|das\s+(?:so\s+ist|stimmt|zutrifft)))\b"
    r"[^.?!\n]{0,60}?\b(?:go\s+ahead|proceed|book\w*|buchen|bucht|buchung)",
    re.IGNORECASE,
)
# anything that holds the booking back keeps today's path ("please wait until I confirm")
_HELD = re.compile(
    r"\bwait\w*|\buntil\b|\bbefore\s+(?:we|i)\b|\bnot\b|n't\b|\bno\b|\bhold\b|"
    r"\bwarten\b|\bbevor\b|\bnicht\b|\bnoch\s+nicht\b|\bkein\w*",
    re.IGNORECASE,
)
_PROVABLE: frozenset[Topic] = frozenset({"fastest", "cheapest"})


def _holds(f: Facts, topic: Topic) -> bool:
    """The quoted option is the fastest (cheapest) of every stored air option."""
    if f.chosen is None or not f.options:
        return False
    if topic == "fastest":
        return f.chosen.transit_days <= min(o.transit_days for o in f.options)
    return f.chosen.total_eur_minor <= min(o.total_eur_minor for o in f.options)


def proven_go_ahead(text: str, facts: Facts | None) -> bool:
    """True when the customer's own words book on a condition the stored quote proves: every
    question before "if so, please go ahead" asks only whether this is the fastest or the
    cheapest option of the quoted shipment, and the stored alternatives say it is. Anything
    else (another topic, another airport, a further question, a hold word, a condition we
    cannot prove) is no consent here."""
    m = _IF_SO.search(text)
    if facts is None or m is None or _HELD.search(text):
        return False
    asked = parse(text[: m.start()])
    if asked is None or not asked.topics or set(asked.topics) - _PROVABLE:
        return False
    if parse(text[m.end() :]) is not None or _fits(asked, facts):
        return False
    return all(_holds(facts, t) for t in asked.topics)


def _clock(stamp: datetime, lang: str) -> str:
    """'18:04 UTC on 21 November 2026'; the final renderer writes the clock as prose."""
    utc = stamp.astimezone(UTC)
    joiner = "am" if lang == "de" else "on"
    return f"{utc.strftime('%H:%M')} UTC {joiner} {written_date(utc.date(), lang)}"


def _sentence(f: Facts, topic: Topic, lang: str, now: datetime) -> tuple[str | None, str]:
    de = lang == "de"
    o, d, n = f.origin, f.destination, f.transit_days
    if topic in ("fastest", "cheapest"):
        return _compare(f, topic, lang)
    if topic == "schedule":
        if f.departure and f.arrival:
            dep, arr = written_date(f.departure, lang), written_date(f.arrival, lang)
            if de:
                return f"Frühester Abflug ab {o} ist am {dep}, Ankunft in {d} am {arr}.", "answered"
            return (
                f"The earliest departure from {o} is {dep}, arriving in {d} on {arr}."
            ), "answered"
        if de:
            return (
                f"Die Laufzeit beträgt {_days(n, lang)} ab Abflug. Die genauen Flugdaten "
                "bestätige ich bei der Buchung mit der Airline."
            ), "answered"
        return (
            f"The transit time is {_days(n, lang)} from departure. I will confirm the exact "
            "flight dates with the airline when I book."
        ), "answered"
    if topic == "transit":
        if de:
            return f"Die Laufzeit beträgt {_days(n, lang)} ab Abflug.", "answered"
        return f"The transit time is {_days(n, lang)} from departure.", "answered"
    if topic == "airline":
        if de:
            return f"Die Sendung fliegt mit {f.airline}.", "answered"
        return f"The shipment flies with {f.airline}.", "answered"
    if topic == "pickup":
        if not f.door:
            if de:
                return (
                    f"Das Angebot gilt für Luftfracht ab dem Flughafen {o} und enthält keine "
                    "Abholung."
                ), "answered"
            return (
                f"This offer is for air freight from {o} airport and does not include a pickup."
            ), "answered"
        parts: list[str]
        if de:
            parts = [
                (
                    f"Ja, im Preis sind die Abholung in {f.pickup_label} und der Transport zum "
                    f"Flughafen {o} enthalten."
                )
            ]
            parts.append(
                f"Geplant ist die Abholung am {written_date(f.pickup_day, lang)}, den genauen "
                "Abholtermin bestätige ich Ihnen nach der Buchung."
                if f.pickup_day
                else "Den genauen Abholtermin bestätige ich Ihnen nach der Buchung."
            )
            if f.truck_lead_hours is not None:
                parts.append(f"Der Trucker nennt etwa {f.truck_lead_hours} Stunden Vorlauf.")
        else:
            parts = [
                f"Yes, the price includes pickup from {f.pickup_label} and trucking to {o} airport."
            ]
            parts.append(
                f"The plan is to collect it on {written_date(f.pickup_day, lang)}, and I will "
                "confirm the exact pickup time once it is booked."
                if f.pickup_day
                else "I will confirm the exact pickup time once it is booked."
            )
            if f.truck_lead_hours is not None:
                parts.append(
                    f"The trucker reports a lead time of about {f.truck_lead_hours} hours."
                )
        return " ".join(parts), "answered"
    if topic == "validity":
        if de:
            change = (
                "Falls sich der Preis der Airline bei der Buchung geändert hat, frage ich Sie, "
                "bevor etwas bestätigt wird."
            )
        else:
            change = (
                "If the airline's price has changed when I book, I will check with you before "
                "anything is confirmed."
            )
        if f.valid_until is None:
            if de:
                return f"Für diese Rate habe ich keine feste Ablaufzeit. {change}", "answered"
            return f"I have no fixed expiry time for this rate. {change}", "answered"
        when = _clock(f.valid_until, lang)
        if f.valid_until > now:
            if de:
                return f"Der Preis ist bis {when} buchbar.", "answered"
            return f"The price is bookable until {when}.", "answered"
        if de:
            return f"Der Preis war bis {when} reserviert. {change}", "answered"
        return f"The rate was held until {when}. {change}", "answered"
    if topic == "includes":
        total = amount(f.total_minor, f.currency)
        if de:
            text = (
                f"Der Gesamtpreis von {total} ist der volle Preis für diese Sendung. Er umfasst "
                f"die Luftfracht von {o} nach {d}"
            )
            text += (
                f", die Abholung in {f.pickup_label} und den Transport zum Flughafen {o}."
                if f.door
                else "."
            )
            return text, "answered"
        text = f"The total of {total} is the full price for this shipment. It covers the air freight from {o} to {d}"
        text += (
            f", the pickup from {f.pickup_label} and the trucking to {o} airport."
            if f.door
            else "."
        )
        return text, "answered"
    if topic == "price":
        total = amount(f.total_minor, f.currency)
        if de:
            return (
                f"Unser Gesamtpreis von {total} bleibt wie angeboten. Günstiger können wir "
                "diese Sendung leider nicht anbieten."
            ), "answered"
        return (
            f"Our all-in total of {total} stays as offered. We cannot go below it for this "
            "shipment."
        ), "answered"
    if topic == "cutoff":
        if de:
            return (
                f"Der späteste Anlieferzeitpunkt in {o} ist noch nicht bestätigt. Den kläre ich "
                "bei der Buchung mit der Airline."
            ), "answered"
        return (
            f"The latest drop off time at {o} is not confirmed yet. I will confirm it with the "
            "airline when I book."
        ), "answered"
    # routing
    if de:
        return (
            "Die genaue Flugroute liegt mir noch nicht vor. Die Flugdetails bestätige ich bei "
            "der Buchung mit der Airline."
        ), "answered"
    return (
        "I do not have the flight routing yet. I will confirm the flight details with the "
        "airline when I book."
    ), "answered"


def answer(
    text: str,
    facts: Facts | None,
    lang: str,
    name: str | None,
    now: datetime | None = None,
) -> Answer:
    """The reply to the customer's own words `text` (quoted history already removed)."""
    asked = parse(text)
    if asked is None:
        return Answer(None, "no question we can place")
    if facts is None:
        return Answer(None, "no stored quote", asked.topics)
    lang = "de" if lang == "de" else "en"
    mismatch = _fits(asked, facts)
    if mismatch:
        return Answer(None, mismatch, asked.topics)
    now = now or datetime.now(UTC)
    paragraphs: list[str] = []
    for topic in asked.topics:
        sentence, reason = _sentence(facts, topic, lang, now)
        if sentence is None:
            return Answer(None, reason, asked.topics)
        paragraphs.append(sentence)
    if lang == "de":
        hello = f"Hallo {name}," if name else "Guten Tag,"
        opener = "danke für Ihre Rückfrage vor der Buchung."
        go = "Wenn alles passt, sag mir einfach Bescheid und ich kümmere mich um die Buchung."
    else:
        hello = f"Hi {name}," if name else "Hello,"
        opener = "thanks for checking before you book."
        go = "If everything fits, let me know and I will arrange the booking."
    body = "\n\n".join([hello, opener, *paragraphs, go, SIGNATURE]) + "\n"
    return Answer(body, "answered", asked.topics)


# ---------------------------------------------------------------- send

MIN_GAP_S = 10.0  # never two of our mails to the customer within this many seconds


def _age_s(stamp: Any, now: datetime) -> float:
    value = _stamp(stamp)
    return (now - value).total_seconds() if value else float("inf")


def mails_after_quote(session: dict[str, Any], categories: tuple[str, ...]) -> int:
    """Our mails to the customer after the first quote in the given categories."""
    from app.mail.router import address

    quote_mail = int((active(session) or {}).get("mail_id") or 0)
    customer = address(str(session.get("customer") or ""))
    return sum(
        1
        for m in session.get("mails") or []
        if m["direction"] == "out"
        and m.get("category") in categories
        and address(str(m.get("recipient") or "")) == customer
        and int(m["id"]) >= quote_mail
    )


def answered_before(session: dict[str, Any]) -> bool:
    return mails_after_quote(session, (CATEGORY,)) > 0


def reply(mail: Any, sid: int, session: dict[str, Any]) -> bool:
    """Answer a pre-booking question in the thread. True when the reply is handled here (sent,
    or deliberately not sent: paused, manual, quarantined, mail budget used up); False hands
    it back to today's clarification path. Caller holds the booking lock of the session."""
    import time

    from app import db, hackloop
    from app.agents import booking
    from app.config import settings
    from app.mail import multilingual
    from app.mail.router import address
    from app.mail.signature import first_name

    if db.reply_workflow(sid) is not None:
        return True  # the central owner uses answer(), never this legacy sender
    request = session.get("request") or {}
    lang = multilingual.mail_language(mail, request.get("language") or "en")
    name = request.get("customer_name") or first_name(mail.body)
    result = answer(booking.own_text(mail.body), facts_for(session), lang, name)
    if result.body is None:
        db.emit(
            "prebook_not_answered",
            f"Vorabfrage nicht automatisch beantwortet: {result.reason}",
            sid,
            level="warn" if result.topics else "info",
            topics=list(result.topics),
        )
        return False
    if db.reply_blocked(mail.id):
        db.emit("prebook_blocked", "Vorabfrage aus Quarantäne: keine Antwort", sid, level="warn")
        return True
    if db.is_paused(sid) or db.is_manual(sid):
        db.emit("prebook_held", "Vorabfrage: Session pausiert/manuell, keine Antwort", sid)
        return True
    # only our own earlier answers count: the 5 s nudges go out before most replies and
    # must never use up the answer to a direct customer question (round 152); the cap
    # still stops a question loop
    used = mails_after_quote(session, (CATEGORY,))
    if not session.get("workflow") and used >= settings.followup_max:
        db.emit(
            "prebook_budget",
            f"Vorabfrage nicht beantwortet: {used} Mails nach dem Angebot, Limit erreicht",
            sid,
            level="warn",
        )
        return True
    customer = address(str(session.get("customer") or mail.sender))
    now = datetime.now(UTC)
    ours = [
        m
        for m in session.get("mails") or []
        if m["direction"] == "out" and address(str(m.get("recipient") or "")) == customer
    ]
    if ours:
        wait = MIN_GAP_S - _age_s(ours[-1]["received_at"], now)
        if wait > 0:
            time.sleep(wait)  # the nudge waits on the same lock, so nothing slips in between
    subject = mail.subject if mail.subject.lower().startswith("re:") else f"Re: {mail.subject}"
    try:
        sent = hackloop.send_customer(
            customer,
            subject,
            result.body,
            in_reply_to=mail.id,
            session_id=sid,
            review=False,
            language_code=lang,
        )
    except hackloop.CustomerSendBlocked:
        db.emit("prebook_held", "Vorabfrage: Versand gesperrt (pausiert/manuell)", sid)
        return True
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=customer,
        subject=sent["subject"],
        body=sent["body"],
        category=CATEGORY,
        session_id=sid,
        sender=settings.team_mailbox,
        in_reply_to=mail.id,
        raw=sent,
    )
    db.emit(
        db.Ev.STATUS,
        f"Vorabfrage beantwortet ({', '.join(result.topics)}), Freigabe erfragt, nicht gebucht",
        sid,
    )
    return True
