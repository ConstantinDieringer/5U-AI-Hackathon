"""Current-shipment insurance evidence and deterministic, currency-aware premiums.

No default coverage or rate: an unpriced requested service blocks the all-in quote.
The ordinary RFQ extraction still uses the gateway; this adds no model call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.models import CargoInsurance
from app.pricing.fx import eur_rate
from app.pricing.pricing import money, to_minor

_TERM = re.compile(
    r"\binsur(?:ance|ed|e)\b|\bversicher\w*|\b(?:transport|waren|fracht)?versicherung\b|"
    r"\bassurance\b|\bassicurazione\b|\bseguro\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"\bno\s+(?:cargo\s+|transport\s+)?insurance\s+(?:needed|required)\b|"
    r"\b(?:do not|don't)\s+(?:need|want|include)\s+(?:cargo\s+)?insurance\b|"
    r"\b(?:without|ohne)\s+(?:cargo\s+)?(?:insurance|versicherung)\b|"
    r"\bkeine\s+(?:transport)?versicherung\b",
    re.IGNORECASE,
)
_NUM = r"[+-]?(?:\d+(?:[.,\u00a0\u202f ]\d+)*|[.,]\d+)"
_CUR = r"EUR|USD|GBP|CHF|€|\$"
_MONEY = rf"(?:(?P<before>{_CUR})\s*(?P<n1>{_NUM})|(?P<n2>{_NUM})\s*(?P<after>{_CUR}))"
_VALUE = re.compile(
    rf"\b(?:goods\s*(?:value|are\s+worth)|value\s+of\s+(?:the\s+)?goods|"
    rf"(?:cargo|commercial|invoice|merchandise|declared|insured)\s+value|warenwert)"
    rf"\s*(?:is|of|ist|beträgt|:|=)?\s*{_MONEY}",
    re.IGNORECASE,
)
_COVER = re.compile(
    rf"\b(?:insured\s*(?:at|for)|covered\s*(?:at|for)?|cover(?:age)?\s*(?:is|at|of)?|"
    rf"versichert\s*(?:zu|mit|auf)|deckung)\s*[:=]?\s*({_NUM})\s*(?:%|percent|prozent)",
    re.IGNORECASE,
)
_RATE = re.compile(
    rf"\b(?:premium\s*(?:rate)?|prämien?satz|prämie)\s*(?:is|of|at|ist|beträgt|:|=)?\s*"
    rf"({_NUM})\s*(?:%|percent|prozent)",
    re.IGNORECASE,
)
_MIN_TERM = re.compile(r"minimum\s+premium|min(?:imum)?\.?\s+premium|mindestprämie", re.IGNORECASE)
_MIN = re.compile(rf"(?:{_MIN_TERM.pattern})\s*(?:is|of|:|=)?\s*{_MONEY}", re.IGNORECASE)


def _number(value: str, *, monetary: bool = True) -> str | None:
    raw = re.sub(r"[ \u00a0\u202f]", "", value)
    if "," in raw and "." in raw:
        raw = (
            raw.replace(",", "")
            if raw.rfind(".") > raw.rfind(",")
            else raw.replace(".", "").replace(",", ".")
        )
    elif monetary and re.fullmatch(r"[+-]?\d{1,3}(?:[.,]\d{3})+", raw):
        raw = raw.replace(",", "").replace(".", "")
    else:
        raw = raw.replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    return format(number, "f") if number.is_finite() and number >= 0 else None


def declined(text: str) -> bool:
    for match in _NO.finditer(text):
        before = text[max(0, match.start() - 45) : match.start()]
        if re.search(r"(?:not|never|nicht|don't)\b[^.!?\n]{0,30}$", before, re.IGNORECASE):
            continue
        return True
    return False


def _money(match: re.Match[str]) -> tuple[str | None, str]:
    return _number(match["n1"] or match["n2"]), {"€": "EUR", "$": "USD"}.get(
        match["before"] or match["after"],
        (match["before"] or match["after"]).upper(),
    )


def missing(policy: CargoInsurance) -> list[str]:
    fields = [
        name
        for name in ("goods_value", "currency", "coverage_pct", "premium_pct")
        if getattr(policy, name) is None
    ]
    for name in ("goods_value", "coverage_pct"):
        value = getattr(policy, name)
        if value is not None and Decimal(value) <= 0:
            fields.append(name)
    if policy.minimum_required and policy.minimum_premium is None:
        fields.append("minimum_premium")
    if policy.currency is not None and policy.currency not in ("EUR", "USD"):
        fields.append("supported_currency")
    return fields


def read(
    texts: Iterable[str],
    previous: CargoInsurance | None = None,
    *,
    requested: bool = False,
) -> tuple[CargoInsurance | None, list[str]]:
    """Read explicit labelled terms; conflicting mail/document values remain unresolved.

    `previous` is the same shipment's persisted policy, never another customer's history.
    Clarification reads the new answer separately, then revalidates the merged policy.
    """
    parts = list(texts)
    combined = "\n".join(parts)
    if declined(parts[0] if parts else ""):
        return None, []
    if previous is None and not requested and not _TERM.search(combined):
        return None, []
    values: dict[str, set[str]] = {
        name: set()
        for name in (
            "goods_value",
            "currency",
            "coverage_pct",
            "premium_pct",
            "minimum_premium",
        )
    }
    invalid: set[str] = set()
    for text in parts:
        for pattern, field in ((_VALUE, "goods_value"), (_MIN, "minimum_premium")):
            for match in pattern.finditer(text):
                number, currency = _money(match)
                if number is not None:
                    values[field].add(number)
                else:
                    invalid.add(field)
                values["currency"].add(currency)
        for pattern, field in ((_COVER, "coverage_pct"), (_RATE, "premium_pct")):
            for match in pattern.finditer(text):
                if (number := _number(match[1], monetary=False)) is not None:
                    values[field].add(number)
                else:
                    invalid.add(field)
    data = previous.model_dump() if previous else {}
    conflicts = []
    for field, observations in values.items():
        if field in invalid:
            data[field] = None
            conflicts.append(field)
            continue
        # Equivalent spellings such as 0.30 and 0.3 are the same evidence.
        canonical = {
            str(Decimal(x).normalize()) if field != "currency" else x for x in observations
        }
        if len(canonical) > 1:
            data[field] = None
            conflicts.append(field)
        elif observations:
            data[field] = next(iter(observations))
    data["minimum_required"] = bool(_MIN_TERM.search(combined)) or bool(
        data.get("minimum_required")
    )
    policy = CargoInsurance.model_validate(data)
    return policy, [f"conflicting {field}" for field in conflicts] + missing(policy)


@dataclass(frozen=True)
class Premium:
    eur_minor: int
    original_amount: str
    currency: str
    fx_rate: str
    fx_source: str


def premium(policy: CargoInsurance) -> Premium:
    absent = missing(policy)
    if absent:
        raise ValueError("Insurance terms unresolved: " + ", ".join(absent))
    value = Decimal(policy.goods_value or "0") * Decimal(policy.coverage_pct or "0") / 100
    charge = value * Decimal(policy.premium_pct or "0") / 100
    charge = money(max(charge, Decimal(policy.minimum_premium or "0")))
    fx, source, _ = eur_rate(policy.currency or "")
    return Premium(to_minor(charge * fx), str(charge), policy.currency or "", str(fx), source)


def requested(text: str) -> bool:
    return bool(_TERM.search(text)) and not declined(text)


_NOTICE = {
    "en": "Cargo insurance on your agreed terms is included in this total.",
    "de": "Die Transportversicherung zu deinen vereinbarten Konditionen ist im Gesamtpreis enthalten.",
    "fr": "L'assurance transport selon vos conditions convenues est incluse dans ce total.",
    "es": "El seguro de carga según las condiciones acordadas está incluido en este total.",
    "it": "L'assicurazione merci alle condizioni concordate è inclusa nel totale.",
}


def notice(policy: CargoInsurance | None, language_code: str) -> str:
    if policy is None:
        return ""
    if missing(policy):
        raise ValueError("cannot describe unpriced cargo insurance as included")
    return _NOTICE.get(language_code.split("-")[0].lower(), _NOTICE["en"])


def assert_copy(text: str, policy: CargoInsurance | None) -> None:
    if policy is None:
        return
    if missing(policy):
        raise ValueError("insurance agreement incomplete")
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if _TERM.search(sentence) and re.search(
            r"\bnot\b|\bwithout\b|unavailable|extra|on top|"
            r"\bnicht\b|\bkeine?\b|noch nicht|non inclus|no incluido|"
            r"not able|cannot|can't|can't yet|unable|non disponibile|no disponible|pas inclus",
            sentence,
            re.IGNORECASE,
        ):
            raise ValueError("priced insurance must not be described as unavailable or extra")


def with_notice(body: str, policy: CargoInsurance | None, language_code: str) -> str:
    from app.mail.style import SIGNATURE

    text = notice(policy, language_code)
    assert_copy(body, policy)
    if not text or text in body:
        return body
    index = body.rfind(SIGNATURE)
    if index < 0:
        raise ValueError("missing signature on insured quotation")
    return f"{body[:index].rstrip()}\n\n{text}\n\n{body[index:]}"
