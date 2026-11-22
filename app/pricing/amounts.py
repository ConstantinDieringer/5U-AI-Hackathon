"""Reading money amounts out of carrier and airline text. Deterministic, no LLM.

`parse_decimal` reads one written number with either separator convention. `mail_total`
finds the one all-in total a mail states; per-unit rates, optional or waiting charges,
insurance or goods values and several different totals never become a price.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.pricing.fx import CURRENCY, currency_code
from app.pricing.pricing import money

MONEY = r"(?:\d{1,3}(?:[ ,.'’]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
AMOUNT = re.compile(
    rf"(?:(?P<cur1>{CURRENCY})\s*(?P<num1>{MONEY}))|(?:(?P<num2>{MONEY})\s*(?P<cur2>{CURRENCY}))",
    re.IGNORECASE,
)
SPACES = str.maketrans({" ": " ", " ": " ", " ": " ", " ": " "})


def parse_decimal(raw: str) -> Decimal | None:
    """1,234.50 / 1.234,50 / 1'234.50 / 1 234 -> Decimal. A single group of exactly three
    digits after one separator is thousands (1.250 -> 1250), unless the integer part is 0
    (0.125 stays 0.125, a per-kg rate)."""
    s = raw.translate(SPACES).replace(" ", "").replace("'", "").replace("’", "")
    sign = "-" if s.startswith("-") else ""
    s = s.lstrip("+-")
    if "," in s and "." in s:  # the later separator is the decimal one
        s = (
            s.replace(",", "")
            if s.rfind(".") > s.rfind(",")
            else s.replace(".", "").replace(",", ".")
        )
    elif "," in s:
        head, _, tail = s.rpartition(",")
        thousands = len(tail) == 3 and head not in ("", "0")
        s = s.replace(",", "") if thousands or s.count(",") > 1 else f"{head}.{tail}"
    elif "." in s:
        head, _, tail = s.rpartition(".")
        if s.count(".") > 1 or (len(tail) == 3 and head not in ("", "0")):
            s = s.replace(".", "")  # 1.234 / 1.234.567 = thousands
    try:
        value = Decimal(sign + s)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


# Words that make a clause's total the deciding one, or at least a price statement.
_TOTAL = re.compile(
    r"all[- ]?in|(?<!sub)(?<!sub-)(?<!sub )\btotal\b|\bgesamt|insgesamt|\bgrand\b|"
    r"endpreis|endbetrag|\bsumme\b",
    re.IGNORECASE,
)
_PRICE = re.compile(
    r"price|preis|\brate\b|\boffer\b|angebot|\bquot(?:e|ation)\b|\bkosten\b|\bcost\b|"
    r"\bcharge\b|\bfor\b|\bfür\b",
    re.IGNORECASE,
)
# A clause whose amount is not the transport price.
_NOT_PRICE = re.compile(
    r"sub[- ]?total|zwischensumme|optional|\bextra\b|additional|zusätzlich|zuzüglich|\bzzgl|"
    r"not\s+(?:part|included|incl)|nicht\s+(?:im|enthalten|inkl)|exclud|"
    r"if\s+(?:required|applicable|needed)|falls|on\s+request|auf\s+anfrage|"
    r"wait(?:ing)?|warte|storage|lager|demurrage|detention|cancell?ation|storno|"
    r"discount|rabatt|deposit|\bvalue\b|warenwert|\bwert\b|insured|versichert|"
    r"deductible|selbstbehalt|liability|haftung|minimum|\bmin\b|mindest",
    re.IGNORECASE,
)
# "no extra charges" / "keine Zusatzkosten" deny an extra; they do not name one.
_DENIED_EXTRA = re.compile(
    r"\b(?:no|without|keine?n?|ohne)\s+(?:\w+\s+)?"
    r"(?:extra|additional|zusätzlich\w*|zusatz\w*|hidden|surcharges?|minimum|mindest\w*)\w*",
    re.IGNORECASE,
)
_PER_UNIT = re.compile(
    r"\bper\b|\bpro\b|\bje\b|/\s*(?:kg|h|hr|hour|std|stunde|day|tag|piece|pc|cbm|m3)\b|"
    r"\beach\b|\bdaily\b|täglich",
    re.IGNORECASE,
)
# A whole shipment is the unit of an all-in price, not a per-unit rate.
_PER_SHIPMENT = re.compile(
    r"\b(?:per|pro|je)\s+(?:shipment|consignment|load|sendung|auftrag|transport|tour)\b|"
    r"/\s*(?:shipment|sendung)\b",
    re.IGNORECASE,
)
# Clause ends: line ends, sentence ends, semicolons, and ", " (not "1,240").
_CLAUSE = re.compile(r"\n|(?<=[.!?;])\s+|,\s+")


def _amounts(clause: str) -> list[tuple[int, Decimal, str]]:
    found = []
    for m in AMOUNT.finditer(clause):
        num = parse_decimal(m.group("num1") or m.group("num2") or "")
        if num is None or num <= 0:
            continue
        cur = currency_code((m.group("cur1") or m.group("cur2") or "").upper())
        found.append((m.start(), money(num), cur))
    return found


def mail_total(text: str) -> tuple[Decimal, str] | None:
    """The one explicit total of a mail, or None.

    Tier 1: clauses naming a total or all-in price. Tier 2, only without any: clauses naming
    a price, rate or offer. Tier 3, only without either: a clause that just states an amount.
    Clauses about per-unit rates, optional, waiting or storage charges, minimums, goods or
    insured values never count. Several different amounts in the deciding tier: None, never
    the largest. In one clause "800 EUR + 200 EUR = 1,000 EUR" the amount after "=" counts."""
    tiers: tuple[set, set, set] = (set(), set(), set())
    for clause in _CLAUSE.split(text.translate(SPACES)):
        amounts = _amounts(clause)
        if not amounts:
            continue
        cleaned = _DENIED_EXTRA.sub(" ", _PER_SHIPMENT.sub(" ", clause))
        if _NOT_PRICE.search(cleaned) or _PER_UNIT.search(cleaned):
            continue
        equals = clause.rfind("=")
        if len(amounts) > 1 and equals >= 0:
            amounts = [a for a in amounts if a[0] > equals] or amounts
        tier = 0 if _TOTAL.search(clause) else 1 if _PRICE.search(clause) else 2
        tiers[tier].update((amount, cur) for _, amount, cur in amounts)
    deciding = tiers[0] or tiers[1] or tiers[2]
    return next(iter(deciding)) if len(deciding) == 1 else None
