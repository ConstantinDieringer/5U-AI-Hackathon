"""Tracking agent: find the booking a question is about, answer from the CRM."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app import db
from app.agents import tracking
from app.mail.style import SIGNATURE
from app.models import Mail

TEAM = "austrian-power@hackloop.mail"
TRACK = "tracking@hackloop.mail"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
    yield


class Api:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, to, subject, body, in_reply_to=None, references=None):
        m = {
            "id": 9000 + len(self.sent),
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
            "references": [],
        }
        self.sent.append(m)
        return m


def _booked(sid: int, customer: str, o: str, d: str, bk: str, confirmed: bool = True) -> None:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=sid,
        sender=customer,
        recipient=TEAM,
        subject=f"Quote {o} to {d}",
        body="quote please",
        category="rfq",
        received_at=now,
    )
    db.set_request(
        sid,
        {
            "destination": d,
            "weight_kg": 1228,
            "volume_cbm": 5.37,
            "ready_date": "2026-11-25",
            "pickup_zip": "70173",
        },
    )
    db.record_quote(
        sid,
        total_minor=440852,
        currency="EUR",
        airline="Fjordline Air Cargo",
        origin=o,
        destination=d,
        transit_days=4,
        source="portal",
        rate_id=f"R-rate{sid}rate{sid}",
    )
    db.record_booking(
        sid,
        booking_reference=bk,
        rate_id=f"R-rate{sid}rate{sid}",
        airline="Fjordline Air Cargo",
        price_minor=440852,
        currency="EUR",
    )
    if confirmed:
        db.update_booking_status(bk, "confirmed")


def _tracking_session(mid: int, subject: str, body: str) -> None:
    db.ingest_mail(mail_id=mid, sender=TRACK, recipient=TEAM, subject=subject, body=body,
                   category="tracking", received_at=datetime.now(UTC).isoformat())  # fmt: skip


@pytest.fixture()
def two_bookings():
    _booked(1223, "weidner-spielwaren@hackloop.mail", "STR", "EZE", "BK-6Z1GMPG3")
    _booked(731, "auer-maschinenbau@hackloop.mail", "DUS", "JFK", "BK-J1TGYUF2", False)


@pytest.mark.core
def test_answer_from_crm(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    monkeypatch.setattr(tracking, "_today", lambda: date(2026, 11, 21))
    _tracking_session(3000, "Shipment status", "Where is BK-6Z1GMPG3?")
    q = Mail(3000, 3000, TRACK, TEAM, "Shipment status", "Where is BK-6Z1GMPG3?", "")
    tracking.handle(q, 3000)
    a = api.sent[-1]
    assert a["to"] == TRACK and a["in_reply_to"] == 3000
    body = a["body"]
    assert "BK-6Z1GMPG3" in body and "confirmed by Fjordline Air Cargo" in body
    assert "STR to EZE" in body and "1228 kg" in body and "4.408,52 €" in body
    # door pickup in 70173, ready 25 Nov: still at the shipper, trucked, departs a day later
    assert "still at your site in postal area 70173" in body and "25 November 2026" in body
    assert "depart STR on 26 November 2026 and arrive in EZE on 30 November 2026" in body
    assert "- " not in body and body.startswith("Hello,")  # prose, neutral greeting
    assert body.rstrip().endswith(SIGNATURE)
    assert db.get_session(3000)["status"] == "answered"


# ---------------------------------------------------------------- real stored mails


@pytest.mark.core
def test_unknown_asks_for_reference(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    tracking.handle(Mail(3001, 3001, TRACK, TEAM, "Status", "Where is my cargo?", ""), 3001)
    assert "booking reference" in api.sent[-1]["body"]
