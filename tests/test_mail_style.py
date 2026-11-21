"""Mail presentation contracts across agents without sending real mail."""

import json
import re

import pytest

from app.agents import bids, booking, rfq, tracking
from app.mail.style import (
    SIGNATURE,
    TEAM_LINE,
    amount,
    booked_joke,
    has_template_artifacts,
)
from app.models import RateOption, Rfq


def assert_style(text: str) -> None:
    assert text.rstrip().endswith("\n\n" + SIGNATURE) and SIGNATURE.startswith(
        "Moritz\n"
    )
    assert text.count(TEAM_LINE) == 1
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
    quote = {
        "origin": "FRA",
        "destination": "JFK",
        "total_minor": 147500,
        "currency": "EUR",
    }
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


@pytest.mark.parametrize(
    "bad_copy",
    [
        "Route FRA – JFK",
        "Route FRA - JFK",
        "- Price",
        "Best regards,",
        "[Company]",
        "[Your Company]",
        "[Your Name]",
        "{company}",
        "<name>",
        "\nSubject: Freight quote",
        "\nBetreff: Angebot",
    ],
)
def test_composer_falls_back_when_style_is_wrong(monkeypatch, bad_copy: str) -> None:
    request = Rfq(
        origin="FRA",
        destination="JFK",
        weight_kg=100,
        customer_questions=["Can you insure the cargo?"],
    )
    option = RateOption("Test Air", "FRA", "JFK", 2, 147500, "EUR", 147500, "sheet")
    response = {
        "body": f"Hello,\n\n1.475,00 € all-in. {bad_copy}\n\nYour austrian Power Team",
        "answered_questions": [],
        "unavailable_questions": [0],
        "addressed_constraints": [],
        "unavailable_constraints": [],
    }
    monkeypatch.setattr(
        rfq.llm, "complete", lambda *args, **kwargs: json.dumps(response)
    )
    text = rfq.compose_quote_text(request, option, 147500, None, 1)
    assert_style(text)
    assert "Verified information" not in text and "all-in" in text


@pytest.mark.parametrize("lang", ["de", "en"])
def test_placeholder_customer_name_uses_generic_greeting(lang: str) -> None:
    request = Rfq(
        origin="FRA",
        destination="JFK",
        weight_kg=100,
        customer_name="[Your Name]",
        language=lang,
    )
    option = RateOption("Test Air", "FRA", "JFK", 2, 147500, "EUR", 147500, "sheet")
    quote = {
        "origin": "FRA",
        "destination": "JFK",
        "airline": "Test Air",
        "total_minor": 147500,
        "currency": "EUR",
    }
    for body in (
        rfq.quote_text(request, option, 147500, None, 0),
        booking._confirmation(lang, request.customer_name, quote, "BK-ABC123"),
    ):
        assert not has_template_artifacts(body)
        assert_style(body)


def test_reference_and_date_hyphens_are_not_template_fields() -> None:
    assert not has_template_artifacts("Booking BK-ABC123 for AP-421, ready 2026-11-25")


def test_booking_confirmation_has_a_joke_and_the_signer():
    quote = {
        "origin": "FRA",
        "destination": "JFK",
        "airline": "A",
        "total_minor": 100,
        "currency": "EUR",
    }
    for lang in ("de", "en"):
        text = booking._confirmation(lang, "Sara", quote, "BK-ABC123")
        assert booked_joke(lang, "BK-ABC123") in text
        assert_style(text)


def test_no_generic_unverified_line_in_fallback():
    request = Rfq(
        origin="FRA",
        destination="JFK",
        weight_kg=100,
        customer_questions=["Can you insure the cargo?"],
    )
    option = RateOption("Test Air", "FRA", "JFK", 2, 147500, "EUR", 147500, "sheet")
    text = rfq._fallback_with_coverage(request, option, 147500, None, 1, None)
    assert "Verified information" not in text
