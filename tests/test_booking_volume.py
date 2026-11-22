"""The chart is a true booking-time window even when an older RFQ is booked today."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import db
from app.ui import server


def test_booking_time_and_agreed_customer_value_define_24h_chart(tmp_path):
    db.init_db(str(tmp_path / "volume.db"))
    stamp = datetime.now(UTC)
    for mid, currency, state in (
        (1, "EUR", "confirmed_sent"),
        (2, "USD", "price_accepted"),
        (3, "EUR", "price_change_asked"),
        (4, "EUR", "confirmed_sent"),
    ):
        sid = db.ingest_mail(
            mail_id=mid,
            sender=f"customer{mid}@example.com",
            recipient="team@example.com",
            subject="Freight",
            body="Quote",
            category="rfq",
        )
        qid = db.record_quote(
            sid,
            total_minor=100_000,
            currency=currency,
            airline="Airline",
            origin="FRA",
            destination="DEL",
            transit_days=3,
            source="portal",
        )
        db.record_booking(
            sid,
            booking_reference=f"BK-{mid}",
            rate_id=None,
            airline="Airline",
            price_minor=20_000,
            currency="EUR",
            status="confirmed",
        )
        db.reply_bind_booking(sid, qid)
        db.set_booking_customer_state(sid, state, 120_000)
        with db.tx() as c:
            c.execute(
                "UPDATE sessions SET created_at=? WHERE id=?",
                ((stamp - timedelta(days=10)).isoformat(), sid),
            )
            if mid == 4:
                c.execute(
                    "UPDATE bookings SET created_at=? WHERE session_id=?",
                    ((stamp - timedelta(hours=25)).isoformat(), sid),
                )
    with TestClient(server.app) as client:
        data = client.get("/api/booking-volume").json()
    assert datetime.fromisoformat(data["to"]) - datetime.fromisoformat(data["from"]) == timedelta(
        hours=24
    )
    assert data["amounts"] == [
        {"currency": "EUR", "minor": 120_000},
        {"currency": "USD", "minor": 120_000},
    ]
    assert len(data["events"]) == 2
    assert db.business_metrics(1)["requests"] == 0


def test_bookings_without_customer_amount_report_airline_cost_separately(tmp_path):
    """A booking backfilled from the board has an airline price but no quote or customer total."""
    db.init_db(str(tmp_path / "airline.db"))
    sid = db.ingest_mail(
        mail_id=1,
        sender="customer@example.com",
        recipient="team@example.com",
        subject="Freight",
        body="Quote",
        category="rfq",
    )
    db.record_booking(
        sid,
        booking_reference="BK-1",
        rate_id=None,
        airline="Airline",
        price_minor=369_600,
        currency="EUR",
        status="confirmed",
    )
    volume = db.booking_volume_24h()
    assert volume["events"] == [] and volume["amounts"] == []
    assert volume["airline_amounts"] == [{"currency": "EUR", "minor": 369_600}]
    metrics = db.business_metrics(1)
    assert metrics["amounts"]["booked"] == []
    assert metrics["amounts"]["booked_airline"] == [{"currency": "EUR", "minor": 369_600}]
    assert metrics["unknown_booked_value"] == 1
