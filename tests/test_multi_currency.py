"""Foreign currencies (JPY, GBP, CHF, ...) in bids, portal rates and tariffs.

Every amount keeps its original currency; conversion to EUR only with a configured rate.
"""

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app import db
from app.agents import bid_reader, bids, tariff
from app.models import Mail, Rfq
from app.pricing import fx, portal_sync, pricing
from app.pricing.tariff_parser import TariffDocument

ROOT = Path(__file__).parents[1]
RATE = json.loads((ROOT / "tests/fixtures/portal_rate_eur.json").read_text())


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


@pytest.mark.parametrize(
    "token,code",
    [
        ("¥", "JPY"),
        ("yen", "JPY"),
        ("£", "GBP"),
        ("US$", "USD"),
        ("CN¥", "CNY"),
        ("€", "EUR"),
        ("chf", "CHF"),
        ("SFr", "CHF"),
    ],
)
def test_currency_aliases(token, code):
    assert fx.currency_code(token) == code


def test_configured_rates_convert_and_record_source(monkeypatch):
    _rates(monkeypatch)
    rate, source, expiry = fx.eur_rate("JPY")
    assert rate == Decimal("0.0062") and source == "configured:FX_EUR_RATES:JPY" and expiry
    assert fx.eur_rate("¥")[0] == Decimal("0.0062")
    assert fx.convertible() == {"EUR", "JPY", "GBP", "CHF"}


def test_unconfigured_or_expired_currency_is_not_converted(monkeypatch):
    _rates(monkeypatch)
    with pytest.raises(ValueError):
        fx.eur_rate("SEK")
    _rates(monkeypatch, hours=-1)
    with pytest.raises(ValueError):
        fx.eur_rate("JPY")
    assert fx.convertible() == {"EUR"}


def test_bad_rate_never_becomes_a_rate(monkeypatch):
    _rates(monkeypatch, rates="JPY=abc,GBP=0,CHF=1.06")
    assert fx.convertible() == {"EUR", "CHF"}


def test_legacy_usd_setting_still_works(monkeypatch):
    _rates(monkeypatch, rates="", usd="0.92")
    assert fx.eur_rate("USD")[0] == Decimal("0.92")
    _rates(monkeypatch, rates="USD=0.90", usd="0.92")  # the new setting wins
    assert fx.eur_rate("USD")[0] == Decimal("0.90")


# ---------------------------------------------------------------- bid text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("All-in total JPY 152,300, transit 3 days", (Decimal("152300.00"), "JPY")),
        ("Total: ¥152,300", (Decimal("152300.00"), "JPY")),
        ("Our price is 152.300 Yen all-in", (Decimal("152300.00"), "JPY")),
        ("Total £ 840.50", (Decimal("840.50"), "GBP")),
        ("Total CHF 1'250.00", (Decimal("1250.00"), "CHF")),
        ("Total US$ 1,240.00", (Decimal("1240.00"), "USD")),
        ("Total 1.240,00 €", (Decimal("1240.00"), "EUR")),
    ],
)
def test_bid_prices_in_any_currency(text, expected):
    assert bids.parse_price(text) == expected


def test_weight_in_pounds_is_not_money():
    assert bids.parse_price("Shipment of 250 pounds, transit 2 days") is None


def test_document_total_with_foreign_column_header():
    text = "Item | Amount (JPY)\nFreight | 140,000\nFuel | 12,300\nTotal | 152,300"
    assert bids.document_price(text) == (Decimal("152300.00"), "JPY")


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


def test_jpy_bid_without_rate_is_skipped_not_guessed(monkeypatch):
    _rates(monkeypatch, rates="")
    mail = Mail(
        10,
        10,
        "yellowtail-air-freight@hackloop.mail",
        "team",
        "[AP-5] bid",
        "Total JPY 100,000, 2 days",
        datetime.now(UTC).isoformat(),
    )
    bids.on_bid_mail(mail, 5)
    assert bids.offers(5) == []
    bids.close(5)


def test_model_reading_accepts_symbol_for_configured_currency():
    raw = json.dumps({"total": "152300", "currency": "¥", "transit_days": 3})
    reading = bid_reader.check(raw, "Gesamt 152,300 Yen", {"EUR", "JPY"})
    assert reading is not None and reading.currency == "JPY"
    assert bid_reader.check(raw, "Gesamt 152,300 Yen", {"EUR", "USD"}) is None


# ---------------------------------------------------------------- portal and tariffs


def test_portal_rate_in_jpy_uses_its_own_fx_field():
    rate = portal_sync.parse_rate(
        {**RATE, "total": "100000", "currency": "JPY", "fx_eur_per_jpy": "0.006"}
    )
    assert (rate.currency, rate.total_eur_minor) == ("JPY", 60000)
    assert rate.fx_source == "portal:fx_eur_per_jpy"


def test_portal_rate_in_gbp_falls_back_to_configured_rate(monkeypatch):
    _rates(monkeypatch)
    rate = portal_sync.parse_rate({**RATE, "total": "100", "currency": "GBP"})
    assert (rate.total_eur_minor, rate.fx_source) == (11700, "configured:FX_EUR_RATES:GBP")


def test_tariff_sheet_in_jpy_prices_in_eur(monkeypatch):
    _rates(monkeypatch)
    doc = TariffDocument.model_validate(
        {
            "airline": "Yellowtail Air Freight",
            "currency": "JPY",
            "valid_from": "2026-12-01",
            "valid_to": "2026-12-31",
            "lanes": [
                {
                    "origin": "FRA",
                    "destination": "DFW",
                    "transit_days": 2,
                    "minimum": "1000",
                    "fuel": "50",
                    "security": "10",
                    "breaks": [{"from_kg": 0, "rate": "200"}, {"from_kg": 100, "rate": "100"}],
                }
            ],
        }
    )
    tariff.publish(
        tariff.TariffUpdate(doc, 500, "jpy", "2026-11-21T16:00:00+00:00"), pricing.RUNTIME_TARIFFS
    )
    option = pricing.sheet_options(
        Rfq(origin="FRA", destination="DFW", weight_kg=120), on=date(2026, 12, 1)
    )[0]
    # 120 kg x (100 + 50 + 10) = 19,200 JPY -> 119.04 EUR at 0.0062
    assert (option.currency, option.total_minor, option.total_eur_minor) == (
        "JPY",
        1920000,
        11904,
    )
