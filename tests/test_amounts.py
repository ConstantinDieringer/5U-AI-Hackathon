"""Money read from carrier and airline text: one parser, explicit totals only (rate-sheet and
bid-mail traps, each with its own case)."""

import json
from decimal import Decimal

import pytest

from app.agents import bid_reader, bids
from app.models import Rfq
from app.pricing.amounts import mail_total, parse_decimal
from app.pricing.reference import carriers
from app.pricing.tariff_parser import number
from app.pricing.truck_estimate import PastBid, estimate


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234.50", "1234.50"),
        ("1.234,50", "1234.50"),
        ("1'234.50", "1234.50"),
        ("1’234.50", "1234.50"),
        ("1 234", "1234"),
        ("1.250", "1250"),
        ("1,250", "1250"),
        ("2,85", "2.85"),
        ("12,5", "12.5"),
        # a per-kg rate with three decimals is not thousands
        ("0.125", "0.125"),
        ("0,125", "0.125"),
        ("-3", "-3"),
    ],
)
def test_parse_decimal_reads_both_separator_conventions(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "text,expected",
    [
        # trap: a waiting-time rate per day is larger than the all-in price
        (
            "Our all-in price is 1,240 EUR. Waiting time is charged at 1,500 EUR per day "
            "if required.",
            ("1240.00", "EUR"),
        ),
        # trap: an optional extra is larger than the total
        ("Total 980 EUR, optional insurance extra 1,200 EUR", ("980.00", "EUR")),
        # trap: airline confirmation naming the per-kg rate and the insured cargo value
        (
            "Booking BK-1 confirmed. Rate 2.85 EUR/kg, total 2,450.00 EUR, "
            "cargo value 30,000 EUR insured.",
            ("2450.00", "EUR"),
        ),
        ("Price: 800 EUR + fuel 200 EUR = 1,000 EUR all-in", ("1000.00", "EUR")),
        ("All-in 500 EUR per shipment, no extra charges", ("500.00", "EUR")),
        ("Freight 800 EUR, fuel 200 EUR, total 1,000 EUR", ("1000.00", "EUR")),
        ("Rate 2.85 EUR/kg, minimum 150 EUR, total 2,450 EUR", ("2450.00", "EUR")),
    ],
)
def test_mail_total_takes_the_explicit_total(text, expected):
    assert mail_total(text) == (Decimal(expected[0]), expected[1])
    assert bids.parse_price(text) == mail_total(text)


@pytest.mark.parametrize(
    "text",
    [
        "Rate 2.85 EUR/kg",  # a per-kg rate alone is no total
        "Rate 2.85 EUR/kg, min 150 EUR",  # neither is a minimum charge
        "Pickup 180 EUR and delivery 60 EUR",  # two components, no total: never the largest
        "Total 980 EUR, total 1,100 EUR",  # two different totals
        "Cargo value 30,000 EUR insured",
        "Waiting time 45 EUR per hour",
    ],
)
def test_mail_total_never_guesses(text):
    assert mail_total(text) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Minimum EUR 1,250.00", "1250.00"),
        ("1.250,00", "1250.00"),
        ("2,85", "2.85"),
        ("0.125", "0.125"),
        ("144", "144"),
    ],
)
def test_tariff_numbers_keep_thousands_separators(value, expected):
    assert number(value) == Decimal(expected)


def _reading(total: str, text: str, currency: str = "EUR"):
    raw = json.dumps({"total": total, "currency": currency, "transit_days": None})
    return bid_reader.check(raw, text, {"EUR", "USD"})


def test_model_total_must_be_written_as_money_in_the_bid():
    # the weight, a per-kg rate and a reference number are written, but not as the price
    text = "Shipment 500 kg, rate 2.85 EUR/kg, ref 4711, all in as discussed"
    assert _reading("500", text) is None
    assert _reading("2.85", text) is None
    assert _reading("4711", text) is None


def test_model_total_needs_the_currency_the_bid_names():
    assert _reading("450", "Total 450 EUR", currency="USD") is None
    assert _reading("450", "Total 450 EUR").amount == Decimal(450)


def test_model_total_reads_decimal_comma():
    # "450,00" is 450, not 45000
    assert _reading("450,00", "Gesamt 450,00 EUR").amount == Decimal("450.00")


def test_same_day_offer_is_never_zero_transit_days():
    carrier = next(c for c in carriers() if c.kind == "airline")
    offer = bids.Offer(carrier, Decimal("500"), 0.5, 1)
    rfq = Rfq(origin="MUC", destination="ORD", weight_kg=100, volume_cbm=0.5)
    assert bids.airline_option(offer, rfq, "MUC", 100.0).transit_days == 1


def test_truck_estimate_rounds_through_money():
    bid = PastBid("t@hackloop.mail", Decimal("100.00"), 100, 1)
    result = estimate([bid], 200)
    assert result is not None and result.total_eur == Decimal("141.42")
