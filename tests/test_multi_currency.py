"""Foreign currencies (JPY, GBP, CHF, ...) in bids, portal rates and tariffs.

Every amount keeps its original currency; conversion to EUR only with a configured rate.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app import db
from app.agents import bids
from app.models import Mail
from app.pricing import fx, portal_sync, pricing


def _rates(monkeypatch, rates="JPY=0.0062,GBP=1.17,CHF=1.06", hours=1, usd=""):
    expiry = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
    monkeypatch.setattr(
        fx,
        "settings",
        dataclasses.replace(
            fx.settings,
            fx_eur_rates=rates,
            fx_valid_until=expiry,
            usd_eur_rate=usd,
            usd_eur_valid_until=expiry if usd else "",
        ),
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(pricing, "RUNTIME_TARIFFS", tmp_path / "tariffs.json")
    monkeypatch.setattr(portal_sync, "_cache", {})
    monkeypatch.setattr(portal_sync, "_seen_dataset_version", None)
    monkeypatch.setattr(portal_sync, "refresh_if_stale", lambda *a: None)
    pricing.reload_lanes()
    yield
    pricing.reload_lanes()


# ---------------------------------------------------------------- fx module


def test_unconfigured_or_expired_currency_is_not_converted(monkeypatch):
    _rates(monkeypatch)
    with pytest.raises(ValueError):
        fx.eur_rate("SEK")
    _rates(monkeypatch, hours=-1)
    with pytest.raises(ValueError):
        fx.eur_rate("JPY")
    assert fx.convertible() == {"EUR"}


# ---------------------------------------------------------------- bid text


def test_jpy_bid_is_converted_with_configured_rate(monkeypatch):
    _rates(monkeypatch)
    mail = Mail(
        9,
        9,
        "yellowtail-air-freight@hackloop.mail",
        "team",
        "[AP-4] bid",
        "Total JPY 100,000, 2 days",
        datetime.now(UTC).isoformat(),
    )
    bids.on_bid_mail(mail, 4)
    offer = bids.offers(4)[-1]
    assert (offer.original_amount, offer.currency) == (Decimal("100000.00"), "JPY")
    assert offer.total_eur == Decimal("620.00")
    bids.close(4)
