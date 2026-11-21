"""QUOTE_PDF: the quote goes out with a PDF attachment that code renders from the priced
option. No LLM call, the printed total is exactly the mail's total, and a broken PDF never
stops the quote."""

from __future__ import annotations

import base64
import dataclasses
import io
from decimal import Decimal
from typing import Any

import pdfplumber
import pytest

from app import db, hackloop
from app.agents import rfq as rfq_agent
from app.mail import quote_pdf
from app.models import RateOption, Rfq


def _rfq(**kw: Any) -> Rfq:
    base: dict[str, Any] = {
        "origin": "MUC",
        "destination": "BKK",
        "weight_kg": 76,
        "volume_cbm": 0.31,
        "pieces": 9,
        "language": "de",
        "customer_name": "Anna Berger",
    }
    return Rfq(**{**base, **kw})


def _choice() -> RateOption:
    return RateOption(
        "Polaris Air", "MUC", "BKK", 3, 31084, "EUR", 31084, "portal",
        rate_id="R-1", valid_until="2026-11-28T00:00:00Z", chargeable_kg=76.0, billed_kg=100.0,
    )  # fmt: skip


def _text(pdf: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 1
        return doc.pages[0].extract_text() or ""


def test_pdf_states_the_exact_all_in_total_and_the_facts() -> None:
    doc = quote_pdf.quote_doc(42, _rfq(), _choice(), 147500, None)
    text = _text(quote_pdf.render(doc))
    assert "Angebot Luftfracht" in text
    assert "1.475 €" in text  # same formatter as the mail body
    for fact in ("AP-42", "MUC - BKK", "Polaris Air", "Anna Berger", "9 Packstücke", "100 kg"):
        assert fact in text
    assert "28. November 2026" in text


def test_cents_and_other_languages() -> None:
    for lang, title in (("en", "Air freight quotation"), ("fr", "Offre de fret aérien"),
                        ("es", "Oferta de carga aérea"), ("it", "Offerta di trasporto aereo"),
                        ("pt", "Air freight quotation")):  # fmt: skip
        doc = quote_pdf.quote_doc(7, _rfq(language=lang), _choice(), 39016, None)
        text = _text(quote_pdf.render(doc))
        assert title in text and "390,16 €" in text


def test_door_to_port_names_the_pickup() -> None:
    from app.agents.bids import Offer

    truck = Offer(carrier=object(), total_eur=Decimal("79.32"), transit_days=1, mail_id=1)  # type: ignore[arg-type]
    doc = quote_pdf.quote_doc(5, _rfq(origin=None, pickup_zip="84455"), _choice(), 39016, truck)
    assert "Abholung 84455" in _text(quote_pdf.render(doc))


def test_attachment_shape_matches_the_mailbox_api() -> None:
    att = quote_pdf.attachment(quote_pdf.quote_doc(42, _rfq(), _choice(), 147500, None))
    assert att["filename"] == "Angebot_AP-42.pdf"
    assert att["content_type"] == "application/pdf"
    assert base64.b64decode(att["content_base64"]).startswith(b"%PDF-1.4")


def test_text_outside_winansi_and_pdf_syntax_cannot_break_the_file() -> None:
    doc = quote_pdf.quote_doc(1, _rfq(customer_name="Dr. (Zhang) 张 \\ Li"), _choice(), 100, None)
    assert "Dr. (Zhang) ? \\ Li" in _text(quote_pdf.render(doc))


def test_manual_notfall_quote_document() -> None:
    doc = quote_pdf.manual_doc(
        9,
        {"language": "en", "weight_kg": 120, "customer_name": "Sam"},
        {"airline": "Nova", "origin": "DUS", "destination": "SYD", "transit_days": 2},
        202000,
        "EUR",
    )
    text = _text(quote_pdf.render(doc))
    assert "2.020 €" in text and "DUS - SYD" in text and "120 kg" in text


def test_send_puts_attachments_into_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}
    monkeypatch.setattr(db, "reply_blocked", lambda _mid: False)
    monkeypatch.setattr(hackloop, "_request", lambda *a, **k: sent.update(k) or object())
    monkeypatch.setattr(hackloop, "_json_obj", lambda _r: {"id": 1, "references": []})
    att = {"filename": "a.pdf", "content_type": "application/pdf", "content_base64": "JVBE"}
    hackloop.send("customer@hackloop.mail", "Re: x", "hello", in_reply_to=5, attachments=[att])
    assert sent["json"]["attachments"] == [att]
    hackloop.send("customer@hackloop.mail", "Re: y", "hello again", in_reply_to=6)
    assert "attachments" not in sent["json"]


def test_rfq_quote_pdf_flag_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        db, "emit", lambda kind, msg, *a, **k: events.append((kind, k.get("level", "info")))
    )
    on = dataclasses.replace(rfq_agent.settings, quote_pdf=True)
    monkeypatch.setattr(rfq_agent, "settings", on)
    [att] = rfq_agent._quote_pdf(3, _rfq(), _choice(), 147500, None) or []
    assert att["filename"] == "Angebot_AP-3.pdf"

    def broken(*_a: Any, **_k: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(quote_pdf, "quote_doc", broken)
    assert rfq_agent._quote_pdf(3, _rfq(), _choice(), 147500, None) is None
    assert ("quote_pdf_failed", "error") in events  # error level reaches #sos

    monkeypatch.setattr(rfq_agent, "settings", dataclasses.replace(on, quote_pdf=False))
    assert rfq_agent._quote_pdf(3, _rfq(), _choice(), 147500, None) is None
