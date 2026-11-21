"""Customer display rules preserve facts, references, and the existing repair path."""

import json
from datetime import date

import pytest

from app import db, llm
from app.agents import booking, clarification, rfq, tracking
from app.mail.style import customer_presentation, has_customer_presentation_issue, written_date
from app.models import RateOption, Rfq, RfqDraft


@pytest.mark.parametrize("lang,expected", [("de", "24. November 2026"), ("en", "24 November 2026")])
def test_dates_currency_and_embedded_identifiers(lang, expected):
    text = customer_presentation(
        "On 2026-11-24 the total is 1.200,00 EUR. BK-2026-11-24 and R-EUR-123 stay.", lang
    )
    assert expected in text and "1.200 €" in text
    assert "BK-2026-11-24" in text and "R-EUR-123" in text
    assert written_date(date(2026, 11, 24), lang) == expected
    assert not has_customer_presentation_issue(text)
    assert "not-a-date" == customer_presentation("not-a-date", lang)


def test_internal_label_at_sentence_end_but_not_a_filename_or_contact():
    assert has_customer_presentation_issue("The field is ready_date.")
    assert not has_customer_presentation_issue("Use packing_list.pdf or sara_sales@example.test.")


def test_long_paragraphs_split_at_sentences_without_splitting_german_dates():
    first = "Am 24. November 2026 " + "kommt " * 34 + "die Sendung an."
    second = "Wir " + "prüfen " * 34 + "die Buchung."
    text = customer_presentation(first + " " + second, "de")
    assert text == first + "\n\n" + second
    assert "24. November 2026" in text
    assert not has_customer_presentation_issue(text)


def test_timestamp_keeps_the_same_instant_and_explicit_zone():
    assert (
        customer_presentation("2026-11-24T00:15:00+02:00", "en") == "23 November 2026 at 22:15 UTC"
    )


@pytest.mark.usefixtures("compose_repair_on")
@pytest.mark.parametrize(
    "issue",
    [
        "The timing is road_transit.",
        "The weight_kg is 736.",
        "Origin: FRA",
        "Booked (subject to confirmation).",
        "Ready on 24.11.2026.",
        "Ready on 24 Nov 2026.",
        "2026-99-24",
        "word " * 71,
    ],
)
def test_unfriendly_or_internal_presentation_is_repaired(tmp_path, monkeypatch, issue):
    db.init_db(str(tmp_path / "test.db"))
    option = RateOption("Test Air", "FRA", "DEL", 2, 120000, "EUR", 120000, "sheet")
    request = Rfq(origin="FRA", destination="DEL", weight_kg=736)
    calls = []

    def complete(purpose, *args, **kwargs):
        calls.append(purpose)
        body = "Hi Sara,\n\nTest Air flies FRA to DEL for 1.200 €.\n\n"
        body += issue if len(calls) == 1 else "The earliest possible departure is 2026-11-24."
        return json.dumps(
            {
                "body": body + "\n\nYour austrian Power Team",
                "answered_questions": [],
                "unavailable_questions": [],
                "addressed_constraints": [],
                "unavailable_constraints": [],
                "referenced_mail_ids": [],
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    text = rfq.compose_quote_text(request, option, 120000, None, 1)
    assert calls == ["compose_quote", "compose_quote_repair"]
    assert "24 November 2026" in text and "1.200 €" in text
    assert not has_customer_presentation_issue(text)


@pytest.mark.parametrize("lang", ["de", "en"])
def test_templates_use_complete_prose_without_parentheses(lang):
    quote = {
        "origin": "FRA",
        "destination": "DEL",
        "airline": "Test Air",
        "total_minor": 120000,
        "currency": "EUR",
    }
    request = Rfq(
        origin="FRA", destination="DEL", weight_kg=736, language=lang, ready_date="2026-11-24"
    )
    option = RateOption("Test Air", "FRA", "DEL", 2, 120000, "EUR", 120000, "sheet")
    messages = [
        booking._confirmation(lang, "Sara", quote, "BK-ABC123"),
        tracking._not_found_text(lang),
        rfq.quote_text(request, option, 120000, None, 0, today=date(2026, 11, 21)),
    ]
    for field in ["origin", "pickup_zip", "volume_cbm", "dangerous_goods"]:
        messages.append(
            clarification.text_for(RfqDraft(issues=[{"field": field, "code": "missing"}]), lang)
        )
    for text in messages:
        assert "(" not in text and ")" not in text and "EUR" not in text
        assert "\n\n" in text
        assert not has_customer_presentation_issue(text)
    record = {
        **quote,
        "price_minor": 120000,
        "booking_reference": "BK-ABC123",
        "status": "confirmed",
        "transit_days": 2,
        "booked_at": "2026-11-21",
        "request": {"weight_kg": 736, "ready_date": "2026-11-24", "pickup_zip": "70173"},
    }
    for variant in range(4):
        text = tracking.answer_text(record, lang, variant=variant, today=date(2026, 11, 21))
        assert not has_customer_presentation_issue(text)
        assert "1.200 €" in text and "70173" in text and "BK-ABC123" in text
