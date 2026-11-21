"""SOS alerts: which events reach Slack, once each, with a link. No network."""

import pytest

from app import db
from app.ui import alerts


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "a.db"))


def test_alerts_once_per_session_and_kind_with_link():
    sid = db.ingest_mail(
        mail_id=10,
        sender="weidner-spielwaren@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="Air freight quote to RUH",
        body="b",
        category="rfq",
    )
    db.set_request(sid, {"origin": "FRA", "destination": "RUH", "weight_kg": 970})
    db.kv_set(alerts.KV_LAST, "0")
    db.emit("no_price", "Kein Preis gefunden, kein Angebot", sid, level="error")
    db.set_status(sid, "error", "kein Preis")
    db.set_status(sid, "error", "kein Preis")  # repeated: no second alert
    db.save_board_result(
        round_id="20",
        verdict="rejected",
        reason="too_high",
        points=None,
        raw={},
        session_id=sid,
    )
    db.save_board_result(
        round_id="21",
        verdict="accepted",
        reason=None,
        points=None,
        raw={},
        session_id=sid,
    )
    seen: set = set()
    out = alerts.scan(seen)
    assert len(out) == 2
    assert out[0].startswith(":x: Keine Quote möglich: kein Preis")
    assert (
        "FRA → RUH" in out[0]
        and "weidner-spielwaren" in out[0]
        and f"#session={sid}" in out[0]
    )
    assert out[1].startswith(":red_circle: 5U rejected (too_high)")
    assert alerts.scan(seen) == []  # cursor advanced


def test_program_error_and_quiet_events():
    db.kv_set(alerts.KV_LAST, "0")
    db.emit("error", "Mail 2256 failed: AttributeError: x", None, level="error")
    db.emit("quoted", "Quote: X 100 EUR", None)
    out = alerts.scan(set())
    assert out == [":boom: Fehler: Mail 2256 failed: AttributeError: x"]


def test_post_is_noop_without_webhook(monkeypatch):
    monkeypatch.setattr(alerts, "WEBHOOK", "")
    assert alerts.post("x") is False
