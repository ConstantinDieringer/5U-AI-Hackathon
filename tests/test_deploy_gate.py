"""Deploy gate (a push restarts the only live instance). Round at 01:50:51 UTC: the gate said
"next RFQ in 359 s" 0.4 s after RFQ 31793 arrived, and the restart hit its pricing."""

from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.ui import deploy_gate

TEAM = "austrian-power@hackloop.mail"
T0 = datetime(2026, 11, 22, 1, 20, tzinfo=UTC)
SPACING = 364


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "gate.db"))
    yield


def _rounds(n: int) -> datetime:
    """n answered RFQs every 364 s; returns the newest arrival."""
    at = T0
    for i in range(n):
        at = T0 + timedelta(seconds=i * SPACING)
        sid = 100 + i
        db.ingest_mail(
            mail_id=sid,
            sender=f"c{i}@shipper.example",
            recipient=TEAM,
            subject="RFQ",
            body="quote please",
            category="rfq",
            received_at=at.isoformat(),
        )
        db.conn().execute(
            "UPDATE sessions SET created_at = ?, updated_at = ?, first_reply_at = ?,"
            " status = 'quoted' WHERE id = ?",
            (at.isoformat(), at.isoformat(), (at + timedelta(seconds=45)).isoformat(), sid),
        )
    db.conn().commit()
    return at


def test_green_between_rounds():
    newest = _rounds(5)
    result = deploy_gate.gate(newest + timedelta(seconds=120))
    assert result["safe"] is True


def test_red_while_the_next_rfq_is_due_but_not_stored_yet():
    newest = _rounds(5)
    result = deploy_gate.gate(newest + timedelta(seconds=SPACING + 1))
    assert result["safe"] is False and "fällig" in result["reason"]


def test_green_again_when_the_due_rfq_never_came():
    newest = _rounds(5)
    assert deploy_gate.gate(newest + timedelta(seconds=SPACING + 200))["safe"] is True


def test_red_while_a_session_is_pricing():
    newest = _rounds(5)
    now = datetime.now(UTC)
    db.conn().execute(
        "UPDATE sessions SET status = 'pricing', updated_at = ? WHERE id = 102", (now.isoformat(),)
    )
    db.conn().commit()
    result = deploy_gate.gate(newest + timedelta(seconds=120))
    assert result["safe"] is False and result["pricing_sessions"] == 1
