"""Holding replies wait before one targeted follow-up; all sends use isolated SQLite."""

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.agents import clarification, rfq
from app.mail.rfq_validation import validate
from app.models import Mail, Rfq, RfqDraft

OWNER = "sonnberg-roesterei@hackloop.mail"


class Clock(datetime):
    instant = datetime(2026, 11, 21, 18, 51, 41, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.instant if tz else cls.instant.replace(tzinfo=None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    Clock.instant = datetime(2026, 11, 21, 18, 51, 41, tzinfo=UTC)
    path = str(tmp_path / "followups.db")
    db.init_db(path)
    monkeypatch.setattr(clarification, "datetime", Clock)
    monkeypatch.setattr(db, "now", lambda: Clock.now(UTC).isoformat())
    monkeypatch.setattr(
        clarification,
        "settings",
        dataclasses.replace(
            clarification.settings,
            rfq_validation_mode="enforce",
            clarification_followup_s=15,
        ),
    )
    sent = []

    def send(to, subject, body, **kwargs):
        mid = db.conn().execute("SELECT COALESCE(MAX(id),0)+1 FROM mails").fetchone()[0]
        sent.append({"id": mid, "to": to, "body": body, **kwargs})
        return {"id": mid}

    monkeypatch.setattr(clarification.hackloop, "send", send)
    monkeypatch.setattr(rfq, "handle_rfq", lambda *a: None)
    return sent, path


def incoming(body, root=False):
    mid = db.conn().execute("SELECT COALESCE(MAX(id),0)+1 FROM mails").fetchone()[0]
    stamp = Clock.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=OWNER,
        recipient="austrian-power@hackloop.mail",
        subject="Air freight quote request to MIA",
        body=body,
        category="rfq" if root else "customer",
        references=[] if root else [1],
        received_at=stamp,
    )
    return Mail(
        mid,
        1,
        OWNER,
        "austrian-power@hackloop.mail",
        "Air freight quote request to MIA",
        body,
        stamp,
        references=[] if root else [1],
    )


def dg_request():
    root = incoming(
        "Please quote STR to MIA, including collection from our site in 71184. "
        "Gross weight: 378 kg. Total volume: 1.86 cbm. Total pieces: 4.",
        root=True,
    )
    candidate = Rfq(
        origin="STR",
        destination="MIA",
        pickup_zip="71184",
        weight_kg=378,
        volume_cbm=1.86,
        pieces=4,
    )
    sources = {
        "packing.pdf": "Dangerous goods: UN3481, Class 9, Lithium ion batteries packed with equipment"
    }
    _, draft = validate(root, candidate, sources)
    assert {i["field"] for i in draft.issues} == {"dangerous_goods"}
    draft.evidence["attachment_texts"] = sources
    clarification.request(root, 1, draft)
    return root


def hold(text="Thanks for your message. I need to check that and will come back to you."):
    reply = incoming(text)
    assert clarification.handle_reply(reply, 1)
    return reply


def advance(seconds):
    Clock.instant += timedelta(seconds=seconds)


def timer():
    return dict(
        db.conn().execute("SELECT * FROM clarification_followups WHERE session_id=1").fetchone()
    )


def test_thread_10132_waits_15_seconds_then_rephrases_dg_request(env):
    sent, _ = env
    dg_request()
    reply = hold()
    assert len(sent) == 1 and timer()["state"] == "pending"
    advance(14.9)
    clarification.follow_up_due()
    assert len(sent) == 1
    advance(0.1)
    clarification.follow_up_due()
    assert len(sent) == 2
    followup = sent[-1]
    assert followup["body"] != sent[0]["body"]
    assert followup["in_reply_to"] == reply.id and followup["to"] == OWNER
    for term in ("dangerous goods documents", "UN3481", "Class 9", "DGD", "SDS", "attach"):
        assert term in followup["body"]
    assert "Pickup ZIP" not in followup["body"]
    clarification.follow_up_due()
    clarification.catch_up()
    assert len(sent) == 2 and timer()["state"] == "sent"


def test_mentioning_documents_in_holding_reply_does_not_clear_dg_gate(env):
    sent, _ = env
    dg_request()
    hold("I need to check the dangerous goods documents and will get back to you.")
    row = db.validation_get(1)
    assert row["state"] == "awaiting_clarification" and len(sent) == 1
    assert any(i["field"] == "dangerous_goods" for i in row["draft"]["issues"])
    assert row["draft"]["evidence"]["corrections"].get("dangerous_goods") is None


def test_newer_reply_cancels_even_before_its_agent_runs(env):
    sent, _ = env
    dg_request()
    hold()
    advance(14)
    incoming("Please find the DG documents attached.")
    advance(1)
    clarification.follow_up_due()
    assert len(sent) == 1 and timer()["state"] == "cancelled"


def test_complete_answer_resumes_and_cancels_timer(env, monkeypatch):
    sent, _ = env
    root = incoming("Please quote FRA to MIA. Volume: 2 cbm.", root=True)
    _, draft = validate(
        root, RfqDraft(facts={"origin": "FRA", "destination": "MIA", "volume_cbm": 2})
    )
    clarification.request(root, 1, draft)
    hold()
    advance(10)
    answer = incoming("Gross weight: 100 kg")
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *a: resumed.append(a))
    clarification.handle_reply(answer, 1)
    advance(5)
    clarification.follow_up_due()
    assert len(sent) == 1 and len(resumed) == 1
    assert db.validation_get(1)["state"] == "ready" and timer()["state"] == "cancelled"


def test_reply_in_webhook_queue_cancels_before_mail_processing(env):
    sent, _ = env
    dg_request()
    pending = hold()
    db.delivery_enqueue(
        pending.id + 1,
        {
            "id": pending.id + 1,
            "from": OWNER,
            "references": [1],
            "body": "Here are the documents.",
        },
        "webhook",
    )
    advance(15)
    clarification.follow_up_due()
    assert len(sent) == 1 and timer()["state"] == "cancelled"


def test_new_holding_reply_restarts_wait_but_duplicate_does_not(env):
    sent, _ = env
    dg_request()
    first = hold()
    advance(10)
    clarification.handle_reply(first, 1)
    later = hold("I will check with our shipping department.")
    advance(5)
    clarification.follow_up_due()
    assert len(sent) == 1
    advance(10)
    clarification.follow_up_due()
    assert len(sent) == 2 and sent[-1]["in_reply_to"] == later.id


def test_interrupted_timer_send_becomes_unknown_on_restart(env):
    sent, path = env
    dg_request()
    pending = hold()
    advance(15)
    assert db.clarification_followup_claim(1, pending.id, Clock.now(UTC).isoformat())
    db.init_db(path)
    clarification.catch_up()
    clarification.follow_up_due()
    assert len(sent) == 1 and timer()["state"] == "unknown"


def test_partial_information_is_retained_while_customer_checks_remaining_fact(env):
    sent, _ = env
    root = incoming("Please quote FRA to MIA.", root=True)
    _, draft = validate(root, RfqDraft(facts={"origin": "FRA", "destination": "MIA"}))
    clarification.request(root, 1, draft)
    hold("Gross weight: 100 kg. I need to check the volume and will come back to you.")
    advance(15)
    clarification.follow_up_due()
    assert len(sent) == 2 and "total volume" in sent[-1]["body"]
    assert "gross weight" not in sent[-1]["body"]
    assert db.validation_get(1)["draft"]["evidence"]["corrections"]["weight_kg"] == 100


@pytest.mark.parametrize("control", ["pause", "manual", "global_pause"])
def test_human_control_blocks_timer(env, control):
    sent, _ = env
    dg_request()
    hold()
    if control == "pause":
        db.set_paused(1, True)
    elif control == "manual":
        db.takeover(1)
    else:
        db.kv_set("global_pause", "1")
    advance(15)
    clarification.follow_up_due()
    assert len(sent) == 1 and timer()["state"] == "pending"


def test_expired_rfq_never_sends_timer(env):
    sent, _ = env
    dg_request()
    hold()
    advance(361)
    clarification.follow_up_due()
    assert len(sent) == 1 and db.validation_get(1)["state"] == "expired"


def test_restart_keeps_due_time_and_does_not_repeat(env):
    sent, path = env
    dg_request()
    hold()
    advance(10)
    db.init_db(path)
    clarification.catch_up()
    clarification.follow_up_due()
    assert len(sent) == 1
    advance(5)
    clarification.follow_up_due()
    db.init_db(path)
    clarification.catch_up()
    clarification.follow_up_due()
    assert len(sent) == 2


def test_uncertain_send_is_never_retried(env, monkeypatch):
    sent, _ = env
    dg_request()
    hold()
    calls = []

    def timeout(*a, **kw):
        calls.append(1)
        raise TimeoutError("ambiguous delivery")

    monkeypatch.setattr(clarification.hackloop, "send", timeout)
    advance(15)
    clarification.follow_up_due()
    clarification.catch_up()
    clarification.follow_up_due()
    assert calls == [1] and timer()["state"] == "unknown" and len(sent) == 1


def test_two_workers_cannot_send_the_same_timer(env):
    sent, _ = env
    dg_request()
    hold()
    advance(15)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: clarification.follow_up_due(), range(2)))
    assert len(sent) == 2


def test_further_holding_replies_do_not_create_a_reminder_loop(env):
    sent, _ = env
    dg_request()
    first = hold()
    clarification.handle_reply(first, 1)  # repeated delivery does not postpone the timer
    advance(15)
    clarification.follow_up_due()
    hold()
    advance(30)
    clarification.follow_up_due()
    assert len(sent) == 2 and timer()["state"] == "sent"


def test_german_holding_reply_gets_specific_german_followup(env):
    sent, _ = env
    dg_request()
    hold("Ich muss das erst prüfen und melde mich dann wieder.")
    advance(15)
    clarification.follow_up_due()
    assert "Gefahrgutdokumente" in sent[-1]["body"]
    assert "Sicherheitsdatenblatt" in sent[-1]["body"] and "UN3481" in sent[-1]["body"]


def test_customer_can_decline_further_reminders(env):
    sent, _ = env
    dg_request()
    hold("I need to check internally. Please don't send any reminders.")
    advance(30)
    clarification.follow_up_due()
    assert len(sent) == 1 and db.validation_get(1)["state"] == "manual_review"


def test_second_question_cap_does_not_leave_send_in_flight(env):
    sent, _ = env
    dg_request()
    hold()
    advance(15)
    clarification.follow_up_due()
    answer = incoming("The packing list is correct. Please quote.")
    clarification.handle_reply(answer, 1)
    assert len(sent) == 2 and db.validation_get(1)["state"] == "manual_review"
    assert db.validation_get(1)["send_state"] != "sending"
