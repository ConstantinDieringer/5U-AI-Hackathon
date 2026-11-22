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


ANNA_ROOT = (
    "Could you please quote the same shipment as in my earlier request? The cargo and pickup"
    " details are unchanged. The new destination is LAX, ready for pickup on 2026-11-29."
    " Please send me an all-in price in EUR for the cheapest option."
)
ANNA_REPLY = (
    "The departure airport is Stuttgart (STR), and the destination for this shipment is LAX."
    " The shipment consists of 6 pieces, with a total gross weight of 1,841 kg and a total"
    " volume of 4.63 cbm. We will deliver the shipment to STR ourselves, so no pickup is required."
)


def _rescue_plan(corrections):
    from app.agents import rescue

    return rescue.Plan.model_validate(
        {
            "action": "retry_pricing",
            "reason": "customer answered",
            "corrections": [{"field": f, "value": v, "evidence": e} for f, v, e in corrections],
            "releases": [],
            "question_en": "",
            "question_de": "",
        }
    )


@pytest.mark.core
def test_rescue_plan_names_open_fields():
    """Session 30351: a blocked rescue plan expired silently. It must say what stays open;
    a complete answer still builds the RFQ."""
    from app.agents import rescue

    root = mail(1, ANNA_ROOT)
    reply = mail(3, ANNA_REPLY, sid=1)
    facts = {"destination": "LAX", "preference": "cheapest", "ready_date": "2026-11-29"}
    _, draft = validate(root, RfqDraft(facts=facts))
    evidence = rescue._normal(ANNA_ROOT + "\n" + ANNA_REPLY)
    full = [
        ("origin", "STR", "The departure airport is Stuttgart (STR)"),
        ("weight_kg", "1841", "with a total gross weight of 1,841 kg"),
        ("volume_cbm", "4.63", "a total volume of 4.63 cbm"),
        ("pieces", "6", "The shipment consists of 6 pieces"),
        ("no_pickup", "true", "so no pickup is required"),
    ]
    request, _ = rescue._apply(
        reply, 1, root, draft.model_copy(deep=True), _rescue_plan(full), evidence
    )
    assert request and request.origin == "STR" and request.weight_kg == 1841

    # counterexample: the origin quote is not in the customer's words, so it stays open
    partial = [("origin", "STR", "we ship from Stuttgart")] + full[1:]
    request, checked = rescue._apply(
        reply, 1, root, draft.model_copy(deep=True), _rescue_plan(partial), evidence
    )
    assert request is None
    assert "origin" in {i["field"] for i in checked.issues}
