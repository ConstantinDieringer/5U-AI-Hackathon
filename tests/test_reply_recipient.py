"""Manual recipient routing with real isolated CRM records and a mocked mail transport."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import db, hackloop
from app.ui import server

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "anna@hackloop.mail"
TRUCKER = "kranich-kurier@hackloop.mail"
AIRLINE = "kestrel-airways@hackloop.mail"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "recipients.db"))
    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, ui_password=""))
    db.ingest_mail(
        mail_id=1,
        sender=f"Anna <{CUSTOMER}>",
        recipient=TEAM,
        subject="RFQ to MIA",
        body="Please quote.",
        category="rfq",
    )
    db.record_outgoing(
        mail_id=2,
        recipient=TRUCKER,
        subject="[AP-1] Pickup to STR",
        body="Please quote trucking.",
        category="bid_request",
        session_id=1,
    )
    db.ingest_mail(
        mail_id=3,
        sender=TRUCKER,
        recipient=TEAM,
        subject="Re: [AP-1] Pickup to STR",
        body="Our bid is 100 EUR.",
        category="carrier_bid",
        references=[1, 2],
        in_reply_to=2,
    )
    db.record_outgoing(
        mail_id=4,
        recipient=AIRLINE,
        subject="[AP-1] STR to MIA",
        body="Please quote air freight.",
        category="bid_request",
        session_id=1,
    )
    sent = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"id": 100 + len(sent)}

    monkeypatch.setattr(hackloop, "send", send)
    with TestClient(server.app) as client:
        yield client, sent


def test_recipients_distinguish_starter_airline_and_trucker(env):
    client, _ = env
    choices = client.get("/api/sessions/1").json()["reply_recipients"]
    assert choices[0]["address"] == CUSTOMER
    by_address = {r["address"]: r for r in choices}
    assert len(choices) == 3 and TEAM not in by_address
    assert by_address[CUSTOMER]["is_thread_starter"]
    assert "Thread gestartet" in by_address[CUSTOMER]["role_label"]
    assert (
        by_address[TRUCKER]["role"] == "trucker"
        and "Frachtführer" in by_address[TRUCKER]["role_label"]
    )
    assert by_address[AIRLINE]["role"] == "airline"
    assert by_address[AIRLINE]["parent_mail_id"] == 4  # contacted, not yet answered
    assert by_address[TRUCKER]["parent_mail_id"] == 3 and by_address[TRUCKER]["mail_ids"] == [2, 3]


def test_default_is_original_customer_even_if_last_incoming_mail_is_carrier(env):
    client, sent = env
    assert client.post("/api/sessions/1/reply", json={"text": "Hello Anna"}).status_code == 200
    assert sent == [
        {"to": CUSTOMER, "subject": "Re: RFQ to MIA", "body": "Hello Anna", "in_reply_to": 1}
    ]
    assert db.get_session(1)["first_reply_at"] is not None


def test_explicit_carrier_reply_uses_its_thread_and_does_not_mark_customer_answered(env):
    client, sent = env
    response = client.post(
        "/api/sessions/1/reply",
        json={"to": TRUCKER, "mail_id": 3, "text": "Can you improve your bid?"},
    )
    assert response.status_code == 200
    assert sent[0]["to"] == TRUCKER and sent[0]["in_reply_to"] == 3
    session = db.get_session(1)
    assert (
        session["first_reply_at"] is None
        and session["quotes"] == []
        and session["mode"] == "manual"
    )
    saved = next(
        m for m in session["mails"] if m["direction"] == "out" and m["category"] == "manual_reply"
    )
    assert saved["recipient"] == TRUCKER and saved["in_reply_to"] == 3


def test_outbound_only_participant_can_be_contacted(env):
    client, sent = env
    response = client.post(
        "/api/sessions/1/reply", json={"to": AIRLINE, "text": "Please confirm your transit time."}
    )
    assert response.status_code == 200 and sent[0]["to"] == AIRLINE
    assert sent[0]["in_reply_to"] == 4


def test_legacy_explicit_mail_selection_keeps_working(env):
    client, sent = env
    assert (
        client.post("/api/sessions/1/reply", json={"mail_id": 3, "text": "Thanks"}).status_code
        == 200
    )
    assert sent[0]["to"] == TRUCKER


@pytest.mark.parametrize(
    "payload",
    [
        {"to": CUSTOMER, "mail_id": 3},
        {"to": TRUCKER, "mail_id": 1},
        {"to": "outsider@hackloop.mail"},
        {"to": TEAM},
        {"to": ""},
        {"to": CUSTOMER + "\r\nBcc: outsider@hackloop.mail"},
        {"to": CUSTOMER, "mail_id": 999},
    ],
)
def test_invalid_recipient_or_parent_is_rejected_before_send_and_takeover(env, payload):
    client, sent = env
    assert (
        client.post("/api/sessions/1/reply", json={"text": "Hello", **payload}).status_code == 422
    )
    assert not sent and db.get_session(1)["mode"] == "auto"


def test_customer_quote_cannot_be_registered_against_carrier_mail(env):
    client, sent = env
    response = client.post(
        "/api/sessions/1/reply",
        json={
            "to": TRUCKER,
            "text": "100 EUR",
            "quote": {"total_minor": 10000},
        },
    )
    assert response.status_code == 422 and not sent and db.get_session(1)["quotes"] == []


def test_parent_from_another_session_is_rejected(env):
    client, sent = env
    db.ingest_mail(
        mail_id=20,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Different shipment",
        body="Other cargo",
        category="rfq",
    )
    response = client.post(
        "/api/sessions/1/reply", json={"to": CUSTOMER, "mail_id": 20, "text": "Hello"}
    )
    assert response.status_code == 422 and not sent


def test_other_participants_are_labelled_without_guessing_their_role(env):
    client, _ = env
    db.ingest_mail(
        mail_id=5,
        sender="assistant@hackloop.mail",
        recipient=TEAM,
        subject="Re: RFQ to MIA",
        body="Please keep me posted",
        category="customer",
        references=[1],
    )
    choice = next(
        r
        for r in client.get("/api/sessions/1").json()["reply_recipients"]
        if r["address"] == "assistant@hackloop.mail"
    )
    assert choice["role"] == "participant" and choice["role_label"] == "Threadteilnehmer"


@pytest.mark.parametrize(
    ("email", "role"),
    [
        ("ratedesk@hackloop.mail", "ratedesk"),
        ("tracking@hackloop.mail", "tracking"),
    ],
)
def test_service_addresses_get_their_own_role(env, email, role):
    client, _ = env
    db.ingest_mail(
        mail_id=5,
        sender=email,
        recipient=TEAM,
        subject="Re: RFQ",
        body="Status",
        category="portal_reply",
        references=[1],
    )
    choice = next(
        r for r in client.get("/api/sessions/1").json()["reply_recipients"] if r["address"] == email
    )
    assert choice["role"] == role and not choice["is_thread_starter"]


def test_recipient_is_normalized_and_duplicate_display_names_are_not_options(env):
    client, sent = env
    db.ingest_mail(
        mail_id=5,
        sender=f"New name <{CUSTOMER.upper()}>",
        recipient=TEAM,
        subject="Re: RFQ",
        body="Another question",
        category="customer",
        references=[1],
    )
    choices = client.get("/api/sessions/1").json()["reply_recipients"]
    assert sum(r["address"] == CUSTOMER for r in choices) == 1
    response = client.post(
        "/api/sessions/1/reply", json={"to": CUSTOMER.upper(), "text": "Hello Anna"}
    )
    assert response.status_code == 200 and sent[0]["to"] == CUSTOMER and sent[0]["in_reply_to"] == 5
