"""Explicit conversion of any configured currency to EUR. Never infer a rate from unrelated
portal calls.

Rates come from config only: `FX_EUR_RATES` ("USD=0.92,JPY=0.0061,GBP=1.17", EUR per unit)
valid until `FX_VALID_UNTIL`. The older `USD_EUR_RATE` / `USD_EUR_VALID_UNTIL` pair still
works for USD. A currency without a valid configured rate is recognised but not converted.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.config import settings

# ISO 4217 codes we recognise in carrier mails, tariffs and portal answers. Recognising a
# code does not make it convertible: that needs a configured rate.
ISO_CODES = [
    "EUR",
    "USD",
    "GBP",
    "CHF",
    "JPY",
    "CNY",
    "HKD",
    "SGD",
    "AUD",
    "CAD",
    "NZD",
    "SEK",
    "NOK",
    "DKK",
    "PLN",
    "CZK",
    "HUF",
    "RON",
    "TRY",
    "AED",
    "SAR",
    "QAR",
    "INR",
    "KRW",
    "THB",
    "MYR",
    "IDR",
    "PHP",
    "VND",
    "TWD",
    "ZAR",
    "BRL",
    "MXN",
    "ILS",
    "EGP",
]

# Symbols and words that name exactly one currency. Ambiguous ones stay out on purpose:
# "kr" (SEK/NOK/DKK), "pound(s)" (also a weight), "Fr." (also Friday/Frau). A bare "$" and "¥" keep their common
# freight meaning, USD and JPY, as before.
_ALIASES: dict[str, str] = {
    "€": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "US$": "USD",
    "$": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "£": "GBP",
    "JP¥": "JPY",
    "¥": "JPY",
    "円": "JPY",
    "YEN": "JPY",
    "CN¥": "CNY",
    "RMB": "CNY",
    "元": "CNY",
    "YUAN": "CNY",
    "SFR": "CHF",
    "HK$": "HKD",
    "S$": "SGD",
    "A$": "AUD",
    "C$": "CAD",
    "NZ$": "NZD",
    "ZŁ": "PLN",
    "₺": "TRY",
    "₹": "INR",
    "₩": "KRW",
    "R$": "BRL",
    "₪": "ILS",
}


def _token_pattern() -> str:
    # Longest first, so "US$" wins over "$" and "CN¥" over "¥". Letter tokens may not touch
    # other letters ("EUROPE", "USDA"), symbols may stand anywhere.
    tokens = sorted(set(ISO_CODES) | set(_ALIASES), key=len, reverse=True)
    parts = []
    for t in tokens:
        esc = re.escape(t)
        parts.append(rf"(?<![A-Za-z]){esc}(?![A-Za-z])" if t[-1].isalpha() else esc)
    return "(?:" + "|".join(parts) + ")"


# A currency token, for embedding in other regexes (use re.IGNORECASE).
CURRENCY = _token_pattern()


def currency_code(token: str) -> str:
    """ISO code for a symbol, word or code ("¥" -> "JPY"); unknown tokens come back upper."""
    t = token.strip().upper()
    return _ALIASES.get(t, t)


def _configured() -> tuple[dict[str, Decimal], str]:
    rates: dict[str, Decimal] = {}
    for part in (settings.fx_eur_rates or "").split(","):
        code, sep, value = part.partition("=")
        if not sep:
            continue
        try:
            rates[currency_code(code)] = Decimal(value.strip())
        except InvalidOperation:
            continue  # rejected below as missing; a typo never becomes a rate
    if "USD" not in rates and settings.usd_eur_rate:
        try:
            rates["USD"] = Decimal(settings.usd_eur_rate)
        except InvalidOperation:
            pass
    return rates, settings.fx_valid_until or settings.usd_eur_valid_until


def eur_rate(currency: str) -> tuple[Decimal, str, str | None]:
    """(EUR per unit, source, valid until) for `currency`; ValueError if not convertible."""
    code = currency_code(currency)
    if code == "EUR":
        return Decimal(1), "identity", None
    rates, valid_until = _configured()
    if code not in rates:
        raise ValueError(f"Unsupported currency: {code} (no rate in FX_EUR_RATES)")
    try:
        rate = rates[code]
        expiry = datetime.fromisoformat(valid_until)
        if (
            not rate.is_finite()
            or rate <= 0
            or expiry.tzinfo is None
            or expiry <= datetime.now(UTC)
        ):
            raise ValueError("expired or invalid FX")
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(
            f"{code} requires a positive rate in FX_EUR_RATES and a future FX_VALID_UNTIL"
        ) from exc
    return rate, f"configured:FX_EUR_RATES:{code}", expiry.isoformat()


def convertible() -> set[str]:
    """Currencies eur_rate() can convert right now (always includes EUR)."""
    out = {"EUR"}
    for code in _configured()[0]:
        try:
            eur_rate(code)
        except ValueError:
            continue
        out.add(code)
    return out
