"""RFQ attachment evidence and customer clarification tests."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import openpyxl
import pytest

from app import db
from app.agents import rfq
from app.mail import attachments
from app.models import Mail, Rfq


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path) -> None:
    db.init_db(str(tmp_path / "test.db"))


def request() -> Rfq:
    return Rfq(
        pickup_zip="21663",
        destination="YYZ",
        weight_kg=855,
        volume_cbm=3.74,
        pieces=4,
    )


def test_xlsx_dangerous_goods_and_weight_conflicts_are_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    target = inbox / "2530"
    target.mkdir(parents=True)
    path = target / "7-shipment.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Dangerous Goods", "Yes"])
    sheet.append(["UN Number", "UN1133"])
    sheet.append(["Class", "3"])
    sheet.append(["Total Gross Weight (kg)", "900"])
    sheet.append(["Destination Airport", "YYZ"])
    sheet.append(["Shipper", "Example GmbH, Erlangen, Germany"])
    book.save(path)
    monkeypatch.setattr(
        attachments,
        "settings",
        dataclasses.replace(attachments.settings, inbox_dir=inbox),
    )
    mail = Mail(
        id=2530,
        thread_root=2530,
        sender="customer@example.com",
        to="team@example.com",
        subject="RFQ",
        body="Pickup in 21663 Lueneburg. This is non dangerous cargo, 855 kg to YYZ.",
        received_at="2026-11-21T12:00:00+00:00",
        attachments=[{"id": 7, "filename": "shipment.xlsx"}],
    )

    facts, conflicts = attachments.analyze(mail, request(), 2530)

    assert facts[0].un_numbers == ["1133"]
    assert facts[0].gross_weight_kg == 900
    assert {item.field for item in conflicts} == {
        "dangerous_goods",
        "weight_kg",
        "pickup_location",
    }


def test_clarification_claim_is_idempotent_and_visible() -> None:
    sid = db.ingest_mail(
        mail_id=10,
        sender="customer@example.com",
        recipient="team@example.com",
        subject="RFQ",
        body="quote please",
        category="rfq",
    )
    conflict = {
        "field": "dangerous_goods",
        "mail_value": False,
        "attachment_value": True,
        "filename": "shipment.xlsx",
        "reason": "conflict",
    }

    first, first_created = db.ensure_clarification(sid, [conflict])
    second, second_created = db.ensure_clarification(sid, [conflict])

    assert first_created is True and second_created is False
    assert first["id"] == second["id"]
    assert db.list_sessions()[0]["needs_clarification"] == 1
    assert db.get_session(sid)["clarifications"][0]["state"] == "pending"


def test_clear_non_dangerous_reply_can_resume_same_request() -> None:
    conflict = {
        "field": "dangerous_goods",
        "mail_value": False,
        "attachment_value": True,
        "filename": "shipment.xlsx",
        "reason": "conflict",
    }
    reply = Mail(
        id=11,
        thread_root=10,
        sender="customer@example.com",
        to="team@example.com",
        subject="Re: RFQ",
        body="Confirmed: this shipment is non dangerous. The spreadsheet is outdated.",
        received_at="2026-11-21T12:01:00+00:00",
    )

    choices = rfq._resolution_from_reply(reply, 10, [conflict])

    assert choices == {"0": "mail"}
    assert rfq._apply_resolutions(request(), [conflict], choices) == request()


def test_explicit_origin_disagreement_requires_clarification() -> None:
    request_with_origin = Rfq(
        origin="EDDL",
        pickup_zip="51896",
        destination="GRU",
        weight_kg=100,
    )
    mail = Mail(
        id=937,
        thread_root=937,
        sender="customer@example.com",
        to="team@example.com",
        subject="RFQ EDDL-GRU",
        body="Pickup from 51896 and departure airport EDDL.",
        received_at="2026-11-21T12:00:00+00:00",
    )

    conflict = rfq._explicit_origin_conflict(request_with_origin, mail)

    assert conflict is not None
    assert conflict["mail_value"] == "EDDL"
    assert conflict["attachment_value"] == "CGN"


def test_clarification_mail_is_sent_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = db.ingest_mail(
        mail_id=20,
        sender="customer@example.com",
        recipient="team@example.com",
        subject="RFQ",
        body="Non dangerous cargo to YYZ",
        category="rfq",
    )
    mail = Mail(
        id=20,
        thread_root=20,
        sender="customer@example.com",
        to="team@example.com",
        subject="RFQ",
        body="Non dangerous cargo to YYZ",
        received_at="2026-11-21T12:00:00+00:00",
    )
    sent: list[dict] = []

    def send(to, subject, body, in_reply_to=None):
        result = {
            "id": 21,
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
        }
        sent.append(result)
        return result

    monkeypatch.setattr(rfq.hackloop, "send", send)
    conflict = {
        "field": "dangerous_goods",
        "mail_value": False,
        "attachment_value": True,
        "filename": "shipment.xlsx",
        "reason": "conflict",
    }

    rfq._ask_clarification(mail, request(), sid, [conflict])
    rfq._ask_clarification(mail, request(), sid, [conflict])

    assert len(sent) == 1
    session = db.get_session(sid)
    assert session["clarifications"][0]["question_mail_id"] == 21
    assert [item["category"] for item in session["mails"]] == ["rfq", "clarification"]
