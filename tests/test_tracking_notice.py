"""Tracking instructions survive generation and a missing notice is repaired once."""

import pytest

from app import db, hackloop
from app.agents import tracking_notice

CUSTOMER = "sara@example.test"
TEAM = "austrian-power@hackloop.mail"
SID = 500


@pytest.fixture(autouse=True)
def store(tmp_path):
    path = str(tmp_path / "notice.db")
    db.init_db(path)
    return path


def session(*, booked=False, body="Our quote is 100 €."):
    db.ingest_mail(
        mail_id=SID,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Your shipment",
        body="Please quote FRA to DEL",
        category="rfq",
    )
    db.record_outgoing(
        mail_id=501,
        recipient=CUSTOMER,
        subject="Your shipment",
        body=body,
        category="quote",
        session_id=SID,
        in_reply_to=SID,
    )
    db.record_quote(
        SID,
        total_minor=10000,
        currency="EUR",
        airline="Test Air",
        origin="FRA",
        destination="DEL",
        transit_days=2,
        source="sheet",
        mail_id=501,
    )
    if booked:
        db.record_booking(
            SID,
            booking_reference="BK-ABC123",
            rate_id=None,
            airline="Test Air",
            price_minor=10000,
            currency="EUR",
            status="confirmed",
        )


def test_uncertain_transport_or_restart_never_duplicates_a_notice(monkeypatch):
    session(booked=True)
    attempts = []

    def uncertain(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("delivery may have happened")

    monkeypatch.setattr(hackloop, "send", uncertain)
    assert not tracking_notice.repair(SID)
    assert not tracking_notice.repair(SID)
    assert len(attempts) == 1
    assert db.kv_get(f"tracking.notice.recovery.{SID}") == "unknown"
    assert any(e["kind"] == "tracking_notice_unknown" for e in db.get_session(SID)["events"])
