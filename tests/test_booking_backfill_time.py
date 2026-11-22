"""Bookings recreated from airline confirmations keep the confirmation's time, not the restart."""

from app import db

CONFIRMED_AT = "2026-11-21T16:08:12+00:00"


def _setup(tmp_path) -> int:
    db.init_db(str(tmp_path / "backfill.db"))
    sid = db.ingest_mail(
        mail_id=1,
        sender="buyer@example.com",
        recipient="team@example.com",
        subject="Air freight DUS to JFK",
        body="Quote please",
        category="rfq",
        received_at="2026-11-21T15:13:13+00:00",
    )
    db.ingest_mail(
        mail_id=2,
        sender="desk@airline.example",
        recipient="team@example.com",
        subject="Booking BK-J1TGYUF2 confirmed",
        body=(
            f"Booking reference: BK-J1TGYUF2\nYour reference: AP-{sid}\n"
            "Airline: Fjordline Air Cargo\nLane: DUS-JFK\nPrice: 3,696.00 EUR\n"
        ),
        category="booking",
        received_at=CONFIRMED_AT,
    )
    return sid


def _booked_at(sid: int) -> str:
    row = (
        db.conn().execute("SELECT created_at FROM bookings WHERE session_id = ?", (sid,)).fetchone()
    )
    return row["created_at"]


def test_backfill_uses_the_confirmation_time(tmp_path):
    sid = _setup(tmp_path)
    db.conn().execute("DELETE FROM bookings WHERE session_id = ?", (sid,))
    assert db.backfill_bookings_from_confirmations() == 1
    assert _booked_at(sid) == CONFIRMED_AT


def test_an_earlier_backfill_stamped_at_restart_is_repaired(tmp_path):
    sid = _setup(tmp_path)
    # what the old backfill wrote: the booking stamped with the restart time
    db.record_booking(
        sid,
        booking_reference="BK-J1TGYUF2",
        rate_id=None,
        airline="Fjordline Air Cargo",
        price_minor=369_600,
        currency="EUR",
        status="confirmed",
        booked_at="2026-11-22T05:07:49+00:00",
    )
    assert db.backfill_bookings_from_confirmations() == 0  # nothing new, only the repair
    assert _booked_at(sid) == CONFIRMED_AT
    db.backfill_bookings_from_confirmations()
    assert _booked_at(sid) == CONFIRMED_AT  # idempotent
