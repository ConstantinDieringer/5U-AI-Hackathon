"""Financial dashboard totals must describe orders, not mixed airline costs."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.ui import server


@pytest.fixture(autouse=True)
def storage(tmp_path):
    db.init_db(str(tmp_path / "business.db"))


def request(mid, currency="EUR", total=100_000):
    sid = db.ingest_mail(
        mail_id=mid,
        sender=f"customer{mid}@example.com",
        recipient="team@example.com",
        subject="FRA to DEL",
        body="A quote please",
        category="rfq",
    )
    db.set_request(sid, {"origin": "FRA", "destination": "DEL", "weight_kg": 100})
    qid = db.record_quote(
        sid,
        total_minor=total,
        currency=currency,
        airline="Airline",
        origin="FRA",
        destination="DEL",
        transit_days=3,
        source="portal",
    )
    return sid, qid


def book(sid, qid, state="confirmed_sent", total=130_000):
    db.record_booking(
        sid,
        booking_reference=f"BK-{sid}",
        rate_id=None,
        airline="Airline",
        price_minor=70_000,
        currency="USD",
        status="confirmed",
    )
    db.reply_bind_booking(sid, qid)
    db.set_booking_customer_state(sid, state, total)


def amounts(data, kind):
    return {r["currency"]: r["minor"] for r in data["amounts"][kind]}


def test_customer_totals_versions_pending_and_foreign_currency():
    sid, qid = request(1)
    book(sid, qid)
    # Later unrelated quote must not inflate or change the bound booking value.
    db.record_quote(
        sid,
        total_minor=999_999,
        currency="EUR",
        airline="Other",
        origin="FRA",
        destination="DEL",
        transit_days=2,
        source="manual",
    )
    usd, uq = request(2, "USD", 50_000)
    book(usd, uq, total=60_000)
    pending, pq = request(3)
    book(pending, pq, state="price_change_asked", total=150_000)
    failed, fq = request(4)
    book(failed, fq)
    db.update_booking_status(f"BK-{failed}", "failed")
    open_sid, _ = request(5, total=20_000)
    db.set_status(open_sid, "quoted")
    data = db.business_metrics()
    assert data["requests"] == data["quoted"] == 5
    assert data["booked"] == 2 and data["awaiting_price"] == 1
    assert amounts(data, "booked") == {"EUR": 130_000, "USD": 60_000}
    assert amounts(data, "offered")["EUR"] == 1_219_999
    assert amounts(data, "pipeline") == {"EUR": 20_000}
    assert data["booked_weight_kg"] == 200


def test_cohort_dates_not_limited_to_latest_crm_rows():
    for mid in range(1, 205):
        sid, _ = request(mid, total=100)
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with db.tx() as c:
        c.execute("UPDATE sessions SET created_at=? WHERE id=?", (old, sid))
    assert len(db.crm()["quotes"]) == 200
    assert db.business_metrics()["requests"] == 204
    assert db.business_metrics(7)["requests"] == 203
    assert amounts(db.business_metrics(7), "offered") == {"EUR": 20_300}


def test_empty_unknown_values_and_api_validation():
    with TestClient(server.app) as client:
        data = client.get("/api/business-metrics").json()
        assert data["requests"] == 0 and data["avg_response_s"] is None
        assert data["amounts"]["booked"] == []
        assert client.get("/api/business-metrics?days=-1").status_code == 422
        assert client.get("/api/business-metrics?days=8").status_code == 422
    sid = db.ingest_mail(
        mail_id=99,
        sender="legacy@example.com",
        recipient="team@example.com",
        subject="Legacy booking",
        body="Book",
        category="rfq",
    )
    db.record_booking(
        sid,
        booking_reference="BK-legacy",
        rate_id=None,
        airline="Airline",
        price_minor=7000,
        currency="EUR",
        status="confirmed",
    )
    data = db.business_metrics()
    assert data["booked"] == data["unknown_booked_value"] == 1
    assert amounts(data, "booked") == {}  # No treating airline cost as customer revenue.
