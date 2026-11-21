"""History-first recovery and repeated clarification on the unified validation workflow."""

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from app import db
from app.agents import booking, clarification, rfq
from app.mail.rfq_validation import validate
from app.models import Mail, Rfq, RfqDraft

OWNER = "owner@hackloop.mail"


def mail(mid, body, sid=None, sender=OWNER):
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=sender,
        recipient="austrian-power@hackloop.mail",
        subject="RFQ",
        body=body,
        category="customer" if sid else "rfq",
        references=[sid] if sid else [],
        received_at=now,
    )
    return Mail(
        mid, sid or mid, sender, "austrian-power@hackloop.mail", "RFQ", body, now
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "history.db"))
    monkeypatch.setattr(
        clarification,
        "settings",
        dataclasses.replace(clarification.settings, rfq_validation_mode="enforce"),
    )
    monkeypatch.setattr(
        clarification.llm,
        "complete",
        lambda *args, **kwargs: json.dumps({"corrections": []}),
    )


@pytest.fixture
def sent(monkeypatch):
    items = []

    def send(*args, **kwargs):
        items.append((args, kwargs))
        mid = db.conn().execute("SELECT COALESCE(MAX(id),0)+1 FROM mails").fetchone()[0]
        return {"id": mid}

    monkeypatch.setattr(clarification.hackloop, "send", send)
    return items


def test_history_recovers_missing_fields_without_sending(sent, monkeypatch):
    mail(1, "Origin: FRA\nDestination: JFK\nGross weight: 100 kg\nVolume: 2 cbm")
    current = mail(3, "Please quote the shipment discussed above.", sid=1)
    candidate = Rfq(origin="FRA", destination="JFK", weight_kg=100, volume_cbm=2)
    _, draft = validate(current, candidate)
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    clarification.request(current, 1, draft)
    assert len(resumed) == 1 and sent == []
    assert db.get_session(1)["request"]["weight_kg"] == 100


def test_history_excludes_other_customers_and_shipments():
    mail(1, "Gross weight: 100 kg")
    mail(2, "Gross weight: 999 kg")
    mail(3, "Gross weight: 888 kg", sid=1, sender="other@hackloop.mail")
    assert [r["id"] for r in db.customer_mail_history(1, OWNER, 4)] == [1]


def test_partial_answers_accumulate_and_repeat_only_for_new_mail(sent, monkeypatch):
    root = mail(1, "Origin: FRA\nDestination: JFK")
    _, draft = validate(root, RfqDraft(facts={"origin": "FRA", "destination": "JFK"}))
    clarification.request(root, 1, draft)
    first = mail(3, "Gross weight: 100 kg", sid=1)
    assert clarification.handle_reply(first, 1)
    assert len(sent) == 2
    assert db.validation_get(1)["state"] == "awaiting_clarification"
    assert clarification.handle_reply(first, 1)  # duplicate delivered again
    assert len(sent) == 2
    second = mail(5, "Volume: 2 cbm", sid=1)
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    assert clarification.handle_reply(second, 1)
    assert db.validation_get(1)["state"] == "ready" and len(resumed) == 1
    request = db.get_session(1)["request"]
    assert request["weight_kg"] == 100 and request["volume_cbm"] == 2
    assert db.get_session(1)["first_reply_at"] is None


def test_conflicting_history_is_not_a_guess(sent):
    mail(1, "Gross weight: 100 kg")
    mail(2, "Gross weight: 200 kg", sid=1)
    current = mail(3, "Please confirm the rate.", sid=1)
    draft = RfqDraft(
        facts={"origin": "FRA", "destination": "JFK", "volume_cbm": 2},
        issues=[{"field": "weight_kg", "code": "missing"}],
    )
    clarification.request(current, 1, draft)
    assert len(sent) == 1
    assert db.validation_get(1)["state"] == "awaiting_clarification"


def test_wrong_sender_cannot_resolve_question(sent):
    root = mail(1, "Origin: FRA\nDestination: JFK")
    _, draft = validate(root, RfqDraft(facts={"origin": "FRA", "destination": "JFK"}))
    clarification.request(root, 1, draft)
    stranger = mail(
        3, "Gross weight: 100 kg\nVolume: 2 cbm", sid=1, sender="other@hackloop.mail"
    )
    assert clarification.handle_reply(stranger, 1)
    assert db.validation_get(1)["state"] == "awaiting_clarification"
    assert len(sent) == 1


def test_ambiguous_booking_asks_then_accepts_reply(sent, monkeypatch):
    mail(1, "Origin: FRA\nDestination: JFK\nGross weight: 100 kg\nVolume: 2 cbm")
    db.record_quote(
        1,
        total_minor=10000,
        currency="EUR",
        airline="Test",
        origin="FRA",
        destination="JFK",
        transit_days=2,
        source="portal",
        rate_id="rate",
        mail_id=2,
    )
    unclear = mail(3, "I need to think about the timing", sid=1)
    booking.handle_reply(unclear, 1)
    assert len(sent) == 1 and "confirm whether" in sent[0][1]["body"]
    assert db.get_session(1)["bookings"] == []
    answer = mail(5, "Yes, go ahead and book it", sid=1)
    resumed = []
    monkeypatch.setattr(booking, "handle_reply", lambda *args: resumed.append(args))
    clarification.handle_reply(answer, 1)
    assert resumed == [(answer, 1)]


def test_unknown_send_can_be_resolved_without_resending(monkeypatch):
    root = mail(1, "Origin: FRA\nDestination: JFK")
    _, draft = validate(root, RfqDraft(facts={"origin": "FRA", "destination": "JFK"}))
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise TimeoutError("delivery unknown")

    monkeypatch.setattr(clarification.hackloop, "send", fail)
    clarification.request(root, 1, draft)
    clarification.request(root, 1, draft)
    assert len(attempts) == 1
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    answer = mail(3, "Gross weight: 100 kg\nVolume: 2 cbm", sid=1)
    clarification.handle_reply(answer, 1)
    assert db.validation_get(1)["state"] == "ready" and len(resumed) == 1


def test_restart_processes_saved_answer_even_if_dispatch_never_ran(sent, monkeypatch):
    root = mail(1, "Origin: FRA\nDestination: JFK")
    _, draft = validate(root, RfqDraft(facts={"origin": "FRA", "destination": "JFK"}))
    clarification.request(root, 1, draft)
    mail(3, "Gross weight: 100 kg\nVolume: 2 cbm", sid=1)
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    clarification.catch_up()
    assert db.validation_get(1)["state"] == "ready" and len(resumed) == 1
    clarification.catch_up()
    assert len(resumed) == 1 and len(sent) == 1
