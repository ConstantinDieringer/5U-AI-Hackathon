"""Unfamiliar customer wording reaches a model decision and the real pricing/send path."""

import dataclasses
import json
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app import db, llm
from app.agents import bids, clarification, rescue, rfq
from app.models import Mail, RateOption, Rfq, RfqDraft
from app.pricing.portal_sync import PortalResult


@pytest.fixture
def flow(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "autonomy.db"))
    body = (
        "From STR to MEL. We deliver it to STR ourselves. Ready on 26 November 2026. "
        "6 pieces, gross weight 1841 kg, total volume 4.63 cbm. "
        "Treat 2 December 2026 as the last acceptable landing day. "
        "Go easy on the budget once that is satisfied."
    )
    mail = Mail(
        23465,
        23465,
        "customer@hackloop.mail",
        "team@hackloop.mail",
        "RFQ",
        body,
        datetime.now(UTC).isoformat(),
    )
    db.ingest_mail(
        mail_id=mail.id,
        sender=mail.sender,
        recipient=mail.to,
        subject=mail.subject,
        body=mail.body,
        category="rfq",
        received_at=mail.received_at,
    )
    request = Rfq(
        origin="STR",
        destination="MEL",
        weight_kg=1841,
        volume_cbm=4.63,
        pieces=6,
        ready_date="2026-11-26",
        arrival_deadline="2026-12-02",
        preference="fastest",
    )
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *a: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *a: {})
    monkeypatch.setattr(rfq, "portal_result", lambda *a, **k: PortalResult([], [], {}))
    monkeypatch.setattr(
        rfq,
        "_sheets",
        lambda *a: [
            RateOption("Fast", "STR", "MEL", 3, 804000, "EUR", 804000, "sheet"),
            RateOption("Economy", "STR", "MEL", 4, 606000, "EUR", 606000, "sheet"),
        ],
    )
    monkeypatch.setattr(bids, "wait", lambda *a, **k: None)
    monkeypatch.setattr(rfq, "_quote_pdf", lambda *a, **k: None)
    monkeypatch.setattr("app.pricing.selection.pricing_day", lambda: date(2026, 11, 22))
    monkeypatch.setattr(rfq, "compose_quote_text", lambda *a: "Verified offer")
    monkeypatch.setattr(
        rfq,
        "settings",
        dataclasses.replace(rfq.settings, rfq_validation_mode="enforce", price_adjust_pct=0),
    )
    monkeypatch.setattr(
        clarification,
        "settings",
        dataclasses.replace(
            clarification.settings, rfq_validation_mode="enforce", clarify_once=False
        ),
    )
    sent = []

    def send(to, subject, body, **kwargs):
        sent.append(body)
        return {"id": 24000, "body": body, "subject": subject}

    monkeypatch.setattr(rfq.hackloop, "send_customer", send)
    return mail, request, sent


def decision(evidence):
    return rescue.Plan(
        action="continue",
        reason="Latest landing day is a limit, budget is the goal",
        corrections=[
            rescue.Correction(field="arrival_deadline", value="2026-12-02", evidence=evidence),
            rescue.Correction(
                field="preference",
                value="cheapest",
                evidence="Go easy on the budget once that is satisfied.",
            ),
        ],
        releases=[],
        question_en="",
        question_de="",
    )


@pytest.mark.core
@pytest.mark.parametrize("invented", [False, True])
def test_semantic_recovery_before_question_quotes_only_evidenced_constraints(
    flow, monkeypatch, invented
):
    mail, request, sent = flow
    calls = []

    def complete(purpose, messages, sid, **kwargs):
        calls.append((purpose, json.loads(messages[1]["content"])))
        evidence = (
            "The customer confirmed 2 December"
            if invented
            else ("Treat 2 December 2026 as the last acceptable landing day.")
        )
        return decision(evidence).model_dump_json()

    monkeypatch.setattr(llm, "complete", complete)
    rfq._price_and_quote(mail, mail.id, request, "STR", time.time(), "enforce")
    session = db.get_session(mail.id)
    assert len(sent) == 1 and len(calls) == 1
    assert "insurance" in calls[0][1]["request_fields"]["properties"]
    if invented:
        assert not session["quotes"] and session["first_reply_at"] is None
        assert session["validation"]["state"] == "awaiting_clarification"
    else:
        assert session["quotes"][0]["airline"] == "Economy"
        assert session["quotes"][0]["total_minor"] == 606000
        assert session["request"]["preference"] == "cheapest"


@pytest.mark.core
def test_recovery_can_correct_new_request_fields_and_remove_obsolete_values():
    evidence = "No insurance now. Pickup in Hamburg."
    plan = rescue.Plan(
        action="continue",
        reason="Customer changed service",
        corrections=[
            rescue.Correction(field="insurance", value="null", evidence="No insurance now."),
            rescue.Correction(field="pickup_city", value="Hamburg", evidence="Pickup in Hamburg."),
            rescue.Correction(field="pickup_assumed", value="false", evidence=evidence),
            rescue.Correction(field="weight_kg", value="-20", evidence=evidence),
        ],
        releases=[],
        question_en="",
        question_de="",
    )
    assert rescue._corrected(RfqDraft(), plan, rescue._normal(evidence)) == {
        "insurance": None,
        "pickup_city": "Hamburg",
    }


@pytest.mark.core
def test_rejected_plan_gets_tool_feedback_for_a_fresh_decision(flow, monkeypatch):
    mail, request, _ = flow
    db.set_request(mail.id, request.model_dump())
    contexts = []

    def complete(purpose, messages, sid, **kwargs):
        contexts.append(json.loads(messages[1]["content"]))
        plan = decision("Treat 2 December 2026 as the last acceptable landing day.")
        if len(contexts) == 1:
            plan.corrections = []  # tool rejects a plan that leaves the date unproven
        return plan.model_dump_json()

    monkeypatch.setattr(llm, "complete", complete)
    assert rescue.take_over(mail, mail.id)
    assert not db.get_session(mail.id)["quotes"]
    assert rescue.take_over(mail, mail.id)
    assert db.get_session(mail.id)["quotes"][0]["airline"] == "Economy"
    feedback = next(e for e in contexts[1]["events"] if e["kind"] == "thread_rescue_blocked")
    assert {"field": "arrival_deadline", "code": "unproven"} in feedback["issues"]
    assert len(contexts) == 2


@pytest.mark.core
def test_luna_uses_supported_reasoning_and_accounts_for_its_price(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "model.db"))
    captured = []
    response = SimpleNamespace(
        model="gpt-5.6-luna",
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: captured.append(kwargs) or response)
        )
    )
    monkeypatch.setattr(llm, "_client", client)
    llm.complete("thread_rescue", [], None, max_tokens=1800, model_name="gpt-5.6-luna")
    assert captured[0]["reasoning_effort"] == "medium"
    assert captured[0]["max_completion_tokens"] >= 4096
    assert llm.cost_micro_usd("gpt-5.6-luna", 1_000_000, 1_000_000) == 1_400_000
