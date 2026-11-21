"""Rule-based reading of an RFQ mail: the fields the LLM extraction still supplies alone.

Shadow only: nothing in the live flow reads this yet. `python -m app.mail.rules` compares it
with the stored LLM extractions of the replay corpus, field by field. A field is returned only
when the text names exactly one candidate; anything missing or ambiguous is None. So the rules
are written for precision, not for coverage. Weights, volumes and lots are not read here:
app/mail/lots.py and app/mail/rfq_validation.py own them.

Real phrasings (mails 265-2967):
    ... the fastest air freight option to ORD (Chicago, US) ...
    ... from MUC to LAX (Los Angeles, US) ... we will deliver it to Munich airport (MUC) ourselves
    - Destination airport: Toronto (YYZ), CA
    ... at our site in 51896 (Bergisch Gladbach), Germany. Please arrange the truck ...
    - Pickup: from our site at 21663 Lueneburg, Germany
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

Preference = Literal["cheapest", "fastest"]

# ---- airports. Codes must be upper case; the words around them may be in any case.
_CODE_CITY = re.compile(r"\b([A-Z]{3}) \([^()\n]{2,40},\s*[A-Z]{2}\)")  # ORD (Chicago, US)
_CITY_CODE = re.compile(r"\(([A-Z]{3})\),\s*[A-Z]{2}\b")  # Toronto (YYZ), CA
_LABEL = r"^\W*(?i:{words})\b[^:\n]*:[^\n]*?\b([A-Z]{{3}})\b"
_DEST_LABEL = re.compile(_LABEL.format(words="destination|zielflughafen|ziel"), re.MULTILINE)
_ORIGIN_LABEL = re.compile(
    _LABEL.format(words="origin|departure|abflughafen|abflug|abgangsflughafen"), re.MULTILINE
)
_TO = re.compile(r"\b(?i:to|nach)\s+([A-Z]{3})\b")
_ARRIVAL = re.compile(r"\b(?i:arriv\w+|ankunft)\s+(?i:at|in)\s+([A-Z]{3})\b")
_FROM = re.compile(r"\b(?i:from|ex|ab|von)\s+([A-Z]{3})\b")
_ROUTE = re.compile(r"\b([A-Z]{3})\s*(?:-|–|—|->|→|/|(?i:to|nach))\s*([A-Z]{3})\b")
# "we will deliver it to Munich airport (MUC) ourselves", "latest drop-off time at STR"
_SELF_DELIVERY = re.compile(
    r"\b(?i:deliver\w*|anliefer\w*)\b[^.!?\n]*?\b([A-Z]{3})\b[^.!?\n]*?\b(?i:ourselves|selbst)\b"
    r"|\b(?i:drop[- ]?off)\b[^.!?\n]*?\b(?i:at|in)\s+([A-Z]{3})\b"
)

# ---- pickup ZIP: five digits in a sentence about the pickup site, not a quantity
_ZIP = re.compile(
    r"(?<![\d.,-])\b(\d{5})\b(?![.,-]?\d)"
    r"(?!\s*(?:kg|kgs|kilos?|lbs?|cbm|m3|m³|eur|usd|€|\$|pieces?|pcs|stück|cartons?|units?)\b)",
    re.IGNORECASE,
)
_PICKUP = re.compile(
    r"\b(?:site|pick[- ]?up|collect\w*|truck\w*|premises|warehouse|facility|factory|plant"
    r"|abhol\w*|werk|standort|lager|plz|postal code|zip)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")

# ---- preference: only words that cannot mean anything else
_FASTEST = re.compile(
    r"\b(?:fastest|quickest|shortest transit|as (?:fast|quickly|soon) as possible"
    r"|schnellst\w*)\b",
    re.IGNORECASE,
)
_CHEAPEST = re.compile(
    r"\b(?:cheapest|lowest (?:price|cost|rate)|most economical|most affordable|least expensive"
    r"|g(?:ü|ue)nstigst\w*|billigst\w*)\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(20\d{2}-[01]\d-[0-3]\d)\b")


@dataclass(frozen=True)
class Fields:
    """What the rules read. None = the text does not name exactly one value."""

    destination: str | None = None  # IATA
    origin: str | None = None  # IATA, only an airport the customer names
    pickup_zip: str | None = None
    preference: Preference | None = None
    ready_date: str | None = None  # ISO date


def _only(values: Collection[str]) -> str | None:
    distinct = set(values)
    return next(iter(distinct)) if len(distinct) == 1 else None


def _airports(
    text: str, origins: Collection[str], destinations: Collection[str]
) -> tuple[str | None, str | None]:
    """(origin, destination). An origin must be one of our departure airports. A destination
    is a code in a destination phrase that is not also named as the departure airport."""
    routes = [m.groups() for m in _ROUTE.finditer(text) if m.group(1) in origins]
    from_codes = (
        _FROM.findall(text)
        + _ORIGIN_LABEL.findall(text)
        + [a or b for a, b in _SELF_DELIVERY.findall(text)]
        + [o for o, _ in routes]
    )
    origin_codes = {c for c in from_codes if c in origins}
    # a code next to "(City, CC)" or behind "Destination:" is an airport by its form; a bare
    # "to XXX" counts only for an airport we know as a destination
    strong = _CODE_CITY.findall(text) + _CITY_CODE.findall(text) + _DEST_LABEL.findall(text)
    weak = _TO.findall(text) + _ARRIVAL.findall(text) + [d for _, d in routes]
    dest_codes = {c for c in strong if c not in origin_codes} | {
        c for c in weak if c in destinations and c not in origin_codes and c not in origins
    }
    return _only(origin_codes), _only(dest_codes)


def _pickup_zip(body: str) -> str | None:
    found = [
        m.group(1)
        for sentence in _SENTENCE.split(body)
        if _PICKUP.search(sentence)
        for m in _ZIP.finditer(sentence)
    ]
    return _only(found)


def _preference(text: str) -> Preference | None:
    fast, cheap = _FASTEST.search(text), _CHEAPEST.search(text)
    if fast and not cheap:
        return "fastest"
    if cheap and not fast:
        return "cheapest"
    return None


def read(
    subject: str, body: str, origins: Collection[str], destinations: Collection[str]
) -> Fields:
    """Read one RFQ. `origins` and `destinations` are the airports of our lanes
    (`airports()` below); `body` is the customer's own text, without quoted mail."""
    text = f"{subject or ''}\n{body or ''}"
    origin, destination = _airports(text, origins, destinations)
    return Fields(
        destination=destination,
        origin=origin,
        pickup_zip=_pickup_zip(body or ""),
        preference=_preference(text),
        ready_date=_only(_ISO_DATE.findall(body or "")),
    )


def airports() -> tuple[frozenset[str], frozenset[str]]:
    """(origins, destinations) of our lanes: sheets, portal snapshot, pickup zones and the
    airlines' `serves` lists."""
    from app.pricing.portal_sync import load_portal_lanes
    from app.pricing.pricing import load_lanes
    from app.pricing.reference import carriers, pickup_zones

    portal = load_portal_lanes()["lanes"]
    sheets = load_lanes()
    origins = (
        frozenset(pickup_zones().values())
        | {lane.origin for lane in sheets}
        | {str(lane["origin"]) for lane in portal}
    )
    served = {code for c in carriers() if c.kind == "airline" for code in c.serves}
    destinations = (
        frozenset(served)
        | {lane.destination for lane in sheets}
        | {str(lane["destination"]) for lane in portal}
    )
    return origins, destinations


SHADOW_FIELDS = ("destination", "origin", "pickup_zip", "preference", "ready_date")


def compare(cases: list[dict]) -> dict[str, dict[str, list[int]]]:
    """Rules against the stored LLM extraction of each replay case, per field:
    same = both give the same value, differ = both give one and they differ,
    silent = the rules give none where the LLM has one, extra = only the rules give one.
    Values are session ids."""
    from app.mail.content import current_message

    origins, destinations = airports()
    out: dict[str, dict[str, list[int]]] = {
        f: {"same": [], "differ": [], "silent": [], "extra": []} for f in SHADOW_FIELDS
    }
    for case in cases:
        stored = case.get("llm_extraction")
        if not stored or not case.get("mails"):
            continue
        root = case["mails"][0]
        own = current_message(root["body"] or "", root["subject"] or "")
        got = read(own.subject, own.text, origins, destinations)
        for field in SHADOW_FIELDS:
            mine, theirs = getattr(got, field), stored.get(field)
            if field == "preference" and mine is None:
                mine = "cheapest"  # the extraction prompt's own default when nothing is said
            key = (
                "same"
                if mine == theirs and mine is not None
                else "differ"
                if mine is not None and theirs is not None
                else "silent"
                if theirs is not None
                else "extra"
                if mine is not None
                else None
            )
            if key:
                out[field][key].append(int(case["session_id"]))
    return out


def main() -> None:
    import json
    import sys
    from pathlib import Path

    default = Path(__file__).parents[2] / "tests" / "fixtures" / "replay" / "rfq_sessions.json"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    result = compare(json.loads(path.read_text("utf-8"))["cases"])
    for field, groups in result.items():
        total = sum(len(v) for v in groups.values())
        print(
            f"{field:12} same {len(groups['same'])}/{total}"
            + "".join(f"  {k} {v}" for k, v in groups.items() if k != "same" and v)
        )


if __name__ == "__main__":
    main()
