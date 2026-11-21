"""Case 12460: a carrier's latest reply must not become the customer's default recipient."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import db, hackloop
from app.ui import server

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "auer-maschinenbau@hackloop.mail"
CARRIER = "ultraviolet-air-freight@hackloop.mail"


@pytest.fixture()
def case(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "replies.db"))
    sid = db.ingest_mail(
        mail_id=12460,
        sender=f"Tobias Auer <{CUSTOMER}>",
        recipient=TEAM,
        subject="Air freight quote to EZE",
        body="Please quote",
        category="rfq",
    )
    db.ingest_mail(
        mail_id=12737,
        sender=CARRIER,
        recipient=TEAM,
        subject="Re: [AP-12460] Air freight STR to EZE",
        body="We do not serve STR-EZE",
        category="carrier_bid",
        references=[sid],
    )
    sent = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"id": 50000 + len(sent)}

    monkeypatch.setattr(hackloop, "send", send)
    monkeypatch.setattr(
        server,
        "settings",
        dataclasses.replace(server.settings, ui_user="team", ui_password="test-ui"),
    )
    with TestClient(server.app, headers={"X-Requested-With": "XMLHttpRequest"}) as client:
        client.auth = ("team", "test-ui")
        yield client, sid, sent


def test_default_reply_targets_customer_after_carrier_mail(case):
    client, sid, sent = case
    result = client.post(f"/api/sessions/{sid}/reply", json={"text": "Our quote"})
    assert result.status_code == 200
    assert sent[0]["to"] == f"Tobias Auer <{CUSTOMER}>"
    assert sent[0]["in_reply_to"] == sid
    assert sent[0]["subject"] == "Air freight quote to EZE"
    assert db.get_session(sid)["first_reply_at"] is not None


def test_default_reply_uses_latest_customer_followup(case):
    client, sid, sent = case
    db.ingest_mail(
        mail_id=12740,
        sender=CUSTOMER.upper(),
        recipient=TEAM,
        subject="Re: Air freight quote to EZE",
        body="Please include pickup",
        category="customer",
        references=[sid],
    )
    db.ingest_mail(
        mail_id=12750,
        sender=CARRIER,
        recipient=TEAM,
        subject="Re: Carrier inquiry",
        body="No service",
        category="carrier_bid",
        references=[sid],
    )
    assert client.post(f"/api/sessions/{sid}/reply", json={"text": "Our quote"}).status_code == 200
    assert sent[0]["in_reply_to"] == 12740
    assert sent[0]["to"].lower() == CUSTOMER


def test_explicit_carrier_reply_does_not_count_as_customer_response(case):
    client, sid, sent = case
    result = client.post(
        f"/api/sessions/{sid}/reply", json={"text": "Thanks for clarifying", "mail_id": 12737}
    )
    assert result.status_code == 200 and sent[0]["to"] == CARRIER
    assert sent[0]["in_reply_to"] == 12737
    session = db.get_session(sid)
    assert session["first_reply_at"] is None and not session["quotes"]
    assert CARRIER in session["events"][-1]["message"]


def test_quote_to_carrier_is_rejected_before_sending_or_takeover(case):
    client, sid, sent = case
    result = client.post(
        f"/api/sessions/{sid}/reply",
        json={"text": "Our quote", "mail_id": 12737, "quote": {"total_minor": 139480}},
    )
    assert result.status_code == 422 and not sent
    session = db.get_session(sid)
    assert session["mode"] == "auto"
    assert session["first_reply_at"] is None and not session["quotes"]


@pytest.mark.parametrize("mail_id", [99999, 12470, 20000])
def test_invalid_or_other_session_reply_target_never_sends(case, mail_id):
    client, sid, sent = case
    db.record_outgoing(
        mail_id=12470,
        recipient=CARRIER,
        subject="Bid request",
        body="request",
        category="bid_request",
        session_id=sid,
    )
    db.ingest_mail(
        mail_id=20000,
        sender="other@hackloop.mail",
        recipient=TEAM,
        subject="Other RFQ",
        body="request",
        category="rfq",
    )
    result = client.post(
        f"/api/sessions/{sid}/reply", json={"text": "Our quote", "mail_id": mail_id}
    )
    assert result.status_code == 422 and not sent
    assert db.get_session(sid)["mode"] == "auto"


def test_emergency_quote_to_customer_is_recorded_for_booking(case):
    client, sid, sent = case
    result = client.post(
        f"/api/sessions/{sid}/reply",
        json={
            "text": "Our all-in price is 1.394,80 €.",
            "quote": {"total_minor": 139480, "choice": {"airline": "Aurelia Air Cargo"}},
        },
    )
    assert result.status_code == 200 and sent[0]["in_reply_to"] == sid
    session = db.get_session(sid)
    assert session["quotes"][0]["total_minor"] == 139480
    assert session["mode"] == "auto"
