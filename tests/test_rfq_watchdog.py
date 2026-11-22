"""RFQ watchdog (RFQ_WATCHDOG): an RFQ thread still without our quote ~200 s after arrival gets
the best quote real prices allow, once, through the one-quote claim. Never for a paused or
taken-over session, never on open dangerous goods, an excluded carrier or a phishing mail; no
real price means nothing is sent and an alert is raised. Also: a restart in the middle of a
round (rounds 4, 9, 15 to 17 had pushes inside the round)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import config, db
from app.agents import bids, clarification, rfq, watchdog
from app.models import Mail, RateOption
from app.pricing.portal_sync import PortalResult
from app.pricing.reference import carrier_by_email

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "anna@kunde.example"
TRUCKER = "hansekogge-trucking@hackloop.mail"
SID = 5000
BODY = (
    "Hello,\n\nplease quote air freight for a pickup at our site in 61018 to JFK.\n"
    "3 pieces, gross weight 500 kg, volume 2.0 cbm. Cheapest option please.\n\n"
    "Thanks,\nAnna"
)
REQUEST = {
    "origin": None,
    "pickup_zip": "61018",
    "destination": "JFK",
    "weight_kg": 500.0,
    "volume_cbm": 2.0,
    "pieces": 3,
    "preference": "cheapest",
    "ready_date": None,
    "language": "en",
    "customer_name": "Anna",
    "customer_questions": [],
    "service_constraints": [],
}


def _rate(airline: str, minor: int, days: int = 2) -> RateOption:
    valid = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    return RateOption(
        airline, "FRA", "JFK", days, minor, "EUR", minor, "portal", "R-1", valid, 500.0, 500.0
    )


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Isolated DB, captured sends, portal with two real-looking rates, no LLM."""
    db.init_db(str(tmp_path / "w.db"))
    sent: list[dict] = []
    state = {"portal": [_rate("Solace Air Cargo", 400_000), _rate("Gannet Airways", 450_000)]}

    def send(to, subject, body, in_reply_to=None, **_k):
        sent.append(
            {"id": 9_000 + len(sent), "to": to, "subject": subject, "body": body,
             "in_reply_to": in_reply_to}
        )  # fmt: skip
        return sent[-1]

    def complete(purpose, *_a, **_k):
        if purpose.startswith("extract"):
            return json.dumps({**REQUEST, "attachment_text": ""})
        raise ValueError("no composer in this test")  # deterministic quote copy

    for module in (rfq.hackloop, bids.hackloop, clarification.hackloop):
        monkeypatch.setattr(module, "send", send)
    monkeypatch.setattr(rfq.llm, "complete", complete)
    monkeypatch.setattr(rfq, "portal_result", lambda *_a, **_k: PortalResult(state["portal"]))
    monkeypatch.setattr(rfq, "_sheets", lambda *_a: [])
    trucker = carrier_by_email(TRUCKER)
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *_a: ([trucker], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *_a: {})
    monkeypatch.setattr(bids, "wait", lambda *_a, **_k: [])
    rfq._working.clear()
    yield {"sent": sent, "state": state}
    rfq._working.clear()


def configure(monkeypatch, **changes) -> None:
    new = dataclasses.replace(config.settings, **changes)
    for module in (config, rfq, clarification, watchdog):
        monkeypatch.setattr(module, "settings", new)


def rfq_mail(age_s: float, sid: int = SID, category: str = "rfq") -> Mail:
    at = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    db.ingest_mail(
        mail_id=sid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Air freight to JFK",
        body=BODY,
        category=category,
        received_at=at,
    )
    return Mail(sid, sid, CUSTOMER, TEAM, "Air freight to JFK", BODY, at)


def stored(sid: int = SID, request: dict | None = None, status: str = "error") -> None:
    db.set_request(sid, request or REQUEST)
    db.set_status(sid, status, "test")


def kinds(sid: int = SID) -> list[str]:
    return [e["kind"] for e in (db.get_session(sid) or {}).get("events", [])]


# ---------------------------------------------------------------- forcing


def test_a_claim_whose_send_may_have_happened_is_never_released(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored()
    assert db.claim_quote_send(SID)
    db.kv_set(f"quote.transport.{SID}", db.now())  # the HTTP send had started
    assert watchdog.due()[0]["action"] == "claimed"
    assert watchdog.check_once(background=False) == []
    assert watchdog.check_once(background=False) == []
    assert world["sent"] == []
    assert kinds().count("watchdog_alert") == 1  # a person checks the mailbox, once


# ---------------------------------------------------------------- causes and deploy gate


def test_deploy_gate(world) -> None:
    from app.ui import deploy_gate

    t = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
    for i in range(4):  # every 364 s, the last one at t
        at = (t - timedelta(seconds=364 * (3 - i))).isoformat()
        db.ingest_mail(
            mail_id=100 + i,
            sender=CUSTOMER,
            recipient=TEAM,
            subject="RFQ",
            body="x",
            category="rfq",
            received_at=at,
        )
    for sid in (100, 101, 102):
        assert db.claim_quote_send(sid)
    # newest RFQ 60 s old and not answered: wait
    g = deploy_gate.gate(t + timedelta(seconds=60))
    assert not g["safe"] and g["interval_s"] == 364
    assert db.claim_quote_send(103)
    g = deploy_gate.gate(t + timedelta(seconds=60))
    assert g["safe"] and g["seconds_to_next"] == 304
    assert not deploy_gate.gate(t + timedelta(seconds=300))["safe"]  # next RFQ in 64 s
    # a missed round leaves a double gap; the phase still comes from the newest arrival
    assert deploy_gate.interval_s([t, t + timedelta(seconds=728), t + timedelta(seconds=1092),
                                   t + timedelta(seconds=1456)]) == 364  # fmt: skip
