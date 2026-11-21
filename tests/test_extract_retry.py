"""Extraction retry: an unusable first answer gets one more try with LLM_RETRY_MODEL."""

import dataclasses
import json

import httpx
import openai
import pytest

from app import db
from app.agents import rfq
from app.config import settings
from app.models import Mail

BODY = "Hi,\n\nPlease quote the cheapest option FRA to LOS, 3 pieces, 420 kg, 1.9 cbm.\n\nMira"
GOOD = {
    "origin": "FRA", "pickup_zip": None, "destination": "LOS", "weight_kg": 420,
    "volume_cbm": 1.9, "pieces": 3, "preference": "cheapest", "ready_date": None,
    "language": "en", "customer_name": "Mira",
}  # fmt: skip
BAD = {**GOOD, "weight_kg": 0}  # what gpt-5-nano returned for mail 1967


@pytest.fixture
def mail(tmp_path, monkeypatch) -> Mail:
    db.init_db(str(tmp_path / "t.db"))
    # This file exercises the legacy extraction retry contract; enforce-mode drafts
    # and their clarification lifecycle are covered by test_rfq_validation_gate.
    monkeypatch.setattr(
        rfq, "settings", dataclasses.replace(rfq.settings, rfq_validation_mode="shadow")
    )
    return Mail(7, 7, "nordlicht-audio@hackloop.mail", "x", "Air freight quote to LOS", BODY,
                "2026-11-21T15:49:35+00:00")  # fmt: skip


def _stub(monkeypatch, answers: list) -> list[dict]:
    """llm.complete returns (or raises) the answers in order; returns the recorded calls."""
    calls: list[dict] = []

    def complete(purpose, messages, session_id, **kw):
        calls.append({"purpose": purpose, "messages": messages, **kw})
        a = answers[len(calls) - 1]
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(rfq.llm, "complete", complete)
    return calls


def test_good_first_answer_makes_one_call(mail, monkeypatch) -> None:
    calls = _stub(monkeypatch, [json.dumps(GOOD)])
    r = rfq.extract_rfq(mail, 7)
    assert r is not None and r.weight_kg == 420
    assert len(calls) == 1 and calls[0].get("model_name") is None


def test_unusable_answer_is_retried_with_retry_model(mail, monkeypatch) -> None:
    calls = _stub(monkeypatch, [json.dumps(BAD), json.dumps(GOOD)])
    r = rfq.extract_rfq(mail, 7)
    assert r is not None and r.weight_kg == 420
    assert len(calls) == 2
    assert calls[1]["model_name"] == settings.llm_retry_model
    assert calls[1]["purpose"] == "extract_rfq_retry"
    assert "weight_kg" in calls[1]["messages"][-1]["content"]  # the error is fed back


def test_invalid_json_is_retried(mail, monkeypatch) -> None:
    calls = _stub(monkeypatch, ["not json", json.dumps(GOOD)])
    assert rfq.extract_rfq(mail, 7) is not None and len(calls) == 2


def test_gateway_error_on_first_call_is_retried(mail, monkeypatch) -> None:
    err = openai.APITimeoutError(httpx.Request("POST", "https://x"))
    calls = _stub(monkeypatch, [err, json.dumps(GOOD)])
    assert rfq.extract_rfq(mail, 7) is not None and len(calls) == 2


def test_both_unusable_gives_none_after_two_calls(mail, monkeypatch) -> None:
    calls = _stub(monkeypatch, [json.dumps(BAD), json.dumps(BAD)])
    assert rfq.extract_rfq(mail, 7) is None and len(calls) == 2


def test_retry_respects_the_session_budget(mail, monkeypatch) -> None:
    calls = _stub(
        monkeypatch, [json.dumps(BAD), rfq.llm.LlmBudgetError("no calls left")]
    )
    assert rfq.extract_rfq(mail, 7) is None and len(calls) == 2
