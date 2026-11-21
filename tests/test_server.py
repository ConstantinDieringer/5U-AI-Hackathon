"""API tests for app/ui/server.py (no 5U calls, no streaming loop)."""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.ui import server

TEAM = "austrian-power@hackloop.mail"


@pytest.fixture()
def client(tmp_path):
    db.init_db(str(tmp_path / "api.db"))
    with TestClient(server.app) as c:
        yield c


def seed():
    return db.ingest_mail(
        mail_id=1,
        sender="a@b.example",
        recipient=TEAM,
        subject="RFQ FRA-DFW",
        body="<script>alert(1)</script>",
        category="rfq",
    )


def test_index_and_lists(client):
    sid = seed()
    assert client.get("/").status_code == 200
    rows = client.get("/api/sessions").json()
    assert [r["id"] for r in rows] == [sid]
    assert client.get("/api/sessions", params={"q": "nomatch"}).json() == []
    assert client.get(f"/api/sessions/{sid}").json()["mails"][0]["body"].startswith("<script>")
    assert client.get("/api/sessions/999").status_code == 404


def test_actions_and_events(client):
    sid = seed()
    for path in ("pause", "resume", "takeover", "handback"):
        assert client.post(f"/api/sessions/{sid}/{path}").json() == {"ok": True}
    assert client.post("/api/pause-all").json() == {"ok": True}
    assert client.get("/api/stats").json()["global_pause"] is True
    client.post("/api/resume-all")
    kinds = [e["kind"] for e in client.get("/api/events", params={"since": 0}).json()]
    assert kinds[0] == "mail_in" and kinds.count("human_action") == 6
    first = client.get("/api/events", params={"since": 0}).json()[0]["id"]
    assert all(e["id"] > first for e in client.get("/api/events", params={"since": first}).json())


def test_dashboard_shape(client):
    seed()
    d = client.get("/api/dashboard").json()
    assert {
        "stats",
        "quotes",
        "bookings",
        "hourly",
        "llm_total_micro_usd",
        "unassigned",
    } <= d.keys()


def test_reply_without_mail_client_is_503(client):
    sid = seed()
    r = client.post(f"/api/sessions/{sid}/reply", json={"text": "Hallo"})
    assert r.status_code in (503, 502)


def test_basic_auth_when_password_set(client, monkeypatch):
    import base64
    import dataclasses

    monkeypatch.setattr(
        server, "settings", dataclasses.replace(server.settings, ui_user="team", ui_password="pw")
    )
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/health").status_code == 200
    good = base64.b64encode(b"team:pw").decode()
    bad = base64.b64encode(b"team:nope").decode()
    assert client.get("/api/stats", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert client.get("/api/stats", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_crm_won_lost(client):
    sid = seed()
    db.record_quote(
        sid,
        total_minor=10000,
        currency="EUR",
        airline="X",
        origin="FRA",
        destination="DFW",
        transit_days=2,
        source="sheet",
    )
    db.record_booking(
        sid,
        booking_reference="BK-1",
        rate_id="R-12345678",
        airline="X",
        price_minor=10000,
        currency="EUR",
    )
    lost = db.ingest_mail(
        mail_id=2, sender="c@d.example", recipient=TEAM, subject="RFQ 2", body="b", category="rfq"
    )
    db.set_status(lost, "expired")
    d = client.get("/api/crm").json()
    assert d["totals"]["won"] == 1 and d["totals"]["lost"] == 1 and d["booked_minor"] == 10000
    a = next(c for c in d["customers"] if c["customer"] == "a@b.example")
    assert a["won"] == 1 and a["booked_minor"] == 10000
    assert d["quotes"][0]["outcome"] == "won"
    assert client.get("/api/customers/a@b.example/sessions").json()[0]["id"] == sid
