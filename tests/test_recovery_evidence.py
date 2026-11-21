"""Recovery must see original evidence, learn from failures and keep its own budget."""

import dataclasses
import json
from decimal import Decimal

from app import db, llm
from app.agents import clarification
from app.mail.rfq_validation import numeric_facts, validate
from app.models import Mail, RfqDraft
from tests.test_llm import fake_response
from tests.test_origin_clarification_fallback import setup_mail


def test_session_16288_table_header_cannot_read_pieces_as_weight():
    table = (
        "Pieces | Width (cm) | Length (cm) | Height (cm) | Gross weight (kg)\n"
        "11 | 100 | 120 | 33 | 1674\n\n"
        "Total pieces: | 11\nTotal gross weight (kg): | 1674\n"
        "Total volume (cbm): | 4.36"
    )
    assert numeric_facts(table)["weight_kg"] == Decimal(1674)
    assert "weight_kg" not in numeric_facts(table.split("\n\n")[0])


def test_rescue_uses_raw_attachments_feedback_and_high(tmp_path, monkeypatch):
    root, request = setup_mail(
        tmp_path,
        monkeypatch,
        "enforce",
        "Please quote to DEL. Gross weight: 736 kg. Volume: 6.88 cbm. Pieces: 11.",
        "Quote to DEL",
    )
    _, draft = validate(root, request)
    draft.evidence["attachment_texts"] = {"packing.xlsx": "Departure airport: DUS"}
    calls = []

    def complete(purpose, messages, *args, **kwargs):
        prompt = json.loads(messages[1]["content"])
        assert prompt["attachment_texts"]["packing.xlsx"] == "Departure airport: DUS"
        assert kwargs["model_name"] == "gpt-5.6-sol"
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["max_tokens"] >= 6000
        calls.append(prompt)
        return json.dumps(
            {
                "corrections": [
                    {
                        "field": "origin",
                        "value": "DUS",
                        "evidence": "invented evidence"
                        if len(calls) == 1
                        else "Departure airport: DUS",
                    }
                ]
            }
        )

    monkeypatch.setattr(clarification.llm, "complete", complete)
    result, checked = clarification.rescue_with_model(root, root.id, root, draft)
    assert result.origin == "DUS" and not checked.issues
    assert len(calls) == 2
    assert calls[1]["previous_attempts"][0]["remaining_errors"]
    assert clarification.rescue_with_model(root, root.id, root, draft)[0] is None
    assert len(calls) == 2  # duplicate workers/restarts do not spend the same budget again


def test_rescue_budget_remains_available_after_normal_calls(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "budget.db"))
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return fake_response("gpt-5.6-sol", 100, 100)

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    messages = [{"role": "user", "content": "test"}]
    for _ in range(llm.MAX_CALLS_PER_SESSION):
        llm.complete("normal", messages, 7)
    llm.complete(
        "clarification_rescue", messages, 7, model_name="gpt-5.6-sol", reasoning_effort="high"
    )
    assert calls[-1]["reasoning_effort"] == "high"
    assert len(calls) == llm.MAX_CALLS_PER_SESSION + 1


def test_promised_dg_documents_do_not_clear_gate(tmp_path, monkeypatch):
    root, request = setup_mail(tmp_path, monkeypatch, "enforce")
    draft = RfqDraft(
        facts=request.model_dump(),
        issues=[
            {
                "field": "dangerous_goods",
                "code": "documents_required",
            }
        ],
        evidence={"attachment_texts": {"packing.xlsx": "Dangerous goods: UN1760"}},
    )
    db.validation_save(
        root.id, "awaiting_clarification", draft.model_dump(), dataclasses.asdict(root)
    )
    assert db.clarification_claim(root.id)
    db.clarification_sent(root.id, "sent", 2)
    monkeypatch.setattr(clarification, "rescue_with_model", lambda m, s, r, d: (None, d))
    captured = []
    monkeypatch.setattr(clarification, "request", lambda m, s, d: captured.append(d))
    reply = Mail(
        3,
        root.id,
        root.sender,
        root.to,
        "Re: quote",
        "We will send the dangerous goods documents shortly.",
        root.received_at,
    )
    assert clarification.handle_reply(reply, root.id)
    assert any(i["field"] == "dangerous_goods" for i in captured[0].issues)
