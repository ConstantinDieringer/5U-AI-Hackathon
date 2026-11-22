"""Requested language survives source reading, composition, repair and wire formatting."""

from datetime import date

import pytest

from app import db, hackloop
from app.agents import rfq
from app.mail.language import detect
from app.mail.style import SIGNATURE, amount
from app.mail.subjects import shipment_subject
from app.models import RateOption, Rfq


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))


def request(lang):
    return Rfq(
        origin="DUS",
        destination="SYD",
        weight_kg=279,
        volume_cbm=2.47,
        pieces=9,
        ready_date="2026-11-26",
        preference="fastest",
        language=lang,
    )


def option():
    return RateOption("Nova Atlas Cargo", "DUS", "SYD", 3, 202000, "EUR", 202000, "sheet")


@pytest.mark.parametrize(
    "lang,month",
    [("fr", "novembre"), ("pl", "listopada")],
)
@pytest.mark.core
def test_fallback_and_actual_customer_wire_preserve_language(lang, month, monkeypatch):
    calls = []

    def unavailable(purpose, *args, **kwargs):
        calls.append(purpose)
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(rfq.llm, "complete", unavailable)
    body = rfq.compose_quote_text(
        request(lang), option(), 202000, None, 1, today=date(2026, 11, 21)
    )
    assert detect(body.replace(SIGNATURE, "")) == lang
    assert month in body and "2026" in body
    assert body.count(amount(202000)) == 1
    assert "DUS" in body and "SYD" in body and "Nova Atlas Cargo" in body
    sent = []

    def send(to, subject, body, in_reply_to=None, references=None):
        sent.append(
            {"id": 999, "to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to}
        )
        return sent[-1]

    monkeypatch.setattr(hackloop, "send", send)
    db.ingest_mail(
        mail_id=123,
        sender="customer@hackloop.mail",
        recipient="team@hackloop.mail",
        subject="Request",
        body=body,
        category="rfq",
    )
    db.set_request(123, request(lang).model_dump())
    hackloop.send_customer(
        "customer@hackloop.mail",
        shipment_subject("DUS", "SYD", lang, ""),
        body,
        in_reply_to=123,
        session_id=123,
        review=False,
    )
    assert sent[0]["in_reply_to"] == 123
    assert detect(sent[0]["body"].replace(SIGNATURE, "")) == lang
    from app.mail.tracking_notice import NOTICES

    assert NOTICES[(lang, False)] in sent[0]["body"]
    assert NOTICES[("en", False)] not in sent[0]["body"]
    assert "translate_customer_email" not in calls  # verified copy: no translation at send
