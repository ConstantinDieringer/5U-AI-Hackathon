"""Deterministic RFQ gate: a model extraction is a candidate, not verified input."""

import re
from decimal import Decimal

from app import config
from app.mail.content import current_message
from app.mail.lots import parse_lots
from app.models import Rfq, RfqDraft
from app.pricing.reference import find_area, origin_for_zip, pickup_zones

VERSION = 1
_NUM = r"\d+(?:[.,]\d+)?"
_PICKUP = re.compile(
    r"arrange.{0,30}(?:truck|pickup|collection)|including truck pickup|pickup is|pick.?up from|from our (?:site|warehouse|plant)|abholung|vorlauf",
    re.IGNORECASE,
)
# the customer brings the goods to the airport: "no need to arrange pickup" must not count as
# a pickup request (round 7237 asked for a ZIP four times although Weidner self-delivered)
_NO_PICKUP = re.compile(
    r"no need (?:for|to arrange) (?:a |any )?(?:pick.?up|truck|collection)|no pick.?up|without pick.?up"
    r"|(?:deliver|bring|drop|tender)[^\n.!?]{0,80}\b(?:ourselves|myself)\b|keine abholung"
    r"|selbst (?:zum|an den) flughafen|(?:liefern|bringen)[^\n.!?]{0,80}\bselbst\b",
    re.IGNORECASE,
)
_SIGNATURE = re.compile(
    r"\n\s*(?:best greetings|cheers|regards|thanks a lot|mit freundlichen|viele gr|many greetings)",
    re.IGNORECASE,
)
_ALIASES = {
    "FRA": "Frankfurt|EDDF",
    "HAM": "Hamburg|EDDH",
    "MUC": "Munich|München|EDDM",
    "DUS": "Düsseldorf|Duesseldorf|EDDL",
    "CGN": "Cologne|Köln|EDDK",
    "STR": "Stuttgart|EDDS",
}


def explicit_delivery_origin(mail, destination):
    """Recover a single supported self-delivery airport from current words, never history.

    The IATA code may follow an airport name (``Düsseldorf Airport (DUS)``); it need not
    immediately follow the word ``to``.
    """
    active = current_message(mail.body).facts
    supported = set(pickup_zones().values())
    if config.settings.self_delivery_recovery:
        airports = _stated_departures(active, destination, supported)
    else:  # today's rule (round 5844); it reads "the str" as STR and ignores a "not"
        pattern = re.compile(
            r"\b(?:deliver|bring|tender|liefern|bringen)\b[^\n.!?]{0,120}?"
            r"\b(?:to|nach|zum)\s+(?:the\s+)?([A-Z]{3})\b"
            r"[^\n.!?]{0,60}?\b(?:ourselves|myself|selbst)\b",
            re.IGNORECASE,
        )
        airports = {
            code.upper()
            for match in pattern.finditer(active)
            for code in re.findall(r"\b[A-Z]{3}\b", match[0], re.IGNORECASE)
            if code.upper() in supported
        }
    return next(iter(airports)) if len(airports) == 1 and destination not in airports else None


# "we'll deliver the goods to Cologne airport (CGN) ourselves", "drop the shipment off at CGN
# ourselves" (round 28): the airport may be a city name, a code in brackets, or follow "at"
_SELF_DELIVERY = re.compile(
    r"\b(?:deliver|bring|drop|tender|take|liefern|bringen)\b[^\n.!?]{0,120}?"
    r"\b(?:to|at|nach|zum|an)\b[^\n.!?]{0,60}?\b(?:ourselves|myself|selbst)\b",
    re.IGNORECASE,
)
# German word order puts "selbst" before the place: "Wir liefern die Ware selbst nach
# Frankfurt an", "bringen sie selbst zum Flughafen München"
_SELF_DELIVERY_DE = re.compile(
    r"\b(?:liefern|bringen)\b[^\n.!?]{0,80}?\bselbst\b[^\n.!?]{0,20}?"
    r"\b(?:nach|zum|zur|an den|an)\s+[^\n.!?,;]{0,40}",
    re.IGNORECASE,
)
# "the cheapest option from CGN to DFW": codes in capitals only, so words never count
_ROUTE = re.compile(r"\bfrom\s+([A-Z]{3})\s+(?:to|nach)\s+([A-Z]{3})\b")
# a negation in the clause before the verb, or between verb and "selbst" in German
_NEGATION = re.compile(
    r"\b(?:not|don['’]t|do not|cannot|can['’]t|won['’]t|nicht|kein|keine)\b", re.IGNORECASE
)
_CLAUSE_START = re.compile(r"[.!?;:\n,—–]")


def _negated(text, match):
    """True when "We do not deliver to FRA ourselves" or "liefern nicht selbst" negates it."""
    clause = _CLAUSE_START.split(text[: match.start()])[-1]
    return bool(_NEGATION.search(clause) or _NEGATION.search(match[0]))


def no_pickup(active):
    """The customer brings the goods to the airport. SELF_DELIVERY_RECOVERY skips negated
    wording: "We do not deliver to FRA ourselves, please arrange pickup" wants a pickup."""
    if not config.settings.self_delivery_recovery:
        return bool(_NO_PICKUP.search(active))
    return self_delivers(active)


def self_delivers(text):
    """No pickup wanted, negated self-delivery sentences excluded."""
    return any(
        re.match(r"(?:no|without|keine)\b", m[0], re.IGNORECASE) or not _negated(text, m)
        for m in _NO_PICKUP.finditer(text)
    )


_KG = re.compile(rf"({_NUM})\s*(?:kgs?|kilograms?|kilogramm)\b", re.IGNORECASE)


def free_text_answer(text, asked, destination, known=(), facts=None):
    """CLARIFY_ONCE: what a customer's free-text answer states for the fields we asked about.
    A bare airport code ("CGN", capitals only) or airport city ("Cologne"), a 5-digit ZIP in a
    supported zone, an area name for a pickup, a single "N kg" for the weight. Negated clauses
    never count, the destination is never a departure, and two candidates are no answer.
    THOUSANDS_EVIDENCE: "1,674 kg" is read by weight_reading against `known` and `facts`."""
    body = _SIGNATURE.split(text, 1)[0]
    supported = set(pickup_zones().values())
    codes, cities = set(), set()
    for clause in re.split(r"[.!?;\n]", body):
        if _NEGATION.search(clause):
            continue
        if re.search(
            r"\b(?:last time|previously|used to|usually|normally|letztes mal|früher|normalerweise)\b",
            clause,
            re.IGNORECASE,
        ):
            continue  # historical/usual airports do not answer the current shipment question
        codes |= {c for c in re.findall(r"\b[A-Z]{3}\b", clause) if c in supported}
        cities |= {
            code
            for code, names in _ALIASES.items()
            if re.search(rf"\b(?:{names})\b", clause, re.IGNORECASE)
        }
    codes.discard(destination)
    cities.discard(destination)
    zips = {z for z in re.findall(r"\b(?:D-)?(\d{5})\b", body) if origin_for_zip(z)}
    # "51147 Köln-Porz" is an address: the ZIP wins over the city in it
    airports = codes | (set() if zips else cities)
    found = {}
    if "origin" in asked:
        if not codes and len(zips) == 1:
            found["pickup_zip"] = next(iter(zips))  # the zone fixes the airport
        elif len(airports) == 1:
            found["origin"] = next(iter(airports))
    if "pickup_zip" in asked:
        if len(zips) == 1:
            found["pickup_zip"] = next(iter(zips))
        elif not zips and self_delivers(body) and len(airports) == 1:
            # round 28 answers to a ZIP question: "no pickup ZIP, we'll drop it at CGN ourselves"
            found.update(origin=next(iter(airports)), no_pickup=True)
        elif not zips and (area := find_area(body)):
            found["pickup_city"] = area
    if "weight_kg" in asked and len(weights := set(_KG.findall(body))) == 1:
        raw = next(iter(weights))
        if not config.settings.thousands_evidence:
            found["weight_kg"] = float(raw.replace(",", "."))  # today: "1,674 kg" is 1.674
        elif (value := weight_reading(raw, known=known, context=body, facts=facts)) is not None:
            found["weight_kg"] = float(value)
    return found


_SOURCE = {
    "attachment": r"attach\w*|packing\s+(?:spec\w*|list)|spec\w*\s+sheet|anhang|packliste",
    "body": r"(?:my|our|the)\s+(?:e-?mail|message|request)|meine[rn]?\s+mail|unsere[rn]?\s+mail",
}
_CORRECT = (
    r"(?:is|are|was|were|ist|sind)\s+(?:the\s+)?(?:correct|right|accurate|valid|binding"
    r"|richtig|korrekt|maßgeblich|gültig)\b|\bstimm(?:t|en)\b"
)


def confirmed_source(text, draft, fields):
    """CONFLICT_SOURCE_ANSWER: the answer to a mail/attachment conflict names the source that is
    right ("the attached packing specification is correct", rounds 33 to 35) instead of a
    number. Returns the confirmed source's value for each conflicting field. Nothing when the
    answer names both sources, negates the statement, or the attachments disagree."""
    body = _SIGNATURE.split(text, 1)[0]
    named = set()
    for clause in re.split(r"[.!?;,\n]|\b(?:but|aber)\b", body):
        if _NEGATION.search(clause) or re.search(
            r"\b(?:incorrect|wrong|falsch)\b", clause, re.IGNORECASE
        ):
            continue
        for source, words in _SOURCE.items():
            if re.search(rf"(?:{words})\b.{{0,60}}?(?:{_CORRECT})", clause, re.IGNORECASE):
                named.add(source)
    if len(named) != 1:
        return {}
    source = next(iter(named))
    found = {}
    for field in fields:
        observations = (draft.evidence.get(field) or {}).get("observations") or []
        values = {
            Decimal(str(value))
            for name, value in observations
            if (name == "body") == (source == "body") and name != "customer_correction"
        }
        if len(values) == 1:
            value = next(iter(values))
            found[field] = int(value) if field == "pieces" else float(value)
    return found


def _stated_departures(active, destination, supported):
    """Departure airports the current message states itself (SELF_DELIVERY_RECOVERY).
    Codes count in capitals only and negated sentences never count."""
    found = set()
    for pattern in (_SELF_DELIVERY, _SELF_DELIVERY_DE):
        for match in pattern.finditer(active):
            if _negated(active, match):
                continue
            found |= {c for c in re.findall(r"\b[A-Z]{3}\b", match[0]) if c in supported}
            found |= {
                code
                for code, names in _ALIASES.items()
                if code in supported and re.search(rf"\b(?:{names})\b", match[0], re.IGNORECASE)
            }
    found |= {
        origin for origin, to in _ROUTE.findall(active) if origin in supported and to == destination
    }
    return found


_THOUSANDS = r"\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?"
_UNIT = r"kgs?|kilos?|lbs?|t|tonnes?|tons?"
# UNIT_EVIDENCE: extra ways a mail states the totals (none of the 31 stored rounds uses them)
_UNIT_WEIGHT = [
    rf"({_THOUSANDS})\s*({_UNIT})\s+(?:gross|brutto)\b",
    (
        r"(?:(?<![a-zäöü])bruttogewicht|(?<![a-zäöü])gewicht|(?<!net )\bweight)"
        rf"\s*(?:\(kg\))?\s*(?:(?:of|is|von|beträgt)\s+)?(?:[:|]\s*)*({_THOUSANDS})\s*({_UNIT})\b"
    ),
]
_UNIT_VOLUME = rf"(?:volumen|volume)\s*(?:\(cbm\))?\s*(?:[:|]\s*)*({_NUM})\s*(?:cbm|m3|m³)\b"
_COUNT = re.compile(
    r"\b(\d+)\s*(?:pieces|pcs|pallets?|cartons?|boxes|crates|cases|colli|packages|stück"
    r"|paletten|kartons|kisten)\b",
    re.IGNORECASE,
)
_DIMS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*cm\b",
    re.IGNORECASE,
)
_EACH = re.compile(
    r"\b(?:each|per (?:piece|pallet|carton)|je|pro (?:stück|palette))\b", re.IGNORECASE
)
_TO_KG = {"l": Decimal("0.45359237"), "t": Decimal(1000)}


_GROUPED = re.compile(r"\d{1,3}(?:[.,]\d{3})+")
_ONE_GROUP = re.compile(r"\d{1,3}[.,]\d{3}")
_PALLETS = re.compile(r"\b(?:pallets?|paletten?|skids?)\b", re.IGNORECASE)
# THOUSANDS_EVIDENCE plausibility: an air shipment of pallets, several pieces or more than
# a tenth of a cbm per kg does not weigh under 10 kg
MIN_PLAUSIBLE_KG = Decimal(10)
MIN_DENSITY_KG_PER_CBM = Decimal(10)


def _implausibly_light(kg, context, facts):
    if kg >= MIN_PLAUSIBLE_KG:
        return False
    if _PALLETS.search(context):
        return True
    counts = [int(c) for c in _COUNT.findall(context)]
    pieces = facts.get("pieces") or (max(counts) if counts else 0)
    if pieces and int(pieces) >= 2:
        return True
    volume = facts.get("volume_cbm")
    if not volume and (match := re.search(rf"({_NUM})\s*(?:cbm|m3|m³)\b", context, re.IGNORECASE)):
        volume = match[1].replace(",", ".")
    return bool(volume) and kg / Decimal(str(volume)) < MIN_DENSITY_KG_PER_CBM


def weight_reading(raw, factor=Decimal(1), known=(), context="", facts=None):
    """The kg value of a stated weight figure, or None while it stays ambiguous.

    "1,674" / "1.674" can be thousands (1674) or a decimal (1.674); a 1.674 kg air shipment
    would price at the minimum. The thousands reading counts when it equals a figure already
    known for this shipment (`known`: the extraction, an earlier mail in the thread, an
    attachment). THOUSANDS_EVIDENCE adds: with no known figure at all, a one-group figure whose
    decimal reading is under 10 kg while `context`/`facts` describe pallets, several pieces or
    a volume no such weight fills. Otherwise None. With the setting off this is exactly the
    agreement rule UNIT_EVIDENCE always had (its only known figure was the extraction)."""
    raw = raw.replace(" ", "")
    if not _GROUPED.fullmatch(raw):
        return Decimal(raw.replace(",", ".")) * factor
    thousands = Decimal(re.sub(r"[.,]", "", raw)) * factor
    figures = [Decimal(str(k)) for k in known if k is not None]
    if any(abs(thousands - k) <= Decimal("0.1") for k in figures):
        return thousands
    if (
        config.settings.thousands_evidence
        and not figures
        and factor == 1
        and _ONE_GROUP.fullmatch(raw)
        and _implausibly_light(Decimal(raw.replace(",", ".")), context, facts or {})
    ):
        return thousands
    return None


def _unit_facts(text, expect, known=()):
    """UNIT_EVIDENCE: weight in kg, lbs or tonnes with thousands separators, and volume from
    'N pieces, each L x W x H cm'. A number readable two ways is resolved by weight_reading."""
    found = {}
    for pattern in _UNIT_WEIGHT:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        unit = match[2].lower()
        factor = _TO_KG.get(unit[0], Decimal(1))
        figures = [(expect or {}).get("weight_kg")]
        if config.settings.thousands_evidence:
            figures += list(known)
        value = weight_reading(match[1], factor, figures, text, expect)
        if value is not None:
            found["weight_kg"] = value
        break
    if match := re.search(_UNIT_VOLUME, text, re.IGNORECASE):
        found["volume_cbm"] = Decimal(match[1].replace(",", "."))
    dims, counts = _DIMS.findall(text), {int(c) for c in _COUNT.findall(text)}
    if "volume_cbm" not in found and len(dims) == 1 and len(counts) == 1 and _EACH.search(text):
        length, width, height = (Decimal(x.replace(",", ".")) for x in dims[0])
        found["volume_cbm"] = (
            next(iter(counts)) * length * width * height / Decimal(1_000_000)
        ).quantize(Decimal("0.0001"))
    return found


# Between a total's label and its number. Today's gap lets `\s*` cross a line break, so a
# table header ending in "Gross weight (kg)" takes the first cell of the next row (Kessler
# BOM thread: pieces 11 or a dimension against the mail's 1674 kg, a false conflict and a
# needless weight question). TOTALS_SAME_LINE: a line break only after "of"/"is" or a ":"/"|"
# separator, so "Gross weight:\n1674" and wrapped sentences still count.
_GAP_TODAY = r"\s*(?:\({unit}\))?\s*(?:(?:of|is)\s+)?(?:[:|]\s*)*"
_GAP_SAME_LINE = r"(?:[ \t]*\({unit}\))?(?:\s+(?:of|is)\b)?[ \t]*(?:[:|]\s*)*"


def _total_patterns():
    gap = _GAP_SAME_LINE if config.settings.totals_same_line else _GAP_TODAY
    pieces_gap = r"[ \t]*" if config.settings.totals_same_line else r"\s*"
    return {
        "weight_kg": [
            rf"(?:total\s+)?gross weight{gap.format(unit='kg')}({_NUM})",
            rf"({_NUM})\s*kg\s+gross",
        ],
        "volume_cbm": [
            rf"(?:total\s+)?volume{gap.format(unit='cbm')}({_NUM})",
            rf"({_NUM})\s*(?:cbm|m3|m³)\b",
        ],
        "pieces": [rf"(?:total\s+)?pieces{pieces_gap}(?:[:|]\s*)+(\d+)", r"\b(\d+)\s+pieces\b"],
    }


def numeric_facts(text, expect=None, known=()):
    """Totals the text states. `known`: weights already known for this shipment (other mails
    of the thread, attachments) that THOUSANDS_EVIDENCE may use to read "1,674 kg"."""
    result = {}
    patterns = _total_patterns()
    for field, patterns_ in patterns.items():
        for pattern in patterns_:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                suffix = text[match.end() :].lstrip()
                if _ONE_GROUP.fullmatch(match[1]):
                    # "1,674 kg" or "Gross weight (kg): 1,674": thousands or decimal
                    in_kg = re.match(r"(?:kgs?|kilos?|kilograms?)\b", suffix, re.IGNORECASE) or (
                        "(kg)" in match[0].lower()
                    )
                    value = (
                        weight_reading(
                            match[1],
                            known=[(expect or {}).get("weight_kg"), *known],
                            context=text,
                            facts=expect,
                        )
                        if config.settings.thousands_evidence and field == "weight_kg" and in_kg
                        else None
                    )
                    if value is None:
                        continue  # ambiguous thousands/decimal notation needs confirmation
                    result[field] = value
                    break
                amount = Decimal(match[1].replace(",", "."))
                if field == "weight_kg":
                    if re.match(r"(?:t|tonnes?|tons?)\b", suffix, re.IGNORECASE):
                        amount *= Decimal(1000)
                    elif re.match(r"lbs?\b", suffix, re.IGNORECASE):
                        amount *= Decimal("0.45359237")
                elif field == "volume_cbm" and re.match(
                    r"(?:ft|cu\s*ft|litres?|liters?)", suffix, re.IGNORECASE
                ):
                    continue
                result[field] = amount
                break
    if config.settings.unit_evidence:
        for field, value in _unit_facts(text, expect, known).items():
            result.setdefault(field, value)
    return result


def validate(mail, candidate, attachments=None, attachment_issues=None, corrections=None):
    current = current_message(mail.body, mail.subject)
    active = _SIGNATURE.split(current.facts, 1)[0]
    data = candidate.facts.copy() if isinstance(candidate, RfqDraft) else candidate.model_dump()
    issues = list(attachment_issues or [])
    evidence = {}
    corrections = corrections or {}
    if not data.get("origin"):
        recovered = explicit_delivery_origin(mail, data.get("destination"))
        if recovered:
            data["origin"] = recovered
            evidence["origin"] = {
                "source": "explicit_self_delivery",
                "value": recovered,
            }
    sources = {"body": active, **(attachments or {})}
    if current.subject:
        sources["subject"] = current.subject
    # Never retain a pickup city inferred by the older history/signature heuristic.
    data["pickup_city"] = None
    data["pickup_assumed"] = False
    combined = "\n".join(sources.values())
    pickup = bool(_PICKUP.search(active)) and not no_pickup(active)
    if corrections.get("no_pickup"):  # the answer to our ZIP question says they self-deliver
        pickup = False
    zip_match = re.search(r"\b(?:D-)?(\d{5})\b", active)
    current_area = find_area(active) if pickup else None
    attachment_shipper_cities = set()
    for text in (attachments or {}).values():
        match = re.search(r"(?im)^\s*shipper\s*[:|]\s*(.+?)\s*$", text)
        if not match:
            continue
        parts = [part.strip() for part in match[1].lstrip("|").split(",")]
        if len(parts) >= 3:
            attachment_shipper_cities.add(parts[-2])
    # the shipper line in a document is the company's address; a pickup ZIP written in the
    # mail itself names the collection site and wins (rounds 10132 Bremen vs 71184
    # Boeblingen, 10404 Erlangen vs 81862 Munich South asked for a ZIP they had given)
    if (
        current_area
        and not zip_match
        and any(city.casefold() != current_area.casefold() for city in attachment_shipper_cities)
    ):
        issues.append(
            {
                "field": "pickup_zip",
                "code": "conflict",
                "detail": ("Mail pickup location and attachment shipper identify different places"),
            }
        )
    if pickup:
        data["pickup_zip"] = zip_match[1] if zip_match else None
    elif data.get("pickup_zip") and str(data["pickup_zip"]) not in combined:
        data["pickup_zip"] = None
    for field, value in corrections.items():
        if field == "no_pickup":
            continue
        data[field] = value
        evidence[field] = {"source": "customer_correction", "value": value}
    if data.get("pickup_city"):  # an area from the answer: priced, exact address still asked
        data["pickup_assumed"] = True
    if pickup and not data.get("pickup_zip") and not data.get("pickup_city"):
        issues.append(
            {
                "field": "pickup_zip",
                "code": "missing",
                "detail": "Pickup postal code is required",
            }
        )
    if data.get("pickup_zip"):
        airport = origin_for_zip(str(data["pickup_zip"]))
        if not airport:
            issues.append(
                {
                    "field": "pickup_zip",
                    "code": "unsupported",
                    "detail": "Pickup is outside supported zones",
                }
            )
        else:
            origin = data.get("origin")
            explicit = origin and (
                re.search(rf"\b{re.escape(origin)}\b", active, re.IGNORECASE)
                or re.search(_ALIASES.get(origin, r"(?!)"), active, re.IGNORECASE)
            )
            if explicit and origin != airport and origin != data.get("destination"):
                issues.append(
                    {
                        "field": "origin",
                        "code": "conflict",
                        "detail": (
                            f"Named origin {origin} conflicts with pickup-zone airport {airport}"
                        ),
                    }
                )
            elif not explicit or origin == data.get("destination"):
                data["origin"] = airport
            evidence["origin"] = {"source": "pickup_zones", "value": data["origin"]}
        evidence["pickup_zip"] = {
            "source": "body" if "pickup_zip" not in corrections else "customer_correction",
            "value": data["pickup_zip"],
        }
    origin = data.get("origin")
    if origin:
        if origin not in set(pickup_zones().values()):
            issues.append(
                {
                    "field": "origin",
                    "code": "unsupported",
                    "detail": "Departure must be a supported German export airport",
                }
            )
        elif "origin" not in evidence and not any(
            re.search(
                rf"\b{re.escape(origin)}\b|{_ALIASES.get(origin, r'(?!)')}",
                v,
                re.IGNORECASE,
            )
            for v in sources.values()
        ):
            issues.append(
                {
                    "field": "origin",
                    "code": "unproven",
                    "detail": "Departure is not stated in the current request",
                }
            )
        else:
            evidence.setdefault("origin", {"source": "current_request", "value": origin})
    elif not pickup:
        issues.append(
            {
                "field": "origin",
                "code": "missing",
                "detail": "Departure airport is required",
            }
        )
    if origin and origin == data.get("destination"):
        issues.append({"field": "origin", "code": "same_as_destination"})
    destination = data.get("destination")
    if not destination or (
        "destination" not in corrections
        and not re.search(
            rf"\b{re.escape(destination or 'NO_DESTINATION')}\b",
            combined,
            re.IGNORECASE,
        )
    ):
        issues.append(
            {
                "field": "destination",
                "code": "missing_or_unproven",
                "detail": "Confirm the destination airport code",
            }
        )
    else:
        evidence["destination"] = {"source": "current_request", "value": destination}
    totals = parse_lots(active)
    has_lots = bool(
        re.search(
            r"^\s*(?:lot|los|position|pos\.?)\s*\d+",
            active,
            re.IGNORECASE | re.MULTILINE,
        )
    )
    values = numeric_facts(active, expect=data) if not has_lots else {}
    if has_lots and totals is None and not {"weight_kg", "volume_cbm"} <= corrections.keys():
        issues.append(
            {
                "field": "lots",
                "code": "unreadable",
                "detail": "Please provide complete gross weights and dimensions for every lot",
            }
        )
    elif totals:
        values = {"weight_kg": totals.weight_kg, "volume_cbm": totals.volume_cbm}
    for field in ("weight_kg", "volume_cbm", "pieces"):
        observations = [("body", values[field])] if field in values else []
        known = (values.get("weight_kg"), data.get("weight_kg"))  # for THOUSANDS_EVIDENCE
        observations += [
            (name, facts[field])
            for name, text in (attachments or {}).items()
            if field in (facts := numeric_facts(text, known=known))
        ]
        if field in corrections:
            observations = [("customer_correction", Decimal(str(corrections[field])))]
        if observations:
            tolerance = (
                Decimal("0.01")
                if field == "volume_cbm"
                else Decimal("0.1")
                if field == "weight_kg"
                else Decimal(0)
            )
            if any(abs(v - observations[0][1]) > tolerance for _, v in observations):
                issues.append(
                    {
                        "field": field,
                        "code": "conflict",
                        "detail": "Mail and attachment totals disagree",
                    }
                )
            data[field] = (
                int(observations[0][1]) if field == "pieces" else float(observations[0][1])
            )
            evidence[field] = {
                "source": observations[0][0],
                "value": str(observations[0][1]),
                "observations": [(n, str(v)) for n, v in observations],
            }
        elif field != "pieces":
            issues.append({"field": field, "code": "missing", "detail": f"Confirm {field}"})
    # UN markers in any source take precedence over a generic non-dangerous statement.
    dangerous = bool(
        re.search(
            r"\bUN\s*\d{4}\b|(?:dangerous goods|gefahrgut)\s*[:|]\s*(?:yes|ja|class|klasse)",
            combined,
            re.IGNORECASE,
        )
    )
    declared = re.search(
        r"(?:dangerous goods|gefahrgut)\s*[:|]\s*([^\n|]*\bUN\s*\d{4}[^\n|]*)",
        combined,
        re.IGNORECASE,
    )
    # Dangerous goods (round 10132, reason dg_docs_missing): before quoting we must ask for
    # the DG documents (Shipper's Declaration, safety data sheet). Cleared by the customer's
    # documents, or by an explicit statement that the goods are not dangerous.
    if dangerous and corrections.get("dangerous_goods") not in (
        "not_dangerous_document_corrected",
        "dg_documents_received",
    ):
        issues.append(
            {
                "field": "dangerous_goods",
                "code": "documents_required",
                "detail": "Dangerous goods: request the Shipper's Declaration and safety data sheet",
            }
        )
    evidence["dangerous_goods"] = {
        "source": "all_documents",
        "un_numbers": re.findall(r"\bUN\s*\d{4}\b", combined, re.IGNORECASE),
        "declared": declared[1].strip()[:120] if declared else None,
    }
    try:
        rfq = Rfq.model_validate(data)
    except ValueError as exc:
        rfq = None
        issues.append({"field": "shipment", "code": "invalid", "detail": str(exc)[:200]})
    draft = RfqDraft(facts=data, evidence=evidence, issues=issues, version=VERSION)
    return rfq, draft
