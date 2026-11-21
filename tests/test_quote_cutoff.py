"""Acceptance deadlines must not be invented from departure or rate validity."""

import json

import pytest

from app import db
from app.agents import rfq
from app.models import RateOption, Rfq


@pytest.fixture(autouse=True)
def database(tmp_path):
    db.init_db(str(tmp_path / "cutoff.db"))


def payload(body):
    return json.dumps(
        {
            "body": body,
            "answered_questions": [],
            "unavailable_questions": [0],
            "addressed_constraints": [],
            "unavailable_constraints": [],
            "referenced_mail_ids": [],
        }
    )


@pytest.mark.parametrize(
    "lang,question",
    [
        ("en", "Please confirm the drop-off cutoff."),
        ("de", "Wann ist der Annahmeschluss?"),
    ],
)
@pytest.mark.parametrize(
    "claim",
    [
        "Drop-off cutoff is until close of business on 28 November.",
        "Cargo acceptance is possible until 18:00.",
        "Annahmeschluss ist um 17 Uhr.",
    ],
)
def test_unverified_cutoff_repairs_then_falls_back(monkeypatch, lang, question, claim):
    request = Rfq(
        origin="MUC",
        destination="PVG",
        weight_kg=134,
        ready_date="2026-11-28",
        language=lang,
        customer_questions=[question],
    )
    option = RateOption("Carrier", "MUC", "PVG", 3, 53466, "EUR", 53466, "portal")
    body = f"Hello, Carrier MUC to PVG: 534,66 € all-in. {claim}\n\nYour austrian Power Team"
    calls = []

    def complete(purpose, messages, *args, **kwargs):
        calls.append(purpose)
        facts = json.loads(messages[1]["content"])["verified_facts"]
        assert facts["confirmed_cargo_acceptance_cutoff"] is None
        return payload(body)

    monkeypatch.setattr(rfq.llm, "complete", complete)
    text = rfq.compose_quote_text(request, option, 53466, None, 1)
    assert claim not in text
    assert rfq._CUTOFF_NOTICES[lang] in text
    assert text.count("534,66 €") == 1
    assert calls == ["compose_quote", "compose_quote_repair"]


def test_personal_quote_keeps_booking_validity_and_adds_missing_cutoff_answer(
    monkeypatch,
):
    request = Rfq(
        origin="FRA",
        destination="DEL",
        weight_kg=100,
        customer_questions=["What is the cutoff?"],
    )
    option = RateOption("Carrier", "FRA", "DEL", 2, 10000, "EUR", 10000, "sheet")
    body = (
        "Hello, happy to help again. Carrier FRA to DEL: 100,00 € all-in. "
        "This rate can be booked until 18:00.\n\nYour austrian Power Team"
    )
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **kw: payload(body))
    text = rfq.compose_quote_text(request, option, 10000, None, 1)
    assert "happy to help again" in text and "booked until 18:00" in text
    assert text.count(rfq._CUTOFF_NOTICES["en"]) == 1


def test_airport_quote_without_cutoff_question_is_unchanged():
    request = Rfq(origin="FRA", destination="DEL", weight_kg=100)
    option = RateOption("Carrier", "FRA", "DEL", 2, 10000, "EUR", 10000, "sheet")
    assert "cutoff" not in rfq.quote_text(request, option, 10000, None, 0)
