"""Mail presentation contracts across agents without sending real mail."""

import re

import pytest

from app.agents import bids, booking, rfq, tracking
from app.mail.style import amount
from app.models import RateOption, Rfq


def assert_style(text: str) -> None:
    assert text.rstrip().endswith("\n\nYour austrian Power Team")
    assert text.count("Your austrian Power Team") == 1
    assert "EUR" not in text
    assert not re.search(r"[–—]|\s-\s|(?m:^- )", text)
    assert not any(x in text for x in ("Best regards", "Viele Grüße", "Thank you,"))


@pytest.mark.parametrize("lang", ["de", "en"])
def test_customer_templates(lang: str) -> None:
    request = Rfq(origin="FRA", destination="JFK", weight_kg=100, language=lang)
    option = RateOption("Test Air", "FRA", "JFK", 2, 147500, "EUR", 147500, "sheet")
    quote = {
        "origin": "FRA",
        "destination": "JFK",
        "airline": "Test Air",
        "total_minor": 147500,
        "currency": "EUR",
    }
    record = dict(
        quote,
        booking_reference="BK-ABC123",
        team_reference="AP-421",
        status="confirmed",
        request={"weight_kg": 100, "pickup_zip": "61018", "ready_date": "2026-11-25"},
        transit_days=2,
        price_minor=147500,
    )
    for text in (
        rfq.quote_text(request, option, 147500, None, 0),
        booking._confirmation(lang, "Mira", quote, "BK-ABC123"),
        tracking.answer_text(record, lang),
    ):
        assert_style(text)
        assert text.count("1.475,00 €") == 1
        assert "FRA nach JFK" in text if lang == "de" else "FRA to JFK" in text
    assert "BK-ABC123" in tracking.answer_text(record, lang)
    assert_style(tracking._not_found_text(lang))


@pytest.mark.parametrize("kind", ["airline", "trucker"])
def test_carrier_bid_request(kind: str) -> None:
    request = Rfq(destination="JFK", pickup_zip="61018", weight_kg=100)
    assert_style(bids._body(request, "FRA", kind))
    assert "[AP-421]" in bids._subject(421, request, "FRA", kind)


def test_booking_request_keeps_currency_and_reference(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(
        booking.hackloop,
        "send",
        lambda to, subject, body: sent.append((subject, body)) or {"id": 1},
    )
    monkeypatch.setattr(booking.db, "record_outgoing", lambda **kwargs: None)
    quote = {"origin": "FRA", "destination": "JFK", "total_minor": 147500, "currency": "EUR"}
    booking._book_by_email(421, quote, {"weight_kg": 100}, "airline@hackloop.mail")
    subject, body = sent[0]
    assert "[AP-421]" in subject
    assert "FRA to JFK" in subject
    assert "1.475,00 €" in body
    assert "AP-421" in body
    assert_style(body)
    assert quote["currency"] == "EUR"


def test_money_format_preserves_cents_and_other_currencies() -> None:
    assert amount(1) == "0,01 €"
    assert amount(0) == "0,00 €"
    assert amount(147501) == "1.475,01 €"
    assert amount(147501, "USD") == "1.475,01 USD"
