"""History-first recovery and repeated clarification on the unified validation workflow."""

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from app import db
from app.agents import clarification, rfq
from app.mail.rfq_validation import validate
from app.models import Mail, RfqDraft

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
    return Mail(mid, sid or mid, sender, "austrian-power@hackloop.mail", "RFQ", body, now)


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


@pytest.mark.core
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
