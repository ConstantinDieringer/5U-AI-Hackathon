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
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ValidationError

from app.pricing.fx import currency_code

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


def _numbers_in(text: str) -> set[Decimal]:
    """Every amount written in the text, read with the bid parser's separator rules."""
    from app.agents.bids import _number

    found: set[Decimal] = set()
    for token in _NUMBER_TOKEN.findall(text):
        for candidate in (token, token.strip()):
            value = _number(candidate.replace(" ", " ").replace(" ", " "))
            if value is not None:
                found.add(value)
    return found


def check(raw: str, text: str, allowed_currencies: set[str]) -> Reading | None:
    """Validated reading, or None. Pure: no gateway, no db (unit tested)."""
    try:
        data = _Extraction.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None
    if data.total is None or data.currency is None:
        return None
    try:
        amount = Decimal(data.total.replace(" ", "").replace(",", ""))
    except InvalidOperation:
        return None
    currency = data.currency.strip().upper()
    currency = currency_code(currency)
    if amount <= 0 or currency not in allowed_currencies:
        return None
    if amount not in _numbers_in(text):  # the model may not invent or compute a total
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
