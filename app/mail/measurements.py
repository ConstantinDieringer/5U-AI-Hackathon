"""Exact source-unit conversions. Never treat an unknown unit as SI."""

from __future__ import annotations

import re
from decimal import Decimal

LB_TO_KG = Decimal("0.45359237")
INCH_TO_M = Decimal("0.0254")
FOOT_TO_M = Decimal("0.3048")

# Long aliases first. The suffix boundary prevents m matching mm, or ft matching ft3.
_VOLUME_UNITS = (
    (r"cubic\s*(?:feet|foot)|cu\.?\s*ft\.?|cft|ft(?:\^?3|³)", FOOT_TO_M**3),
    (r"cubic\s*inch(?:es)?|cu\.?\s*in\.?|in(?:\^?3|³)", INCH_TO_M**3),
    (r"cubic\s*(?:centimeters?|centimetres?)|cm(?:\^?3|³)|cc", Decimal("0.000001")),
    (r"cubic\s*(?:millimeters?|millimetres?)|mm(?:\^?3|³)", Decimal("0.000000001")),
    (r"cubic\s*(?:meters?|metres?)|cu\.?\s*m\.?|cbm|m(?:\^?3|³)", Decimal(1)),
    (r"lit(?:er|re)s?|litres?|l", Decimal("0.001")),
)
VOLUME_UNIT = "(?:" + "|".join(pattern for pattern, _ in _VOLUME_UNITS) + ")(?![a-zA-Z0-9³])"
NUMBER = r"(?<![\d.,])(?:\d{1,3}(?:[ '’\u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)"
VOLUME = re.compile(rf"(?P<number>{NUMBER})\s*(?P<unit>{VOLUME_UNIT})", re.IGNORECASE)
VOLUME_HEADER = re.compile(
    rf"\b(?:total\s+)?volum(?:e|en)\s*\((?P<unit>{VOLUME_UNIT})\)\s*[:|=]?\s*(?P<number>{NUMBER})",
    re.IGNORECASE,
)
_EACH = re.compile(r"(?:each|per\s+(?:piece|unit)|je|pro\s+stück)\b", re.IGNORECASE)
_COUNT = re.compile(
    r"\b(\d+)\s+(?:pieces?|pcs|crates?|pallets?|cartons?|boxes|cases|stück)\b", re.IGNORECASE
)


def quantity(raw: str) -> Decimal | None:
    """Do not guess whether a lone three-digit group is decimal or thousands."""
    clean = re.sub(r"[ '’\u00a0\u202f]", "", raw)
    if re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", clean):
        return None
    if "," in clean and "." in clean:
        clean = (
            clean.replace(",", "")
            if clean.rfind(".") > clean.rfind(",")
            else clean.replace(".", "").replace(",", ".")
        )
    elif clean.count(",") > 1 or clean.count(".") > 1:
        if not re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", clean):
            return None
        clean = clean.replace(",", "").replace(".", "")
    return Decimal(clean.replace(",", "."))


def volume_factor(unit: str) -> Decimal | None:
    for pattern, factor in _VOLUME_UNITS:
        if re.fullmatch(pattern, unit.strip(), re.IGNORECASE):
            return factor
    return None


def volume_suffix(text: str) -> Decimal | None:
    match = re.match(VOLUME_UNIT, text.lstrip(), re.IGNORECASE)
    return volume_factor(match[0]) if match else None


def explicit_volumes(text: str) -> list[tuple[Decimal, str]]:
    """Literal scalar volumes, with their evidence. Per-piece values belong to lot parsing."""
    values = []
    for pattern in (VOLUME_HEADER, VOLUME):
        for match in pattern.finditer(text):
            amount = quantity(match["number"])
            if amount is None:
                continue
            per_piece = _EACH.search(
                text[max(0, match.start() - 18) : match.start()]
            ) or _EACH.match(text[match.end() :].lstrip())
            if per_piece:
                counts = {int(m[1]) for m in _COUNT.finditer(text)}
                if len(counts) != 1:
                    continue
                amount *= next(iter(counts))
            factor = volume_factor(match["unit"])
            if factor is not None:
                values.append((amount * factor, match[0]))
    return values


def volume_issues(text: str) -> list[str]:
    if any(
        quantity(m["number"]) is None for p in (VOLUME_HEADER, VOLUME) for m in p.finditer(text)
    ):
        return ["Ambiguous decimal or thousands separator in stated volume"]
    values = [value for value, _ in explicit_volumes(text)]
    if values and any(abs(v - values[0]) > Decimal("0.01") for v in values[1:]):
        return ["Conflicting stated volumes after unit conversion"]
    return []


def length_factor(unit: str) -> Decimal:
    unit = unit.strip().lower()
    if unit in ("mm", "millimeter", "millimeters", "millimetre", "millimetres"):
        return Decimal("0.001")
    if unit in ("cm", "centimeter", "centimeters", "centimetre", "centimetres"):
        return Decimal("0.01")
    if unit in ("m", "meter", "meters", "metre", "metres"):
        return Decimal(1)
    if unit in ("in", "inch", "inches", '"', "''"):
        return INCH_TO_M
    if unit in ("ft", "foot", "feet"):
        return FOOT_TO_M
    raise ValueError("unsupported dimension unit")


def mass_suffix(text: str) -> Decimal | None:
    factors = (
        (r"(?:kg|kgs|kilos?|kilograms?)", Decimal(1)),
        (r"(?:lb|lbs|pounds?)", LB_TO_KG),
        (r"(?:oz|ounces?)", Decimal("0.028349523125")),
        (r"(?:mg|milligrams?)", Decimal("0.000001")),
        (r"(?:g|grams?)", Decimal("0.001")),
        (r"(?:t|tonnes?|tons?)", Decimal(1000)),
    )
    for pattern, factor in factors:
        if re.match(pattern + r"\b", text.lstrip(), re.IGNORECASE):
            return factor
    return None


def needs_conversion(text: str) -> bool:
    if any(
        volume_factor(m["unit"]) != 1 for p in (VOLUME_HEADER, VOLUME) for m in p.finditer(text)
    ):
        return True
    return bool(
        re.search(
            r"\b(?:cubic\s+(?:feet|foot|inches?|yards?)|cu\.?\s*ft|cft|ft[³3]|"
            r"lit(?:er|re)s?|lbs?|pounds?|ounces?|grams?|milligrams?|tonnes?|inches|feet)\b|"
            r"\d\s*(?:mm|ft|oz|mg|g|t)\b",
            text,
            re.IGNORECASE,
        )
    )
