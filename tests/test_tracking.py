"""Tracking agent: find the booking a question is about, answer from the CRM."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import db
from app.agents import booking, tracking
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


def _booked(
    sid: int, customer: str, o: str, d: str, bk: str, confirmed: bool = True
) -> None:
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


@pytest.fixture()
def two_bookings():
    _booked(1223, "weidner-spielwaren@hackloop.mail", "STR", "EZE", "BK-6Z1GMPG3")
    _booked(731, "auer-maschinenbau@hackloop.mail", "DUS", "JFK", "BK-J1TGYUF2", False)


@pytest.mark.parametrize(
    ("text", "session"),
    [
        ("What is the status of booking BK-6Z1GMPG3?", 1223),
        ("Status for bk-j1tgyuf2 please", 731),
        ("Where is shipment AP-731?", 731),
        ("Where is the shipment of Weidner Spielwaren?", 1223),
        ("Status of the Auer Maschinenbau shipment DUS-JFK?", 731),
        ("Any news on the STR to EZE shipment?", 1223),
        ("Where is shipment BK-UNKNOWN1?", None),
    ],
)
def test_find(two_bookings, text: str, session: int | None) -> None:
    rec = tracking.find(text, db.tracking_records())
    assert (rec["session_id"] if rec else None) == session


def test_answer_from_crm(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    q = Mail(3000, 3000, TRACK, TEAM, "Shipment status", "Where is BK-6Z1GMPG3?", "")
    tracking.handle(q, 3000)
    a = api.sent[-1]
    assert a["to"] == TRACK and a["in_reply_to"] == 3000
    assert "BK-6Z1GMPG3" in a["body"] and "confirmed by the airline" in a["body"]
    assert "STR to EZE" in a["body"] and "Fjordline Air Cargo" in a["body"]


def test_unknown_asks_for_reference(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    tracking.handle(
        Mail(3001, 3001, TRACK, TEAM, "Status", "Where is my cargo?", ""), 3001
    )
    assert "booking reference" in api.sent[-1]["body"]


def test_customer_status_question_in_booked_thread(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    now = datetime.now(UTC).isoformat()
    thanks = (
        "Thank you for confirming the booking. Please keep me informed of any updates."
    )
    for mid, body in ((1446, thanks), (1447, "When will it arrive in JFK?")):
        db.ingest_mail(
            mail_id=mid,
            sender="auer-maschinenbau@hackloop.mail",
            recipient=TEAM,
            subject="Re: Quote DUS to JFK",
            body=body,
            category="customer",
            received_at=now,
            in_reply_to=731,
            references=[731],
        )
        booking.handle_reply(
            Mail(
                mid,
                731,
                "auer-maschinenbau@hackloop.mail",
                TEAM,
                "Re: Quote DUS to JFK",
                body,
                now,
                references=[731],
            ),
            731,
        )
    assert len(api.sent) == 1  # the thank-you gets no reply, the question does
    assert "BK-J1TGYUF2" in api.sent[0]["body"] and api.sent[0]["in_reply_to"] == 1447


def test_weight_separates_and_ties_ask(tmp_path) -> None:
    # same customer, same lane, two shipments: only the weight tells them apart
    _booked(1, "weidner-spielwaren@hackloop.mail", "STR", "EZE", "BK-AAAAAAAA")
    _booked(2, "weidner-spielwaren@hackloop.mail", "STR", "EZE", "BK-BBBBBBBB")
    db.set_request(2, {"destination": "EZE", "weight_kg": 500})
    q = "Status of our shipment: Weidner Spielwaren GmbH, STR to EZE, 1228 kg?"
    assert tracking.find(q, db.tracking_records())["session_id"] == 1
    assert (
        tracking.find(q.replace("1228", "500"), db.tracking_records())["session_id"]
        == 2
    )
    tie = "Where is the Weidner Spielwaren shipment STR to EZE?"
    assert tracking.find(tie, db.tracking_records()) is None


@pytest.mark.parametrize(
    ("text", "kg"),
    [
        ("1228 kg", 1228),
        ("1,228 kg", 1228),
        ("1.228 kg", 1228),
        ("12,5 kg", 12.5),
        ("none", None),
    ],
)
def test_weight_parse(text, kg) -> None:
    assert tracking._weight(text) == kg
