"""Shipment totals from "Lot N: ..." lines, in code (the LLM cannot be trusted to add up).

Real formats (mails 1628, 1834, 1967):
    Lot 1: 1 piece, gross weight 618 kg, volume 0.74 cbm
    Lot 2: 2 pieces, 141 kg gross, 2.04 cbm
    Lot 1: 4 drums, together 730 kg gross (642 kg net), together 1.68 cbm
    Lot 2: 4 cartons, each 48 kg gross (41 kg net), each 110 x 110 x 52 cm
Gross weight counts, net is ignored. "each" multiplies by the lot's piece count; dimensions
are per piece unless marked together/total. If any lot line cannot be read, None is returned
and the LLM's totals stay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.mail import measurements

_LOT = re.compile(
    r"^\s*(?:lot|los|position|pos\.?)\s*\d+\s*[:.)-]\s*(?P<rest>.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_COUNT = re.compile(r"^\s*(\d+)\s*(?:x\s*)?[a-zäöü]", re.IGNORECASE)
_NUM = r"(\d+(?:[.,]\d+)?)"
_WEIGHT = re.compile(_NUM + r"\s*(kg|kgs|lbs?|t)\b(?:\s*(gross|brutto|net|netto))?", re.IGNORECASE)
_VOLUME = measurements.VOLUME
_DIMS = re.compile(
    _NUM
    + r"\s*[x×]\s*"
    + _NUM
    + r"\s*[x×]\s*"
    + _NUM
    + r"\s*(mm|cm|m|ft|feet|foot|inches|inch|in(?=[.,;)]|\s*$|\s+each\b)|\"|'')(?=\W|$)",
    re.IGNORECASE,
)
_EACH = re.compile(r"(each|per piece|per unit|je|pro stück)\s*$", re.IGNORECASE)
_TOTAL = re.compile(r"(together|total|in total|insgesamt|zusammen)\s*$", re.IGNORECASE)
_TO_KG = {"kg": Decimal(1), "kgs": Decimal(1), "lb": Decimal("0.45359237"),
          "lbs": Decimal("0.45359237"), "t": Decimal(1000)}  # fmt: skip
_INCH = Decimal("0.0254")
_TO_M = {"mm": Decimal("0.001"), "cm": Decimal("0.01"), "m": Decimal(1), "in": _INCH,
         "inch": _INCH, "inches": _INCH, '"': _INCH, "''": _INCH}  # fmt: skip


@dataclass(frozen=True)
class Totals:
    weight_kg: Decimal
    volume_cbm: Decimal
    lots: int


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", "."))


def _before(text: str, pos: int) -> str:
    return text[max(0, pos - 14) : pos]


def _in_parens(text: str, pos: int) -> bool:
    return text.rfind("(", 0, pos) > text.rfind(")", 0, pos)


def _lot(rest: str) -> tuple[Decimal, Decimal] | None:
    m = _COUNT.match(rest)
    count = Decimal(m.group(1)) if m else Decimal(1)
    weight = None
    for w in _WEIGHT.finditer(rest):
        kind = (w.group(3) or "").lower()
        if kind.startswith("net") or _in_parens(rest, w.start()):
            continue
        weight = _dec(w.group(1)) * _TO_KG[w.group(2).lower()]
        if _EACH.search(_before(rest, w.start())):
            weight *= count
        break
    volume = None
    v = _VOLUME.search(rest)
    if v is not None:
        volume = measurements.quantity(v["number"])
        if volume is None:
            return None
        volume *= measurements.volume_factor(v["unit"])
        if _EACH.search(_before(rest, v.start())):
            volume *= count
    else:
        d = _DIMS.search(rest)
        if d is not None:
            f = measurements.length_factor(d.group(4))
            volume = _dec(d.group(1)) * _dec(d.group(2)) * _dec(d.group(3)) * f**3
            if not _TOTAL.search(_before(rest, d.start())):
                volume *= count  # dimensions describe one piece
    if weight is None or volume is None:
        return None
    return weight, volume


def parse_lots(body: str) -> Totals | None:
    """Summed gross weight and volume over all lot lines, or None (no lots / one unreadable)."""
    lines = [m.group("rest") for m in _LOT.finditer(body or "")]
    if not lines:
        return None
    w_sum, v_sum = Decimal(0), Decimal(0)
    try:
        for rest in lines:
            lot = _lot(rest)
            if lot is None:
                return None
            w_sum += lot[0]
            v_sum += lot[1]
    except InvalidOperation:
        return None
    return Totals(w_sum, v_sum, len(lines))
