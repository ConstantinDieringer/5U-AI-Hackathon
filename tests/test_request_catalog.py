"""The request tab must cover the full mailbox with stable server-side controls."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.ui import server


@pytest.fixture(autouse=True)
def storage(tmp_path):
    db.init_db(str(tmp_path / "catalog.db"))


def seed(count):
    for mid in range(1, count + 1):
        sid = db.ingest_mail(
            mail_id=mid,
            sender=f"customer{mid}@example.com",
            recipient="team@example.com",
            subject=f"Shipment {mid}",
            body="Quote please",
            category="rfq",
        )
        db.set_request(
            sid, {"origin": "FRA", "destination": "DEL" if mid % 2 else "JFK", "weight_kg": mid}
        )
        if mid % 2:
            db.set_status(sid, "quoted")


def test_every_request_is_reachable_beyond_legacy_limit():
    seed(258)
    with TestClient(server.app) as client:
        ids = []
        for offset in range(0, 258, 50):
            data = client.get(f"/api/requests?sort=id&direction=asc&offset={offset}").json()
            assert data["total"] == 258
            ids.extend(row["id"] for row in data["items"])
        assert ids == list(range(1, 259))
        data = client.get(
            "/api/requests?airport=del&status=quoted&sort=weight_kg&direction=desc"
        ).json()
        assert data["total"] == 129
        assert data["items"][0]["weight_kg"] == 257
        assert all(row["destination"] == "DEL" for row in data["items"])


def test_filters_search_empty_results_and_sort_validation():
    seed(3)
    with db.tx() as c:
        c.execute(
            "UPDATE sessions SET created_at=? WHERE id=1",
            ((datetime.now(UTC) - timedelta(days=10)).isoformat(),),
        )
    with TestClient(server.app) as client:
        assert client.get("/api/requests?days=7").json()["total"] == 2
        assert client.get("/api/requests?q=customer3").json()["items"][0]["id"] == 3
        assert client.get("/api/requests?q=%233").json()["total"] == 1
        assert client.get("/api/requests?q=missing").json()["items"] == []
        assert client.get("/api/requests?kind=tracking").json()["total"] == 0
        for params in (
            "sort=DROP%20TABLE",
            "direction=sideways",
            "days=-1",
            "offset=-1",
            "limit=101",
            "kind=invalid",
        ):
            assert client.get("/api/requests?" + params).status_code == 422
