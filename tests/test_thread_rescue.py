"""Recover a real audio/clarification dead end, then keep ownership through booking."""

import dataclasses
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.agents import booking, clarification, rescue, rfq
from app.mail import poller
from app.models import Category, Mail, RfqDraft

SID = 17998
ANSWER = (
    "Textile samples, 11 pieces, 1804 kg gross, 17.18 cbm. Ready on 24 November 2026. "
    "Please include pickup from 51943 Germany and deliver to LIM. "
    "These are all the shipment details from the voice message."
)


@pytest.fixture
def case(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "rescue.db"))
    settings = dataclasses.replace(
        rescue.settings,
        thread_rescue_enabled=True,
        rfq_validation_mode="enforce",
        clarify_once=False,
    )
    for module in (rescue, clarification, rfq):
        monkeypatch.setattr(module, "settings", settings)
    root = Mail(
        SID,
        SID,
        "weidner-spielwaren@hackloop.mail",
        "team@hackloop.mail",
        "Voice message quotation",
        "Please quote the attached voice message.",
        datetime.now(UTC).isoformat(),
        attachments=[{"id": 304, "filename": "voice-message.mp3"}],
    )
    ingest(root, "rfq")
    facts = {"origin": None, "destination": "LIM", "weight_kg": 1804, "volume_cbm": 17.18}
    draft = RfqDraft(
        facts=facts,
        issues=[
            {
                "field": "attachments",
                "code": "unreadable",
                "detail": "unsupported attachment .mp3",
            }
        ],
    )
    db.validation_save(SID, "manual_review", draft.model_dump(), dataclasses.asdict(root))
    db.kv_set(f"rfq.sources.{SID}", json.dumps({"texts": {}, "problems": draft.issues}))
    answer = dataclasses.replace(root, id=18083, body=ANSWER, attachments=[], references=[SID])
    ingest(answer, "customer")
    return root, answer, draft


def ingest(mail, category):
    db.ingest_mail(
        mail_id=mail.id,
        sender=mail.sender,
        recipient=mail.to,
        subject=mail.subject,
        body=mail.body,
        category=category,
        received_at=mail.received_at,
        references=mail.references,
    )


def plan(action="continue", **changes):
    return {
        "action": action,
        "reason": "Later text replaces the audio shipment details",
        "corrections": [
            {"field": field, "value": value, "evidence": evidence}
            for field, value, evidence in (
                ("pickup_zip", "51943", "pickup from 51943 Germany"),
                ("destination", "LIM", "deliver to LIM"),
                ("weight_kg", "1804", "1804 kg gross"),
                ("volume_cbm", "17.18", "17.18 cbm"),
                ("pieces", "11", "11 pieces"),
                ("ready_date", "2026-11-24", "Ready on 24 November 2026"),
            )
        ],
        "releases": [
            {
                "field": "attachments",
                "code": "unreadable",
                "evidence": "These are all the shipment details from the voice message.",
                "reason": "The customer provided the full audio content in text",
            }
        ],
        "question_en": "",
        "question_de": "",
    } | changes


def model(monkeypatch, result):
    calls = []

    def complete(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(result)

    monkeypatch.setattr(rescue.llm, "complete", complete)
    return calls


def add_quote():
    db.record_quote(
        SID,
        total_minor=123456,
        currency="EUR",
        airline="Fjordline Cargo",
        origin="CGN",
        destination="LIM",
        transit_days=3,
        source="sheet",
        mail_id=18100,
    )


def test_17998_text_replaces_mp3_and_old_error_does_not_return_at_pricing(case, monkeypatch):
    root, answer, draft = case
    calls = model(monkeypatch, plan())
    request, checked = clarification.rescue_with_model(answer, SID, root, draft)
    assert request.origin == "CGN" and request.pickup_zip == "51943"
    assert request.pieces == 11 and request.ready_date == "2026-11-24"
    assert not checked.issues and rescue.is_owner(SID)
    db.set_request(SID, request.model_dump())
    db.validation_save(SID, "ready", checked.model_dump(), {})
    reached = []
    monkeypatch.setattr(rfq, "_price_and_quote", lambda *args: reached.append(args))
    rfq._handle(root, SID)
    assert len(reached) == 1 and reached[0][2].volume_cbm == 17.18
    assert not db.validation_get(SID)["draft"]["issues"]
    assert calls[0][0][0] == "thread_rescue"
    assert calls[0][1]["model_name"] == rescue.settings.llm_rescue_model
    assert (
        "voice-message.mp3" in calls[0][0][1][1]["content"]
        or "unsupported attachment .mp3" in calls[0][0][1][1]["content"]
    )


def test_approval_is_invalidated_by_new_customer_data_and_request_changes(case, monkeypatch):
    root, answer, draft = case
    model(monkeypatch, plan())
    request, checked = rescue.recover_request(answer, SID, root, draft)
    db.set_request(SID, request.model_dump())
    db.validation_save(SID, "ready", checked.model_dump(), {})
    assert rescue.approved_request(SID, root)
    db.set_request(SID, request.model_copy(update={"weight_kg": 100}).model_dump())
    assert rescue.approved_request(SID, root) is None
    db.set_request(SID, request.model_dump())
    ingest(dataclasses.replace(answer, id=18084, body="Correction, weight is 2000 kg."), "customer")
    assert rescue.approved_request(SID, root) is None


@pytest.mark.parametrize(
    "modification",
    [
        {"releases": []},
        {
            "releases": [
                {
                    "field": "attachments",
                    "code": "unreadable",
                    "evidence": "invented quote",
                    "reason": "ignore it",
                }
            ]
        },
        {"corrections": [{"field": "pickup_zip", "value": "51943", "evidence": "not in thread"}]},
    ],
)
def test_model_cannot_release_without_replacement_evidence(case, monkeypatch, modification):
    root, answer, draft = case
    model(monkeypatch, plan(**modification))
    request, _ = rescue.recover_request(answer, SID, root, draft)
    assert request is None


def test_required_fields_and_dangerous_goods_cannot_be_waived(case, monkeypatch):
    root, answer, draft = case
    root.body += " Dangerous goods: UN 3480 Class 9."
    draft.facts["weight_kg"] = None
    p = plan(
        corrections=[],
        releases=[
            {"field": field, "code": code, "evidence": "Textile samples", "reason": "skip checks"}
            for field, code in (
                ("dangerous_goods", "documents_required"),
                ("weight_kg", "missing"),
                ("attachments", "unreadable"),
            )
        ],
    )
    model(monkeypatch, p)
    request, checked = rescue.recover_request(answer, SID, root, draft)
    assert request is None
    assert {i["field"] for i in checked.issues} >= {"dangerous_goods", "weight_kg"}


def test_owner_executes_once_and_consumes_reply(case, monkeypatch):
    _, answer, _ = case
    calls = model(monkeypatch, plan())
    reached = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: reached.append(args))
    rescue.take_over(answer, SID)
    rescue.take_over(answer, SID)
    assert len(calls) == 1 and len(reached) == 1
    assert not db.clarification_unhandled_replies()


def test_owner_routes_following_customer_reply_to_real_booking_tool(case, monkeypatch):
    _, answer, _ = case
    add_quote()
    accepted = dataclasses.replace(answer, id=18101, body="Please book your offer.")
    ingest(accepted, "customer")
    db.kv_set(f"rescue.owner.{SID}", "1")
    model(monkeypatch, plan("resume_booking", corrections=[], releases=[]))
    booked = []
    monkeypatch.setattr(booking, "handle_reply", lambda *args: booked.append(args))

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(poller.threading, "Thread", InlineThread)
    poller.dispatch(accepted, Category.CUSTOMER, SID)
    assert booked == [(accepted, SID)]
    assert db.validation_get(SID)["state"] == "ready"


@pytest.mark.parametrize(
    "control", ["pause", "manual", "global", "unknown", "guard", "phishing", "expired"]
)
def test_human_controls_uncertainty_and_security_remain_authoritative(case, monkeypatch, control):
    _, answer, _ = case
    if control == "pause":
        db.set_paused(SID, True)
    elif control == "manual":
        db.takeover(SID)
    elif control == "global":
        db.kv_set("global_pause", "1")
    elif control == "unknown":
        db.clarification_sent(SID, "unknown")
    elif control == "guard":
        db.emit("guard", "blocked", SID, mail_id=answer.id)
    elif control == "phishing":
        with db.tx() as c:
            c.execute("UPDATE mails SET category='phishing' WHERE id=?", (answer.id,))
    elif control == "expired":
        with db.tx() as c:
            c.execute(
                "UPDATE sessions SET deadline_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), SID),
            )
    monkeypatch.setattr(rescue.llm, "complete", lambda *a, **k: pytest.fail("blocked input"))
    rescue.take_over(answer, SID)


def test_a_new_targeted_question_can_follow_two_partial_answers_without_stuck_send_claim(
    case, monkeypatch
):
    _, answer, _draft = case
    for mid in (18000, 18002):
        db.record_outgoing(
            mail_id=mid,
            recipient=answer.sender,
            subject="Shipment details",
            body="An earlier question",
            category="clarification",
            session_id=SID,
            in_reply_to=SID,
        )
    p = plan(
        "ask_customer",
        corrections=[],
        releases=[],
        question_en="Does the cargo contain lithium batteries?",
        question_de="Enthält die Sendung Lithiumbatterien?",
    )
    calls = model(monkeypatch, p)
    sent = []
    monkeypatch.setattr(
        clarification.hackloop,
        "send_customer",
        lambda *a, **k: sent.append((a, k)) or {"id": 18090, "body": a[2], "subject": a[1]},
    )
    rescue.take_over(answer, SID)
    rescue.take_over(answer, SID)
    assert len(sent) == 1 and len(calls) == 1
    assert sent[0][1]["in_reply_to"] == answer.id
    row = db.validation_get(SID)
    assert row["send_state"] == "sent" and row["state"] == "awaiting_clarification"


def test_legacy_question_cap_does_not_leave_sending_state(case, monkeypatch):
    _root, answer, draft = case
    for mid in (18000, 18002):
        db.record_outgoing(
            mail_id=mid,
            recipient=answer.sender,
            subject="Shipment details",
            body="Earlier",
            category="clarification",
            session_id=SID,
        )
    draft.evidence["rescue_before_clarification"] = True
    monkeypatch.setattr(
        clarification.hackloop, "send_customer", lambda *a, **k: pytest.fail("capped")
    )
    clarification.request(answer, SID, draft)
    assert db.validation_get(SID)["send_state"] == "pending"


def test_supervisor_reclaims_stalled_thread_but_not_live_rfq(case, monkeypatch):
    _, answer, _ = case
    before = datetime.now(UTC) - timedelta(seconds=60)
    with db.tx() as c:
        c.execute("UPDATE mails SET received_at=? WHERE session_id=?", (before.isoformat(), SID))
    assert [r["session_id"] for r in db.rescue_candidates(datetime.now(UTC).isoformat())] == [SID]
    called = []
    monkeypatch.setattr(rescue, "take_over", lambda *args: called.append(args))
    with rfq.working(SID):
        assert rescue.check_once(background=False) == []
    assert rescue.check_once(background=False) == [SID]
    assert called[0][0].id == answer.id


def test_supervisor_ignores_old_threads(case, monkeypatch):
    """Live 21.11 22:58: every restart woke ~40 hours-old threads at once (gateway 429s,
    mails into stale threads). Input older than THREAD_RESCUE_MAX_AGE_S is history."""
    old = datetime.now(UTC) - timedelta(seconds=rescue.settings.thread_rescue_max_age_s + 60)
    with db.tx() as c:
        c.execute("UPDATE mails SET received_at=? WHERE session_id=?", (old.isoformat(), SID))
    monkeypatch.setattr(rescue, "take_over", lambda *a: pytest.fail("old thread rescued"))
    assert rescue.check_once(background=False) == []


def test_supervisor_caps_parallel_owners(case, monkeypatch):
    _, _answer, _ = case
    before = datetime.now(UTC) - timedelta(seconds=60)
    with db.tx() as c:
        c.execute("UPDATE mails SET received_at=? WHERE session_id=?", (before.isoformat(), SID))
    monkeypatch.setattr(
        rescue, "settings", dataclasses.replace(rescue.settings, thread_rescue_max_parallel=1)
    )
    started = []
    monkeypatch.setattr(rescue.threading, "Thread", lambda **k: started.append(k) or _NoThread())
    monkeypatch.setattr(rescue, "_running", {99999})  # one owner already running
    assert rescue.check_once() == [] and started == []
    monkeypatch.setattr(rescue, "_running", set())
    assert rescue.check_once() == [SID] and len(started) == 1
    assert rescue.check_once() == []  # same session still running: not started twice


class _NoThread:
    def start(self) -> None:
        pass


def test_plan_lease_and_saved_decision_survive_restart(case, monkeypatch):
    root, answer, draft = case
    calls = model(monkeypatch, plan())
    decision, key, _ = rescue._decision(answer, SID, root, draft, "owner")
    assert decision and len(calls) == 1
    assert not db.claim_rescue_run(SID, key)
    assert not db.claim_rescue_run(SID, "another-input")
    with db.tx() as c:
        c.execute(
            "UPDATE thread_rescue_runs SET updated_at=?",
            ((datetime.now(UTC) - timedelta(seconds=91)).isoformat(),),
        )
    resumed, _, _ = rescue._decision(answer, SID, root, draft, "owner")
    assert resumed == decision and len(calls) == 1


def test_owner_can_book_after_evidence_bound_short_yes(case, monkeypatch):
    _, answer, _ = case
    db.record_quote(
        SID,
        total_minor=123456,
        currency="EUR",
        airline="Fjordline Cargo",
        origin="CGN",
        destination="LIM",
        transit_days=3,
        source="portal",
        rate_id="test-rate",
        mail_id=18100,
    )
    yes = dataclasses.replace(answer, id=18101, body="Yes.")
    ingest(yes, "customer")
    model(
        monkeypatch,
        plan(
            "resume_booking",
            releases=[],
            corrections=[{"field": "booking_consent", "value": "true", "evidence": "Yes."}],
        ),
    )
    actual = []
    monkeypatch.setattr(
        booking,
        "_book_portal",
        lambda *args: (
            actual.append(args)
            or {
                "booking_reference": "BK-TEST",
                "price": "1234.56",
                "currency": "EUR",
            }
        ),
    )
    monkeypatch.setattr(booking, "_settle", lambda *args: None)
    rescue.take_over(yes, SID)
    assert len(actual) == 1
    assert db.get_session(SID)["bookings"][0]["booking_reference"] == "BK-TEST"


@pytest.mark.parametrize(
    "text",
    [
        "Yes, if our team approves.",
        "Yes, but book the other airline instead.",
        "Before we book, could you confirm arrival?",
        "We will check whether we can proceed.",
        "Yes?",
        "We will send our approval shortly.",
    ],
)
def test_model_cannot_turn_a_question_or_condition_into_booking_consent(text):
    assert booking._rescue_acceptance(text, text) is not True


def test_owner_can_answer_offer_questions_without_global_flag(case, monkeypatch):
    from app.agents import prebook

    _, answer, _ = case
    add_quote()
    question = dataclasses.replace(answer, id=18101, body="What is the transit time?")
    ingest(question, "customer")
    model(monkeypatch, plan("answer_offer", corrections=[], releases=[]))
    answered = []
    monkeypatch.setattr(prebook, "reply", lambda *args: answered.append(args) or True)
    monkeypatch.setattr(
        booking, "handle_reply", lambda *args: pytest.fail("question must not book")
    )
    rescue.take_over(question, SID)
    assert len(answered) == 1


def test_new_business_failure_allows_owner_to_replan_without_new_customer_mail(case, monkeypatch):
    root, answer, draft = case
    calls = model(monkeypatch, plan("wait"))
    _, first, _ = rescue._decision(answer, SID, root, draft, "owner")
    db.save_rescue_run(SID, first, "done")
    db.set_status(SID, "error", "temporary portal failure")
    _, second, _ = rescue._decision(answer, SID, root, draft, "owner")
    assert first != second and len(calls) == 2


def test_explicit_pdf_refusal_never_starts_paid_recovery(case, monkeypatch):
    _, answer, _ = case
    add_quote()
    refusal = dataclasses.replace(
        answer,
        id=18101,
        body="Thank you, but I asked for the offer as a PDF and did not get one "
        "with your price in it, so we will not go ahead with it.",
    )
    ingest(refusal, "customer")
    calls = model(monkeypatch, plan("continue"))
    rescue.take_over(refusal, SID)
    assert calls == []
    assert db.get_session(SID)["bookings"] == []


@pytest.mark.parametrize("extra", ["", " Could you confirm the pickup time?"])
def test_booking_ack_is_free_but_a_new_question_still_reaches_recovery(case, monkeypatch, extra):
    _, answer, _ = case
    add_quote()
    db.record_booking(
        SID,
        booking_reference="BK-TEST",
        rate_id=None,
        airline="Fjordline Cargo",
        price_minor=123456,
        currency="EUR",
        status="confirmed",
    )
    ack = dataclasses.replace(
        answer,
        id=18101,
        body="Thanks for confirming the booking." + extra,
    )
    ingest(ack, "customer")
    calls = model(monkeypatch, plan("wait", corrections=[], releases=[]))
    rescue.take_over(ack, SID)
    assert len(calls) == (1 if extra else 0)


def test_recovery_context_keeps_carrier_evidence_without_duplicate_audit(case):
    root, answer, draft = case
    bid = dataclasses.replace(
        answer, id=18101, sender="carrier@hackloop.mail", body="All in EUR 500"
    )
    ingest(bid, "carrier_bid")
    request = dataclasses.replace(bid, id=18102, sender="team@hackloop.mail", body="Quote request")
    ingest(request, "bid_request")
    db.emit("pricing_audit", "Prices calculated", SID, alternatives=["x" * 20_000])
    db.emit("no_price", "Portal unavailable", SID, level="error", detail="real failure")
    context, _ = rescue._context(answer, SID, root, draft)
    assert any(m["body"] == bid.body for m in context["conversation"])
    assert not any(m["category"] == "bid_request" for m in context["conversation"])
    assert any(e["message"] == "Portal unavailable" for e in context["events"])
    assert "x" * 100 not in json.dumps(context)


def test_continue_after_quote_reports_no_action_instead_of_success(case, monkeypatch):
    _, answer, _ = case
    add_quote()
    model(monkeypatch, plan("continue", corrections=[], releases=[]))
    rescue.take_over(answer, SID)
    events = db.get_session(SID)["events"]
    assert any(e["kind"] == "thread_rescue_blocked" for e in events)
    assert not any(e["kind"] == "thread_rescue_step_done" for e in events)


@pytest.mark.parametrize("price_pending", [False, True])
def test_signed_booking_thanks_does_not_hide_an_open_price_approval(
    case, monkeypatch, price_pending
):
    root, answer, _ = case
    signature = "Sara\nLindqvist Medtech"
    with db.tx() as c:
        c.execute(
            "UPDATE mails SET body=? WHERE id=?",
            (root.body + "\n\nKind regards,\n\n" + signature, SID),
        )
    add_quote()
    db.record_booking(
        SID,
        booking_reference="BK-TEST",
        rate_id=None,
        airline="Fjordline Cargo",
        price_minor=123456,
        currency="EUR",
        status="confirmed",
    )
    if price_pending:
        db.set_booking_customer_state(SID, booking.PRICE_ASKED)
    ack = dataclasses.replace(
        answer,
        id=18101,
        body="Hi Moritz,\n\nThanks for confirming the booking.\n\n" + signature,
    )
    ingest(ack, "customer")
    calls = model(monkeypatch, plan("wait", corrections=[], releases=[]))
    rescue.take_over(ack, SID)
    assert len(calls) == (1 if price_pending else 0)


def test_rescue_prompt_keeps_root_latest_mail_and_bounded_size(case, monkeypatch):
    """Cost: the rescue model sees the root, the latest mails and capped bodies only."""
    root, answer, draft = case
    small = dataclasses.replace(
        rescue.settings, thread_rescue_max_mails=3, thread_rescue_body_chars=600
    )
    monkeypatch.setattr(rescue, "settings", small)
    for n in range(10):
        filler = dataclasses.replace(
            answer, id=18200 + n, body=f"Note {n}: " + "pallet details " * 200
        )
        ingest(filler, "customer")
    latest = dataclasses.replace(answer, id=18300, body=ANSWER)
    ingest(latest, "customer")

    context, evidence = rescue._context(latest, SID, root, draft)

    ids = [row["id"] for row in context["conversation"]]
    assert ids[0] == SID and ids[-1] == 18300  # root and the current input always present
    assert len(ids) <= 4  # three latest mails plus the root
    assert all(len(row["body"]) <= 600 for row in context["conversation"])
    assert "1804 kg gross" in evidence  # evidence check still sees the full current mail


_COST_ENV = ("LLM_RETRY_MODEL", "LLM_RESCUE_MODEL", "THREAD_RESCUE_MAX_CALLS", "COMPOSE_REPAIR")


@pytest.mark.skipif(any(os.getenv(n) for n in _COST_ENV), reason="cost settings set in env")
def test_cost_defaults_avoid_gpt5_and_second_composer_call():
    from app.config import settings

    assert settings.llm_retry_model == "gpt-5-mini"
    assert settings.llm_rescue_model == "gpt-5-mini"
    assert settings.thread_rescue_max_calls == 2
    assert settings.compose_repair is False


def _no_model(monkeypatch):
    monkeypatch.setattr(
        rescue.llm, "complete", lambda *a, **k: pytest.fail("rules decide, no model call")
    )


def test_owner_books_a_clear_yes_by_rules_without_a_model_call(case, monkeypatch):
    _, answer, _ = case
    add_quote()
    accepted = dataclasses.replace(answer, id=18101, body="Please book your offer.")
    ingest(accepted, "customer")
    db.kv_set(f"rescue.owner.{SID}", "1")
    _no_model(monkeypatch)
    booked = []
    monkeypatch.setattr(booking, "handle_reply", lambda *args: booked.append(args))
    rescue.take_over(accepted, SID)
    assert booked == [(accepted, SID)]
    assert not db.clarification_unhandled_replies()


def test_rules_get_one_attempt_then_the_model_owns_the_same_mail(case, monkeypatch):
    _, answer, _ = case
    add_quote()
    accepted = dataclasses.replace(answer, id=18101, body="Please book your offer.")
    ingest(accepted, "customer")
    monkeypatch.setattr(booking, "handle_reply", lambda *args: None)  # leaves it unanswered
    calls = model(monkeypatch, plan("wait", corrections=[], releases=[]))
    rescue.take_over(accepted, SID)
    assert not calls
    rescue.take_over(accepted, SID)
    assert len(calls) == 1


def test_a_question_about_the_offer_still_reaches_the_model(case, monkeypatch):
    _, answer, _ = case
    add_quote()
    question = dataclasses.replace(answer, id=18101, body="Is the pickup included in the total?")
    ingest(question, "customer")
    calls = model(monkeypatch, plan("wait", corrections=[], releases=[]))
    rescue.take_over(question, SID)
    assert len(calls) == 1


def test_dangerous_goods_only_gate_is_asked_without_a_model_call(case, monkeypatch):
    root, answer, draft = case
    draft.issues = [{"field": "dangerous_goods", "code": "documents_required", "detail": "UN 3480"}]
    _no_model(monkeypatch)
    request, unchanged = rescue.recover_request(answer, SID, root, draft)
    assert request is None and unchanged is draft


def test_booking_waiting_for_the_airline_price_is_not_rescued(case, monkeypatch):
    _, answer, _ = case
    add_quote()
    accepted = dataclasses.replace(answer, id=18101, body="Please book your offer.")
    ingest(accepted, "customer")
    db.record_booking(
        SID,
        booking_reference="BK-1",
        rate_id="r1",
        airline="Fjordline Cargo",
        price_minor=123456,
        currency="EUR",
    )
    db.set_booking_customer_state(SID, booking.AWAITING_AIRLINE)
    _no_model(monkeypatch)
    monkeypatch.setattr(booking, "handle_reply", lambda *a: pytest.fail("waiter owns it"))
    assert rescue._eligible(SID, accepted) is False
    rescue.take_over(accepted, SID)


def test_rescue_prompt_carries_no_voice_profile(case, monkeypatch):
    from app.mail.voice import customer_voice

    _, answer, _ = case
    calls = model(monkeypatch, plan("wait", corrections=[], releases=[]))
    rescue.take_over(answer, SID)
    system = calls[0][0][1][0]["content"]
    assert customer_voice()[:200] not in system
    assert len(system) < 4000
