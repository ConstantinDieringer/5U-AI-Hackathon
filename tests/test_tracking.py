"""Tracking agent: find the booking a question is about, answer from the CRM."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app import db
from app.agents import booking, tracking
from app.mail.signature import first_name
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
    assert "still at your site (70173)" in body and "2026-11-25" in body
    assert "depart STR on 26 Nov 2026 and arrive in EZE on 30 Nov 2026" in body
    assert "- " not in body and body.startswith("Hello,")  # prose, neutral greeting
    assert body.rstrip().endswith(SIGNATURE)
    assert db.get_session(3000)["status"] == "answered"


def test_answer_variants_and_german(two_bookings) -> None:
    rec = next(r for r in db.tracking_records() if r["session_id"] == 1223)
    today = date(2026, 11, 21)
    texts = {tracking.answer_text(rec, "en", "Klaus", v, today) for v in range(4)}
    assert len(texts) == 4 and all(t.startswith("Hi Klaus,") for t in texts)
    de = tracking.answer_text(rec, "de", None, 0, today)
    assert de.startswith("Guten Tag,") and "STR nach EZE" in de and "4.408,52 €" in de
    assert "Abflug ab STR am 26.11.2026" in de and "Ankunft in EZE am 30.11.2026" in de


@pytest.mark.parametrize(
    ("today", "phrase"),
    [
        (date(2026, 11, 25), "on its way to STR airport"),  # ready, truck to the airport
        (date(2026, 11, 27), "left STR on 26 Nov 2026 and is in transit to EZE"),
        (date(2026, 12, 2), "arrived in EZE on 30 Nov 2026"),
    ],
)
def test_where_follows_the_schedule(two_bookings, today, phrase) -> None:
    rec = next(r for r in db.tracking_records() if r["session_id"] == 1223)
    assert phrase in tracking.answer_text(rec, "en", None, 0, today)


@pytest.mark.parametrize(
    ("body", "name"),
    [
        ("Hello,\n\nWhere is it?\n\nCheers,\nKlaus Weidner\nWeidner Spielwaren GmbH", "Klaus"),
        (("Hi,\n\nStatus?\n\nMit freundlichen Gruessen / Kind regards\nJonas Kessler\n"
         "Logistics Coordinator"), "Jonas"),
        ("Hi,\n\nWhere is it now?\n\nThanks.\n\nMira Petersen\nNordlicht Audio", "Mira"),
        ("Hi,\n\nWhere is it now?\n\nBest regards,\nNordlicht Audio GmbH", None),
        ("Where is my cargo?", None),
    ],
)  # fmt: skip
def test_first_name_from_signature(body, name) -> None:
    assert first_name(body) == name


# ---------------------------------------------------------------- real stored mails

REAL_DB = Path(__file__).resolve().parent.parent / "data" / "forwarder.db"
REAL = {  # mail id -> booking reference, first name, departure, arrival (rfq.schedule)
    1677: ("BK-6Z1GMPG3", "Klaus", "26 Nov 2026", "30 Nov 2026"),
    1850: ("BK-L5RBV4FY", "Jonas", "26 Nov 2026", "1 Dec 2026"),
    2252: ("BK-DCA1A2QI", "Mira", "24 Nov 2026", "28 Nov 2026"),
}


@pytest.fixture()
def real_db(tmp_path):
    """A copy of the stored production snapshot, read with immutable=1 (never written)."""
    if not REAL_DB.exists():
        pytest.skip("data/forwarder.db not present")
    src = sqlite3.connect(f"file:{REAL_DB}?immutable=1", uri=True)
    copy = tmp_path / "real.db"
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    db.init_db(str(copy))


@pytest.mark.parametrize("mail_id", sorted(REAL))
def test_real_tracking_questions(real_db, monkeypatch, mail_id) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    monkeypatch.setattr(tracking, "_today", lambda: date(2026, 11, 21))  # day they came in
    row = next(m for m in db.get_session(mail_id)["mails"] if m["id"] == mail_id)
    mail = Mail(mail_id, mail_id, row["sender"], TEAM, row["subject"], row["body"],
                row["received_at"])  # fmt: skip
    tracking.handle(mail, mail_id)
    ref, name, dep, arr = REAL[mail_id]
    body = api.sent[-1]["body"]
    assert body.startswith(f"Hi {name},")
    assert ref in body
    assert "has not moved yet" in body and "still at your site" in body  # where it is now
    assert "depart" in body and dep in body and arr in body
    assert db.get_session(mail_id)["status"] == "answered"


def test_unknown_asks_for_reference(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    tracking.handle(Mail(3001, 3001, TRACK, TEAM, "Status", "Where is my cargo?", ""), 3001)
    assert "booking reference" in api.sent[-1]["body"]


def test_customer_status_question_in_booked_thread(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(tracking.hackloop, "send", api.send)
    now = datetime.now(UTC).isoformat()
    thanks = "Thank you for confirming the booking. Please keep me informed of any updates."
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
    assert tracking.find(q.replace("1228", "500"), db.tracking_records())["session_id"] == 2
    tie = "Where is the Weidner Spielwaren shipment STR to EZE?"
    assert tracking.find(tie, db.tracking_records()) is None


@pytest.mark.parametrize(
    ("text", "kg"),
    [("1228 kg", 1228), ("1,228 kg", 1228), ("1.228 kg", 1228), ("12,5 kg", 12.5), ("none", None)],
)
def test_weight_parse(text, kg) -> None:
    assert tracking._weight(text) == kg
