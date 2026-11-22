"""Last-chance reading of a carrier bid the rules could not price (rounds 63-65: offers as
PDF/DOCX/tables with empty cells went unanswered three rounds in a row).

One cheap gateway call extracts the offered all-in total, its currency and the transit time.
The model only extracts; code checks the result: the amount must appear literally in the
carrier's text, the currency must be one we can convert, and an answer of "several totals"
or "no total" stays unreadable. Pricing and the choice between options stay in code.
Rollback: BID_LLM_FALLBACK=false.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from app.pricing.amounts import AMOUNT, SPACES, parse_decimal
from app.pricing.fx import CURRENCY, currency_code

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 6000  # a bid is short; a huge attachment is cut, never sent whole
TIMEOUT_S = 15.0
PROMPT = (
    "You read one carrier's reply to our air freight bid request. Extract the single all-in "
    "total price the carrier offers for the whole shipment, its ISO currency code and the "
    "transit time in days if stated. Not a per kg rate, not a minimum, not an optional or "
    "waiting charge, not a subtotal. If there is no total, or several different totals, "
    "set total to null. Answer as JSON only."
)


class _Extraction(BaseModel):
    total: str | None
    currency: str | None
    transit_days: float | None


SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "bid_total",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "total": {"type": ["string", "null"]},
                "currency": {"type": ["string", "null"]},
                "transit_days": {"type": ["number", "null"]},
            },
            "required": ["total", "currency", "transit_days"],
        },
    },
}

_NUMBER_TOKEN = re.compile(r"\d[\d.,'   ]*\d|\d")


@dataclass(frozen=True)
class Reading:
    amount: Decimal
    currency: str
    transit_days: float | None


# A bare number counts only right after a total label in its own cell or clause.
_LABEL_BEFORE = re.compile(
    r"(?:all[- ]?in|total|gesamt\w*|summe|betrag|amount|price|preis|rate)\W*$", re.IGNORECASE
)
_UNIT_AFTER = re.compile(
    r"\s*(?:/|per\b|pro\b|je\b|kg\b|cbm\b|m3\b|%|pcs\b|pieces?\b)", re.IGNORECASE
)


def _written_amounts(text: str) -> set[tuple[Decimal, str]]:
    """Every (amount, currency) the text states as money: a number with its currency next
    to it, or a bare number right after a total label when the text names exactly one
    currency ("Gesamt | 412,80"). Weights, per-unit rates, percentages and reference
    numbers are not money."""
    text = text.translate(SPACES)
    found: set[tuple[Decimal, str]] = set()
    spans: list[tuple[int, int]] = []  # numbers written with a currency are judged only here
    for m in AMOUNT.finditer(text):
        spans.append(m.span())
        if _UNIT_AFTER.match(text, m.end()):
            continue
        value = parse_decimal(m.group("num1") or m.group("num2") or "")
        if value is not None:
            found.add((value, currency_code(m.group("cur1") or m.group("cur2") or "")))
    named = {currency_code(c) for c in re.findall(CURRENCY, text, re.IGNORECASE)}
    if len(named) == 1:
        (only,) = named
        for token in _NUMBER_TOKEN.finditer(text):
            inside = any(a <= token.start() < b for a, b in spans)
            before = re.split(r"[\n,;]", text[: token.start()])[-1][-40:]
            if inside or _UNIT_AFTER.match(text, token.end()) or not _LABEL_BEFORE.search(before):
                continue
            value = parse_decimal(token.group(0).strip())
            if value is not None:
                found.add((value, only))
    return found


def check(raw: str, text: str, allowed_currencies: set[str]) -> Reading | None:
    """Validated reading, or None. Pure: no gateway, no db (unit tested)."""
    try:
        data = _Extraction.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None
    if data.total is None or data.currency is None:
        return None
    amount = parse_decimal(data.total)
    if amount is None:
        return None
    currency = data.currency.strip().upper()
    currency = currency_code(currency)
    if amount <= 0 or currency not in allowed_currencies:
        return None
    if (amount, currency) not in _written_amounts(text):  # never invented or computed
        return None
    days = data.transit_days
    if days is not None and not 0 < days <= 30:
        days = None
    return Reading(amount, currency, days)


def read(text: str, session_id: int, allowed_currencies: set[str]) -> Reading | None:
    """One gateway call (session budget applies). None on any doubt or failure."""
    from app import llm

    text = text.strip()[:MAX_TEXT_CHARS]
    if not text:
        return None
    try:
        raw = llm.complete(
            "bid_extraction",
            [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
            session_id,
            max_tokens=300,
            response_format=SCHEMA,
            timeout_s=TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - budget, timeout or gateway error: stays unreadable
        log.warning("session %s: bid extraction call failed: %s", session_id, exc)
        return None
    return check(raw, text, allowed_currencies)
