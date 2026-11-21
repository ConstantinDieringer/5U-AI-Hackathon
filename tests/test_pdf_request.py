"""QUOTE_PDF_ON_REQUEST: the quote carries the PDF only when the RFQ asks for one. Detection is
rule-based (no LLM call); the customer's own PDFs and declined PDFs are not requests."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app import db
from app.agents import rfq as rfq_agent
from app.mail.pdf_request import asks_for_pdf
from app.models import RateOption, Rfq

ASKS = [
    "Please send the quote as PDF.",
    "Could you share your offer as a PDF for our records?",
    "We need the quotation in PDF format, thanks.",
    "Can you provide a PDF quote?",
    "Bitte schicken Sie uns das Angebot als PDF.",
    "Wir brauchen ein PDF-Angebot für die Buchhaltung.",
    "Hätten Sie das Angebot auch als PDF?",
    "Merci de nous envoyer l'offre en PDF.",
    "¿Nos puede enviar la cotización en formato PDF?",
    "Ci può inviare l'offerta in formato PDF?",
    "Kindly send PDF version of the offer",
]

NOT_ASKS = [
    "Please find attached the PDF document with the dimensions.",
    "See the attached PDF for the packing list.",
    "Anbei das PDF-Dokument mit den Maßen.",
    "No PDF needed, a short mail is fine.",
    "Ein PDF ist nicht nötig.",
    "Packing list: packing_list.pdf",
    "4 pallets, 380 kg, MUC to BKK, cheapest please.",
    "The PDF you sent last time was fine.",
]


@pytest.mark.parametrize("text", ASKS)
def test_requests_are_found(text: str) -> None:
    assert asks_for_pdf(f"Hi,\n\n4 pallets MUC-BKK, 380 kg.\n{text}\n\nBest regards\nAnna")


@pytest.mark.parametrize("text", NOT_ASKS)
def test_non_requests_are_ignored(text: str) -> None:
    assert not asks_for_pdf(f"Hi,\n\n4 pallets MUC-BKK, 380 kg.\n{text}\n\nBest regards\nAnna")


def test_subject_counts_but_not_on_a_reply() -> None:
    assert asks_for_pdf("4 pallets MUC-BKK", "RFQ MUC-BKK, offer as PDF please")
    assert not asks_for_pdf("4 pallets MUC-BKK", "Re: offer as PDF please")


def test_quoted_history_and_signature_do_not_count() -> None:
    body = (
        "New shipment: 2 boxes FRA-JFK, 40 kg.\n\nOn Mon, Anna wrote:\n"
        "> Please send the quote as PDF.\n"
    )
    assert not asks_for_pdf(body)
    assert not asks_for_pdf("2 boxes FRA-JFK\n\nBest regards\nAnna\nOffers as PDF on request")


def _rfq() -> Rfq:
    return Rfq(origin="MUC", destination="BKK", weight_kg=76, language="en")


def _choice() -> RateOption:
    return RateOption(
        "Polaris Air", "MUC", "BKK", 3, 31084, "EUR", 31084, "portal",
        rate_id="R-1", valid_until="2026-11-28T00:00:00Z", chargeable_kg=76.0, billed_kg=100.0,
    )  # fmt: skip


def test_flag_gates_the_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "emit", lambda *a, **k: None)
    always = dataclasses.replace(rfq_agent.settings, quote_pdf=True, quote_pdf_on_request=False)
    monkeypatch.setattr(rfq_agent, "settings", always)
    args: tuple[Any, ...] = (3, _rfq(), _choice(), 31084, None)
    assert rfq_agent._quote_pdf(*args, asked=False)  # flag off: every quote has the PDF

    on_request = dataclasses.replace(always, quote_pdf_on_request=True)
    monkeypatch.setattr(rfq_agent, "settings", on_request)
    assert rfq_agent._quote_pdf(*args, asked=False) is None
    [att] = rfq_agent._quote_pdf(*args, asked=True) or []
    assert att["filename"] == "Quotation_AP-3.pdf"


def test_notfall_quote_follows_the_same_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ui import server

    monkeypatch.setattr(db, "emit", lambda *a, **k: None)
    on = dataclasses.replace(server.settings, quote_pdf=True, quote_pdf_on_request=True)
    monkeypatch.setattr("app.config.settings", on)
    q = {"total_minor": 202000, "currency": "EUR", "choice": {"airline": "Nova"}}
    rfq = {"direction": "in", "sender": "anna@kunde.example", "subject": "RFQ DUS-SYD"}
    s: dict[str, Any] = {
        "customer": "anna@kunde.example",
        "request": {"language": "en", "weight_kg": 120},
        "mails": [{**rfq, "body": "120 kg DUS to SYD, cheapest."}],
    }
    assert server._manual_quote_pdf(9, s, q) is None
    s["mails"] = [{**rfq, "body": "120 kg DUS to SYD. Please send the offer as PDF."}]
    [att] = server._manual_quote_pdf(9, s, q) or []
    assert att["filename"].endswith("AP-9.pdf")


def test_default_is_pdf_only_on_request() -> None:
    from app.config import settings

    assert settings.quote_pdf_on_request is True
