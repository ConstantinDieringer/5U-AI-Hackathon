"""Cargo insurance the customer asks to have inside the all-in price.

Round 91 (rejected `too_low`): "Please include cargo insurance in the all-in price, on the
terms of our frame agreement: goods value 22000 EUR, insured at 110 % of that value, premium
rate 0.40 %." The premium is value x coverage x rate, in code, never by the LLM.

Read only when the mail asks for insurance and states both the goods value in EUR and the
premium rate. Coverage defaults to 100 % when the mail gives none. Anything else returns None
and the caller flags it instead of guessing. Rollback: CARGO_INSURANCE=false.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_ASKED = re.compile(
    r"\binsur(?:ance|ed|e)\b|\bversicher\w*|\bassurance\b|\bseguro\b", re.IGNORECASE
)
_NUM = r"(\d[\d.,'   ]*\d|\d)"
_VALUE = re.compile(
    r"\b(?:goods|cargo|commercial|invoice|declared|insured)?\s*(?:value|warenwert|wert)\b"
    rf"[^\d\n%]{{0,25}}{_NUM}\s*(?:EUR|€|euro)\b"
    rf"|\b(?:EUR|€)\s*{_NUM}\s*(?:goods|cargo|invoice)?\s*(?:value|warenwert)\b",
    re.IGNORECASE,
)
_PCT = r"(\d{1,3}(?:[.,]\d+)?)\s*%"
_COVERAGE = re.compile(
    rf"\b(?:insured|covered|versichert|coverage|deckung)\b[^\d\n]{{0,25}}{_PCT}"
    rf"|{_PCT}\s*(?:of\s+(?:that|the|its|this)\s+(?:goods\s+|cargo\s+|invoice\s+)?value|des\s+warenwerts)",
    re.IGNORECASE,
)
_RATE = re.compile(
    rf"\b(?:premium(?:\s+rate)?|rate|prämie\w*|prämiensatz)\b[^\d\n%]{{0,25}}{_PCT}", re.IGNORECASE
)


@dataclass(frozen=True)
class Insurance:
    value_minor: int  # goods value in EUR cents
    coverage_pct: Decimal
    rate_pct: Decimal
    premium_minor: int  # EUR cents, added to the all-in total


def asked(text: str) -> bool:
    return bool(_ASKED.search(text or ""))


def _amount(raw: str) -> Decimal | None:
    """22000, 22,000, 22.000, 22 000, 22'000.50 and 22.000,50 as a Decimal."""
    s = re.sub(r"[ '  ]", "", raw)
    last = max(s.rfind("."), s.rfind(","))
    if last >= 0 and len(s) - last - 1 in (1, 2):  # a decimal part of one or two digits
        s = re.sub(r"[.,]", "", s[:last]) + "." + s[last + 1 :]
    else:
        s = re.sub(r"[.,]", "", s)
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _pct(match: re.Match[str] | None) -> Decimal | None:
    if match is None:
        return None
    raw = next(g for g in match.groups() if g)
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def read(text: str) -> Insurance | None:
    """The insurance to add, or None when the mail does not ask for it or leaves the value or
    the rate open (or states either one twice with different numbers)."""
    text = text or ""
    if not asked(text):
        return None
    values = {v for m in _VALUE.finditer(text) if (v := _amount(next(g for g in m.groups() if g)))}
    rates = {r for m in _RATE.finditer(text) if (r := _pct(m)) is not None}
    coverages = {c for m in _COVERAGE.finditer(text) if (c := _pct(m)) is not None}
    if len(values) != 1 or len(rates) != 1 or len(coverages) > 1:
        return None
    value, rate = next(iter(values)), next(iter(rates))
    coverage = next(iter(coverages)) if coverages else Decimal(100)
    if value <= 0 or not Decimal(0) < rate <= 10 or not Decimal(50) <= coverage <= 200:
        return None
    premium = (value * coverage / 100 * rate / 100 * 100).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return Insurance(int(value * 100), coverage, rate, int(premium))
