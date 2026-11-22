"""Current customer evidence for the selection goal and latest arrival, never a price."""

from __future__ import annotations

import re
from datetime import date, timedelta

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "januar"),
            ("february", "februar"),
            ("march", "märz"),
            ("april",),
            ("may", "mai"),
            ("june", "juni"),
            ("july", "juli"),
            ("august",),
            ("september",),
            ("october", "oktober"),
            ("november",),
            ("december", "dezember"),
        ),
        1,
    )
    for name in names
}
_MONTH = "|".join(_MONTHS)
_DATE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\.\d{{1,2}}\.\d{{4}}|"
    rf"\d{{1,2}}\.?\s+(?:{_MONTH})\s+\d{{4}}|"
    rf"(?:{_MONTH})\s+\d{{1,2}},?\s+\d{{4}})\b",
    re.IGNORECASE,
)
_ARRIVAL = re.compile(
    r"\b(?:arriv\w*|deliver\w*|reach|ankunft|ankommen|eintreffen|liefer\w*)\b", re.IGNORECASE
)
_LIMIT = re.compile(
    r"\b(?:no later than|not later than|by|before|latest|deadline|bis|vor|spätestens)\b",
    re.IGNORECASE,
)
_FAST = re.compile(r"\b(?:fastest|quickest|express|schnellst\w*)\b", re.IGNORECASE)
_CHEAP = re.compile(
    r"\b(?:cheapest|lowest (?:price|cost)|least expensive|günstigst\w*|billigst\w*)\b",
    re.IGNORECASE,
)
_PRICE_PRIORITY = re.compile(
    r"\b(?:price|cost|preis|kosten)\b.{0,55}\b(?:determin\w*|decid\w*|priority|"
    r"entscheid\w*|maßgeb\w*|ausschlag\w*)\b|"
    r"\b(?:choose|select|prefer|wähl\w*|bevorzug\w*)\b.{0,45}\b(?:cheapest|lowest|günstigst\w*)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"\b(?:not|not necessarily|no need for|nicht|nicht unbedingt|kein\w*)\s+(?:the\s+|die\s+|das\s+)?$",
    re.IGNORECASE,
)


def calendar_date(value: str) -> str | None:
    value = value.strip().casefold()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", value):
            day, month, year = map(int, value.split("."))
        else:
            parts = value.replace(".", "").replace(",", "").split()
            if len(parts) != 3:
                return None
            if parts[0] in _MONTHS:
                month, day, year = _MONTHS[parts[0]], int(parts[1]), int(parts[2])
            else:
                day, month, year = int(parts[0]), _MONTHS[parts[1]], int(parts[2])
        return date(year, month, day).isoformat()
    except (ValueError, KeyError):
        return None


def arrival_dates(
    text: str, origin: str | None = None, destination: str | None = None
) -> dict[str, str]:
    found = {}
    for match in _DATE.finditer(text):
        prefix = text[max(0, match.start() - 220) : match.start()]
        # Do not associate a ready date in the next paragraph/sentence with an older limit.
        clause = re.split(r"\n\s*\n|[.!?]\s+(?=[A-ZÄÖÜ])", prefix)[-1]
        if origin:
            delivered_to_origin = re.search(
                rf"\b(?:to|at|an|zum)\s+(?:airport\s+)?{re.escape(origin)}\b", clause, re.IGNORECASE
            )
            delivered_to_destination = re.search(
                rf"\b(?:to|at|an|zum)\s+(?:airport\s+)?{re.escape(destination or 'NO_DESTINATION')}\b",
                clause,
                re.IGNORECASE,
            )
            if delivered_to_origin and (
                not delivered_to_destination
                or delivered_to_origin.start() > delivered_to_destination.start()
            ):
                continue  # customer delivery to the export airport is not arrival at destination
        if re.search(r"\b(?:not (?:be )?before|nicht vor)\s*(?:dem\s*)?$", clause, re.IGNORECASE):
            continue  # earliest acceptable arrival is not a latest-arrival bound
        arrival_after_date = re.match(
            r"\s*(?:ankommen|eintreffen|geliefert)\b", text[match.end() :], re.IGNORECASE
        )
        if (
            (_ARRIVAL.search(clause) or arrival_after_date)
            and _LIMIT.search(clause)
            and (value := calendar_date(match[0]))
        ):
            if re.search(r"\b(?:before|vor)\s*(?:dem\s*)?$", clause, re.IGNORECASE):
                try:
                    value = (date.fromisoformat(value) - timedelta(days=1)).isoformat()
                except OverflowError:
                    continue
            found[value] = (clause + match[0]).strip()
    return found


def _positive(pattern: re.Pattern, text: str) -> bool:
    return any(
        not _NEGATED.search(text[max(0, m.start() - 64) : m.start()])
        for m in pattern.finditer(text)
    )


def checked(
    data: dict, sources: dict[str, str], corrections: dict | None = None
) -> tuple[dict, list[dict], dict]:
    """Recover clear current facts; uncertain constraints remain visible blocking issues."""
    data = data.copy()
    corrections = corrections or {}
    dates = {
        value: quote
        for text in sources.values()
        for value, quote in arrival_dates(text, data.get("origin"), data.get("destination")).items()
    }
    evidence, issues = {}, []
    active = "\n".join(sources.values())
    stated_price_priority = any(
        not re.search(r"\b(?:not|never|nicht|kein\w*)\b", match[0], re.IGNORECASE)
        and not _NEGATED.search(active[max(0, match.start() - 64) : match.start()])
        for match in _PRICE_PRIORITY.finditer(active)
    )
    limit_without_date = any(
        _ARRIVAL.search(clause)
        and _LIMIT.search(clause)
        and re.search(r"\b(?:must|need|required|spätestens|muss|müssen)\b", clause, re.IGNORECASE)
        and not re.search(r"\b(?:no|kein\w*)\s+(?:fixed\s+)?deadline\b", clause, re.IGNORECASE)
        and not re.search(r"\b(?:not (?:be )?before|nicht vor)\b", clause, re.IGNORECASE)
        for clause in re.split(r"\n\s*\n|[.!?]\s+(?=[A-ZÄÖÜ])", active)
    )
    if "arrival_deadline" not in corrections:
        if len(dates) > 1:
            issues.append({"field": "arrival_deadline", "code": "conflict"})
        elif dates:
            value, quote = next(iter(dates.items()))
            data["arrival_deadline"] = value
            evidence["arrival_deadline"] = {"source": "current_customer", "quote": quote}
        elif data.get("arrival_deadline"):
            issues.append({"field": "arrival_deadline", "code": "unproven"})
        elif limit_without_date:
            issues.append({"field": "arrival_deadline", "code": "missing"})
    else:
        evidence["arrival_deadline"] = {"source": "customer_correction"}
    if (data.get("arrival_deadline") or stated_price_priority) and "preference" not in corrections:
        price_priority = stated_price_priority or (
            _positive(_CHEAP, active) and not _positive(_FAST, active)
        )
        if price_priority or not _positive(_FAST, active):
            data["preference"] = "cheapest"
            evidence["preference"] = {
                "source": "current_customer",
                "reason": "price within arrival limit",
            }
        elif _positive(_CHEAP, active):
            issues.append({"field": "preference", "code": "conflict"})
    if data.get("arrival_deadline") and not data.get("ready_date"):
        issues.append({"field": "ready_date", "code": "missing_for_arrival_deadline"})
    elif data.get("arrival_deadline"):
        try:
            date.fromisoformat(data["ready_date"][:10])
        except ValueError:
            issues.append({"field": "ready_date", "code": "invalid_for_arrival_deadline"})
    return data, issues, evidence


def answer(text: str, asked: set[str]) -> dict:
    """A direct date answer to a persisted question has a known field context."""
    dates = {v for m in _DATE.finditer(text) if (v := calendar_date(m[0]))}
    result = {}
    date_fields = asked & {"arrival_deadline", "ready_date"}
    if len(date_fields) == 1 and len(dates) == 1:
        result[next(iter(date_fields))] = next(iter(dates))
    if "preference" in asked:
        cheap, fast = _positive(_CHEAP, text), _positive(_FAST, text)
        if cheap != fast:
            result["preference"] = "cheapest" if cheap else "fastest"
    return result
