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
    health = client.get("/api/health").json()
    assert health["email_style_version"] == "strict-customer-prose-v4"
    assert health["email_reviewer_model"] == "gpt-5.6-luna"
    assert (
        health["llm_model"]
        == health["llm_compose_model"]
        == health["llm_vision_model"]
        == "gpt-5.6-luna"
    )
    assert "build_revision" in health
    assert client.get("/").status_code == 200
    rows = client.get("/api/sessions").json()
    assert [r["id"] for r in rows] == [sid]
    assert client.get("/api/sessions", params={"q": "nomatch"}).json() == []
    assert client.get(f"/api/sessions/{sid}").json()["mails"][0]["body"].startswith("<script>")
    assert client.get("/api/sessions/999").status_code == 404


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


def test_problem_feed_hides_echoes_and_handled_groups(client):
    sid = seed()
    db.emit("board_verdict", "Board: no answer", sid, level="error")
    db.emit(
        "log_error", f"app.ui.watchers: board: round 100 (session {sid}) no answer", level="error"
    )
    db.emit("no_price", "Kein Preis gefunden, kein Angebot", sid, level="error")
    db.set_status(sid, "error", "kein Preis")
    lone = db.ingest_mail(
        mail_id=2,
        sender="c@d.example",
        recipient=TEAM,
        subject="RFQ MUC-ORD",
        body="b",
        category="rfq",
    )
    db.set_status(lone, "error", "agent crashed")  # no other error names the cause: keep it

    kinds = sorted(g["kind"] for g in client.get("/api/problems").json()["groups"])
    assert kinds == ["board_verdict", "no_price", "status"]

    feed = client.get("/api/problems").json()
    board = next(g for g in feed["groups"] if g["kind"] == "board_verdict")
    ack = client.post("/api/problems/ack", json={"acks": {board["key"]: board["last_event_id"]}})
    assert ack.status_code == 200
    feed = client.get("/api/problems").json()
    assert "board_verdict" not in [g["kind"] for g in feed["groups"]]
    assert feed["handled"] == 1

    db.emit("board_verdict", "Board: no answer", sid, level="error")  # happens again
    assert "board_verdict" in [g["kind"] for g in client.get("/api/problems").json()["groups"]]
    assert client.post("/api/problems/ack", json={"acks": {}}).status_code == 422
