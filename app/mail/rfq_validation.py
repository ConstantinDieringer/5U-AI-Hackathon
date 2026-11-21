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
    """Recover a single supported self-delivery airport from current words, never history."""
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
    return (
        next(iter(airports))
        if len(airports) == 1 and destination not in airports
        else None
    )


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
    return any(
        re.match(r"(?:no|without|keine)\b", m[0], re.IGNORECASE) or not _negated(active, m)
        for m in _NO_PICKUP.finditer(active)
    )


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
        origin
        for origin, to in _ROUTE.findall(active)
        if origin in supported and to == destination
    }
    return found


def numeric_facts(text):
    result = {}
    patterns = {
        "weight_kg": [
            rf"(?:total\s+)?gross weight\s*(?:\(kg\))?\s*(?:(?:of|is)\s+)?(?:[:|]\s*)*({_NUM})",
            rf"({_NUM})\s*kg\s+gross",
        ],
        "volume_cbm": [
            rf"(?:total\s+)?volume\s*(?:\(cbm\))?\s*(?:(?:of|is)\s+)?(?:[:|]\s*)*({_NUM})",
            rf"({_NUM})\s*(?:cbm|m3|m³)\b",
        ],
        "pieces": [r"(?:total\s+)?pieces\s*(?:[:|]\s*)+(\d+)", r"\b(\d+)\s+pieces\b"],
    }
    for field, patterns_ in patterns.items():
        for pattern in patterns_:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                if re.fullmatch(r"\d{1,3}[.,]\d{3}", match[1]):
                    continue  # ambiguous thousands/decimal notation needs confirmation
                amount = Decimal(match[1].replace(",", "."))
                suffix = text[match.end() :].lstrip()
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
    return result


def validate(
    mail, candidate, attachments=None, attachment_issues=None, corrections=None
):
    current = current_message(mail.body, mail.subject)
    active = _SIGNATURE.split(current.facts, 1)[0]
    data = (
        candidate.facts.copy()
        if isinstance(candidate, RfqDraft)
        else candidate.model_dump()
    )
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
    if current_area and any(
        city.casefold() != current_area.casefold() for city in attachment_shipper_cities
    ):
        issues.append(
            {
                "field": "pickup_zip",
                "code": "conflict",
                "detail": (
                    "Mail pickup location and attachment shipper identify different places"
                ),
            }
        )
    if pickup:
        data["pickup_zip"] = zip_match[1] if zip_match else None
    elif data.get("pickup_zip") and str(data["pickup_zip"]) not in combined:
        data["pickup_zip"] = None
    for field, value in corrections.items():
        data[field] = value
        evidence[field] = {"source": "customer_correction", "value": value}
    if pickup and not data.get("pickup_zip"):
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
            "source": "body"
            if "pickup_zip" not in corrections
            else "customer_correction",
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
            evidence.setdefault(
                "origin", {"source": "current_request", "value": origin}
            )
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
    values = numeric_facts(active) if not has_lots else {}
    if (
        has_lots
        and totals is None
        and not {"weight_kg", "volume_cbm"} <= corrections.keys()
    ):
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
        observations += [
            (name, facts[field])
            for name, text in (attachments or {}).items()
            if field in (facts := numeric_facts(text))
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
                int(observations[0][1])
                if field == "pieces"
                else float(observations[0][1])
            )
            evidence[field] = {
                "source": observations[0][0],
                "value": str(observations[0][1]),
                "observations": [(n, str(v)) for n, v in observations],
            }
        elif field != "pieces":
            issues.append(
                {"field": field, "code": "missing", "detail": f"Confirm {field}"}
            )
    # UN markers in any source take precedence over a generic non-dangerous statement.
    dangerous = bool(
        re.search(
            r"\bUN\s*\d{4}\b|(?:dangerous goods|gefahrgut)\s*[:|]\s*(?:yes|ja|class|klasse)",
            combined,
            re.IGNORECASE,
        )
    )
    if (
        dangerous
        and corrections.get("dangerous_goods") != "not_dangerous_document_corrected"
    ):
        issues.append(
            {
                "field": "dangerous_goods",
                "code": "conflict_or_unsupported",
                "detail": "Confirm UN/class and whether the non-dangerous declaration or attachment is incorrect",
            }
        )
    evidence["dangerous_goods"] = {
        "source": "all_documents",
        "un_numbers": re.findall(r"\bUN\s*\d{4}\b", combined, re.IGNORECASE),
    }
    try:
        rfq = Rfq.model_validate(data)
    except ValueError as exc:
        rfq = None
        issues.append(
            {"field": "shipment", "code": "invalid", "detail": str(exc)[:200]}
        )
    draft = RfqDraft(facts=data, evidence=evidence, issues=issues, version=VERSION)
    return rfq, draft
