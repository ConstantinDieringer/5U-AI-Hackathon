"""API tests for app/ui/server.py (no 5U calls, no streaming loop)."""

from datetime import UTC

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
    health = client.get("/api/health").json()
    assert health["email_style_version"] == "strict-customer-prose-v3"
    assert health["email_reviewer_model"] == "gpt-5"
    assert "build_revision" in health
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


def test_reply_without_mail_client_is_503(client, monkeypatch):
    from app import hackloop

    def refused(*_a, **_k):  # was a real POST to the 5U mailbox (401 with a placeholder key)
        raise hackloop.HackloopError("POST", "/v1/mailbox/messages", 401, "Unauthorized")

    monkeypatch.setattr(hackloop, "send", refused)
    sid = seed()
    r = client.post(f"/api/sessions/{sid}/reply", json={"text": "Hallo"})
    assert r.status_code in (503, 502)


def test_basic_auth_when_password_set(client, monkeypatch):
    import base64
    import dataclasses

    monkeypatch.setattr(
        server,
        "settings",
        dataclasses.replace(server.settings, ui_user="team", ui_password="pw"),
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
        mail_id=2,
        sender="c@d.example",
        recipient=TEAM,
        subject="RFQ 2",
        body="b",
        category="rfq",
    )
    db.set_status(lost, "expired")
    d = client.get("/api/crm").json()
    assert d["totals"]["won"] == 1 and d["totals"]["lost"] == 1 and d["booked_minor"] == 10000
    a = next(c for c in d["customers"] if c["customer"] == "a@b.example")
    assert a["won"] == 1 and a["booked_minor"] == 10000
    assert d["quotes"][0]["outcome"] == "won"
    assert client.get("/api/customers/a@b.example/sessions").json()[0]["id"] == sid


def test_overview_and_attention(client):
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    old = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    sid = db.ingest_mail(
        mail_id=10,
        sender="a@b.example",
        recipient=TEAM,
        subject="RFQ A",
        body="b",
        category="rfq",
        received_at=old,
    )
    db.claim_quote_send(sid)
    db.record_llm_call(
        sid,
        purpose="x",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        cost_micro_usd=300,
        latency_ms=1,
    )
    db.record_quote(
        sid,
        total_minor=10000,
        currency="EUR",
        airline="X",
        origin="FRA",
        destination="JFK",
        transit_days=2,
        source="portal",
    )
    db.record_outgoing(
        mail_id=11,
        recipient="a@b.example",
        subject="Re: RFQ A",
        body="q",
        category="quote",
        session_id=sid,
    )
    db.ingest_mail(
        mail_id=12,
        sender="a@b.example",
        recipient=TEAM,
        subject="Re: RFQ A",
        body="yes please book",
        category="customer",
        references=[10, 11],
        in_reply_to=11,
    )
    d = client.get("/api/overview", params={"hours": 6}).json()
    assert d["current"]["rfqs"] == 1 and d["current"]["quoted"] == 1
    assert d["current"]["avg_cost_micro_usd"] == 300 and len(d["series"]) == 12
    assert sum(b["rfqs"] for b in d["series"]) == 1
    kinds = {a["kind"] for a in client.get("/api/attention").json()}
    assert "to_book" in kinds
    db.record_booking(
        sid,
        booking_reference="BK-1",
        rate_id="R-12345678",
        airline="X",
        price_minor=10000,
        currency="EUR",
    )
    assert "to_book" not in {a["kind"] for a in client.get("/api/attention").json()}
    assert client.get("/api/overview", params={"hours": 0}).json()["current"]["won"] == 1


def test_import_db_only_when_agents_disabled(client, monkeypatch, tmp_path):
    import dataclasses
    import sqlite3

    src = tmp_path / "src.db"
    db.init_db(str(src))
    sid = db.ingest_mail(
        mail_id=77,
        sender="x@y.example",
        recipient=TEAM,
        subject="RFQ",
        body="b",
        category="rfq",
    )
    db.kv_set("mail.next_since_id", "77")
    sqlite3.connect(src).execute("PRAGMA wal_checkpoint(TRUNCATE)").close()
    data = src.read_bytes()
    db.init_db(str(tmp_path / "target.db"))
    assert client.post("/api/admin/import-db", content=data).status_code == 409
    monkeypatch.setattr(
        server, "settings", dataclasses.replace(server.settings, agents_enabled=False)
    )
    assert client.post("/api/admin/import-db", content=b"nope").status_code == 400
    r = client.post("/api/admin/import-db", content=data).json()
    assert r["counts"]["sessions"] == 1 and r["since_id"] == "77"
    assert db.get_session(sid)["customer"] == "x@y.example"


def test_status_overview_steps_checks_and_todo(client):
    from datetime import datetime, timedelta

    old = (datetime.now(UTC) - timedelta(minutes=2)).isoformat(timespec="seconds")
    sid = db.ingest_mail(
        mail_id=20,
        sender="a@b.example",
        recipient=TEAM,
        subject="RFQ A",
        body="b",
        category="rfq",
        received_at=old,
    )
    db.set_request(sid, {"origin": "FRA", "destination": "JFK", "weight_kg": 40})
    db.claim_quote_send(sid)
    db.record_llm_call(
        sid,
        purpose="x",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        cost_micro_usd=500,
        latency_ms=1,
    )
    db.record_quote(
        sid,
        total_minor=10000,
        currency="EUR",
        airline="X",
        origin="FRA",
        destination="JFK",
        transit_days=2,
        source="portal",
    )
    db.record_outgoing(
        mail_id=21,
        recipient="a@b.example",
        subject="Re: RFQ A",
        body="q",
        category="quote",
        session_id=sid,
    )
    db.save_board_result(
        round_id="1",
        verdict="accepted",
        reason=None,
        points=None,
        raw={"booked": False},
        session_id=sid,
    )
    # a second request that expired without a quote
    lost = db.ingest_mail(
        mail_id=30,
        sender="c@d.example",
        recipient=TEAM,
        subject="RFQ B",
        body="b",
        category="rfq",
        received_at=old,
    )
    db.set_status(lost, "expired", "Deadline abgelaufen ohne Quote")

    d = client.get("/api/status").json()
    rows = {r["id"]: r for r in d["requests"]}
    assert rows[sid]["lane"] == "FRA → JFK" and rows[sid]["steps"]["quote"]["state"] == "ok"
    assert rows[sid]["steps"]["board"]["state"] == "ok"
    assert rows[sid]["steps"]["booking"]["state"] == "run"
    assert rows[lost]["steps"]["quote"]["state"] == "bad"
    checks = {c["key"]: c for c in d["checks"]}
    assert checks["quotes"]["value"] == "1 / 2" and checks["quotes"]["level"] == "warn"
    assert checks["price"]["value"] == "1 / 1" and checks["mail"]["level"] == "ok"
    assert d["score"]["accepted"] == 1 and d["score"]["cost_pts"] > 39 and d["todo"] == []

    # customer says yes, not booked -> to do; after booking it disappears
    db.ingest_mail(
        mail_id=22,
        sender="a@b.example",
        recipient=TEAM,
        subject="Re: RFQ A",
        body="yes please book",
        category="customer",
        references=[20, 21],
        in_reply_to=21,
    )
    assert [t["session_id"] for t in client.get("/api/status").json()["todo"]] == [sid]
    db.record_booking(
        sid,
        booking_reference="BK-1",
        rate_id="R-1",
        airline="X",
        price_minor=10000,
        currency="EUR",
    )
    d = client.get("/api/status").json()
    assert d["todo"] == [] and {r["id"]: r for r in d["requests"]}[sid]["booked"]


def test_pricing_history_lists_options_truck_and_verdict(client):
    sid = db.ingest_mail(
        mail_id=40,
        sender="a@b.example",
        recipient=TEAM,
        subject="RFQ",
        body="b",
        category="rfq",
    )
    db.emit("bid_received", "Gebot Bodenseefracht: 107.52 EUR, 1.25 Tage", sid)
    db.emit(
        "pricing_audit",
        "Pricing inputs and selected option",
        sid,
        request={
            "destination": "RUH",
            "preference": "fastest",
            "pickup_zip": "61391",
            "pickup_assumed": True,
        },
        selected={
            "airline": "Quetzal Air Freight",
            "source": "sheet",
            "transit_days": 2,
            "total_eur_minor": 361612,
            "origin": "FRA",
            "destination": "RUH",
            "chargeable_kg": 1513.02,
        },
        options=[
            {
                "airline": "Quetzal Air Freight",
                "source": "sheet",
                "transit_days": 2,
                "total_eur_minor": 361612,
            },
            {
                "airline": "Tamarind Airways",
                "source": "portal",
                "transit_days": 3,
                "total_eur_minor": 329838,
            },
        ],
        trucking={"carrier": {"name": "Hansekogge Trucking"}, "total_eur": "219.17"},
    )
    db.save_board_result(
        round_id="20",
        verdict="rejected",
        reason="too_high",
        points=None,
        raw={},
        session_id=sid,
    )
    rows = client.get("/api/crm").json()["pricing"]
    r = rows[0]
    assert r["origin"] == "FRA" and r["cheapest_air_minor"] == 329838
    assert r["truck"] == {"name": "Hansekogge Trucking", "eur_minor": 21917}
    assert r["truck_bids"] == 1 and r["verdict"] == "rejected" and r["reason"] == "too_high"


def test_emergency_draft_and_send_records_quote(client, monkeypatch):
    from types import SimpleNamespace

    from app import hackloop
    from app.ui import drafts

    sid = db.ingest_mail(
        mail_id=7237,
        sender="weidner-spielwaren@hackloop.mail",
        recipient=TEAM,
        subject="Air freight quote: CGN to DFW",
        body="Hi, cheapest CGN to DFW, 85 kg, 0.70 cbm, we deliver to CGN ourselves.",
        category="rfq",
    )
    db.set_request(
        sid,
        {
            "origin": "CGN",
            "destination": "DFW",
            "weight_kg": 85,
            "volume_cbm": 0.7,
            "preference": "cheapest",
            "customer_name": "Klaus",
            "language": "de",
        },
    )
    monkeypatch.setattr(
        drafts,
        "portal_result",
        lambda *a, **k: SimpleNamespace(options=[], errors=["timeout"]),
    )
    d = client.post(f"/api/sessions/{sid}/draft").json()
    assert d["choice"]["airline"] == "Quetzal Air Freight" and d["total_minor"] == 41616
    assert "416,16 €" in d["body"] and d["body"].startswith("Hi Klaus")
    assert any("Portal" in w for w in d["warnings"])

    monkeypatch.setattr(hackloop, "send", lambda **k: {"id": 9001})
    r = client.post(f"/api/sessions/{sid}/reply", json={"text": d["body"], "quote": d})
    assert r.status_code == 200
    s = db.get_session(sid)
    assert s["quotes"][0]["total_minor"] == 41616 and s["quotes"][0]["source"] == "manual"
    assert s["mode"] == "auto"  # handed back so the booking agent books on the yes


def test_emergency_draft_without_origin_explains(client):
    sid = db.ingest_mail(
        mail_id=50,
        sender="x@y.example",
        recipient=TEAM,
        subject="RFQ",
        body="quote to DEL please",
        category="rfq",
    )
    db.set_request(sid, {"destination": "DEL", "weight_kg": 100})
    r = client.post(f"/api/sessions/{sid}/draft")
    assert r.status_code == 422 and "Abflughafen" in r.json()["detail"]
