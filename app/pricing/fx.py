"""Explicit EUR/USD conversion. Never infer a rate from unrelated portal calls."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.config import settings


def eur_rate(currency: str) -> tuple[Decimal, str, str | None]:
    if currency == "EUR":
        return Decimal(1), "identity", None
    if currency != "USD":
        raise ValueError(f"Unsupported currency: {currency}")
    try:
        rate = Decimal(settings.usd_eur_rate)
        expiry = datetime.fromisoformat(settings.usd_eur_valid_until)
        if (
            not rate.is_finite()
            or rate <= 0
            or expiry.tzinfo is None
            or expiry <= datetime.now(UTC)
        ):
            raise ValueError("expired or invalid FX")
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(
            "USD requires a positive USD_EUR_RATE and future USD_EUR_VALID_UNTIL"
        ) from exc
    return rate, "configured:USD_EUR_RATE", expiry.isoformat()
