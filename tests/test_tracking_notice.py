"""Tracking instructions survive generation and a missing notice is repaired once."""

import time

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


def test_uncertain_transport_gets_one_final_notice_and_then_stops(monkeypatch, store):
    session(booked=True)
    attempts = []
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def uncertain(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("delivery may have happened")

    monkeypatch.setattr(hackloop, "send", uncertain)
    assert not tracking_notice.repair(SID)
    db.init_db(store)  # the bounded retry survives a process restart
    assert not tracking_notice.repair(SID)
    assert len(attempts) == 1
    clock[0] += tracking_notice.settings.tracking_notice_check_every_s + 1
    assert not tracking_notice.repair(SID)
    clock[0] += 120
    assert not tracking_notice.repair(SID)
    assert len(attempts) == 2
    assert "final tracking note" in attempts[-1]["body"]
    assert any(e["kind"] == "tracking_mail_unknown" for e in db.get_session(SID)["events"])


def test_quote_tracking_paragraph_does_not_replace_separate_booking_mail(monkeypatch):
    from app.mail.tracking_notice import NOTICES

    session(booked=True, body=NOTICES[("en", False)])
    sent = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"id": 550, "subject": kwargs["subject"], "body": kwargs["body"]}

    monkeypatch.setattr(hackloop, "send", send)
    assert tracking_notice.repair(SID)
    assert not tracking_notice.repair(SID)
    assert len(sent) == 1 and "BK-ABC123" in sent[0]["body"]
    assert "FRA to DEL" in sent[0]["body"]
    assert db.get_session(SID)["mails"][-1]["category"] == "tracking_notice"


def test_booking_notice_is_recovered_after_status_change_and_old_prequote_claim(monkeypatch):
    session(booked=True)
    db.kv_set(f"tracking.notice.recovery.{SID}", "sent:501")
    db.set_booking_customer_state(SID, "price_accepted")
    db.set_status(SID, "answered")
    sent = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: sent.append(kw) or {"id": 551, **kw})
    assert SID in db.tracking_notice_candidates()
    assert tracking_notice.repair(SID)
    assert len(sent) == 1


def test_known_receipt_survives_local_recording_failure(monkeypatch):
    session(booked=True)
    sent = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: sent.append(kw) or {"id": 552, **kw})
    with monkeypatch.context() as patch:

        def failed_record(**kwargs):
            raise RuntimeError("local record unavailable")

        patch.setattr(db, "record_outgoing", failed_record)
        with pytest.raises(RuntimeError):
            tracking_notice.repair(SID)
    assert not tracking_notice.repair(SID)
    assert len(sent) == 1
