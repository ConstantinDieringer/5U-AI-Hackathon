"""Tracking agent: find the booking a question is about, answer from the CRM."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app import db, hackloop
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
    monkeypatch.setattr(hackloop, "send", api.send)
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
    monkeypatch.setattr(hackloop, "send", api.send)
    tracking.handle(Mail(3001, 3001, TRACK, TEAM, "Status", "Where is my cargo?", ""), 3001)
    assert "booking reference" in api.sent[-1]["body"]


def test_dropped_tracking_request_is_recovered_once(two_bookings, monkeypatch):
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    stamp = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
    db.ingest_mail(
        mail_id=3300,
        sender=TRACK,
        recipient=TEAM,
        subject="Status",
        body="Where is BK-6Z1GMPG3?",
        category="tracking",
        received_at=stamp,
    )
    tracking.recover_pending()
    tracking.recover_pending()
    assert len(api.sent) == 1 and api.sent[0]["in_reply_to"] == 3300
    assert "BK-6Z1GMPG3" in api.sent[0]["body"]
    assert db.get_session(3300)["status"] == "answered"


def test_rate_limited_tracking_reply_retries_without_losing_request(two_bookings, monkeypatch):
    api = Api()
    failures = []

    def send(**kwargs):
        if not failures:
            failures.append(True)
            raise hackloop.HackloopError("POST", "/v1/mailbox/messages", 429, "rate limited")
        return api.send(**kwargs)

    monkeypatch.setattr(hackloop, "send", send)
    stamp = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
    db.ingest_mail(
        mail_id=3301,
        sender=TRACK,
        recipient=TEAM,
        subject="Status",
        body="Where is BK-6Z1GMPG3?",
        category="tracking",
        received_at=stamp,
    )
    tracking.recover_pending()
    assert api.sent == []
    tracking.recover_pending()
    tracking.recover_pending()
    assert len(api.sent) == 1


def test_reference_only_followup_stays_in_tracking(two_bookings, monkeypatch):
    from app.mail import poller
    from app.models import Category

    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _tracking_session(3302, "Status", "Where is my cargo?")
    root = Mail(3302, 3302, TRACK, TEAM, "Status", "Where is my cargo?", "")
    tracking.handle(root, 3302)
    db.ingest_mail(
        mail_id=3303,
        sender=TRACK,
        recipient=TEAM,
        subject="Re: Status",
        body="BK-6Z1GMPG3",
        category="customer",
        references=[3302],
        received_at=datetime.now(UTC).isoformat(),
    )
    followup = Mail(3303, 3302, TRACK, TEAM, "Re: Status", "BK-6Z1GMPG3", "")

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(poller.threading, "Thread", InlineThread)
    poller.dispatch(followup, Category.CUSTOMER, 3302)
    assert len(api.sent) == 2 and api.sent[-1]["in_reply_to"] == 3303
    assert "confirmed by Fjordline Air Cargo" in api.sent[-1]["body"]


def test_answering_customer_tracking_preserves_booking_status(two_bookings, monkeypatch):
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    before = db.get_session(1223)["status"]
    mail = Mail(
        3304,
        1223,
        "weidner-spielwaren@hackloop.mail",
        TEAM,
        "Tracking",
        "Where is BK-6Z1GMPG3?",
        "",
    )
    tracking.handle(mail, 1223)
    assert db.get_session(1223)["status"] == before
    tracking.handle(mail, 1223)
    assert len(api.sent) == 1


# ---------------------------------------------------------------- who may see which booking


@pytest.mark.core
def test_customer_cannot_read_another_customers_booking(two_bookings, monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    other = "auer-maschinenbau@hackloop.mail"
    _tracking_session(3500, "Status", "Where is BK-6Z1GMPG3?")
    tracking.handle(Mail(3500, 3500, other, TEAM, "Status", "Where is BK-6Z1GMPG3?", ""), 3500)
    body = api.sent[-1]["body"]
    assert "Fjordline" not in body and "EZE" not in body and "4.408" not in body


def test_customer_reads_own_booking(two_bookings, monkeypatch) -> None:
    owner = "weidner-spielwaren@hackloop.mail"
    records = db.tracking_records()
    assert tracking.find(
        "Where is BK-6Z1GMPG3?", [r for r in records if tracking.visible_to(owner, r)]
    )
    assert tracking.visible_to(TRACK, records[0]) and tracking.visible_to(TRACK, records[1])


def test_failed_booking_is_not_reported_as_booked() -> None:
    _booked(1500, "weidner-spielwaren@hackloop.mail", "MUC", "ORD", "BK-FAILED01", False)
    db.update_booking_status("BK-FAILED01", "failed")
    assert all(r["booking_reference"] != "BK-FAILED01" for r in db.tracking_records())


def test_question_naming_another_customer_never_gets_their_shipment():
    """Route and weight fit Weidner's booking, but the question is about Hofmann: no data of
    another customer, ask for the reference instead."""
    _booked(1223, "weidner-spielwaren@hackloop.mail", "STR", "EZE", "BK-6Z1GMPG3")
    records = db.tracking_records()
    leak = "Where is our 1228 kg shipment STR to EZE? Customer: Hofmann Textilwerk"
    assert tracking.find(leak, records) is None
    own = "Where is our 1228 kg shipment STR to EZE? Customer: Weidner Spielwaren"
    assert tracking.find(own, records)["booking_reference"] == "BK-6Z1GMPG3"


@pytest.mark.core
def test_tracking_reports_the_all_in_price_not_the_portal_air_rate(monkeypatch) -> None:
    """Rounds 27/29/45/46/51/52 earned 5 of 10 tracking points. A portal booking stores the
    portal's bare air rate (here USD, without trucking) as the booking price, and the
    tracking answer called that "the all-in price". The customer agreed to 4.408,52 €."""
    _booked(1223, "weidner-spielwaren@hackloop.mail", "STR", "EZE", "BK-6Z1GMPG3")
    db.conn().execute(
        "UPDATE bookings SET price_minor = 391000, currency = 'USD' WHERE session_id = 1223"
    )
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    monkeypatch.setattr(tracking, "_today", lambda: date(2026, 11, 21))
    _tracking_session(3000, "Shipment status", "Where is BK-6Z1GMPG3?")
    tracking.handle(
        Mail(3000, 3000, TRACK, TEAM, "Shipment status", "Where is BK-6Z1GMPG3?", ""), 3000
    )
    body = api.sent[-1]["body"]
    assert "4.408,52 €" in body and "3.910" not in body and "$" not in body


@pytest.mark.core
def test_tracking_reports_a_customer_accepted_new_total(monkeypatch) -> None:
    """Counterexample: after an airline price change the customer accepted a new total; that
    total, not the original quote, is what we report."""
    _booked(1223, "weidner-spielwaren@hackloop.mail", "STR", "EZE", "BK-6Z1GMPG3")
    db.set_booking_customer_state(1223, "confirmed_sent", 484937)
    record = next(r for r in db.tracking_records() if r["session_id"] == 1223)
    assert record["price_minor"] == 484937 and record["currency"] == "EUR"
