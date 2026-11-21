"""RFQ cost attribution, cache billing and legacy database migration."""

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, llm
from app.ui.server import app


def usage(pt=1000, ct=500, cached=400, reasoning=200):
    return SimpleNamespace(
        prompt_tokens=pt,
        completion_tokens=ct,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def test_cache_discount_and_reasoning_not_double_charged():
    a = llm.accounting("gpt-5-nano-2025-08-07", usage())
    # 600*.05 + 400*.005 + 500*.4 = 232 micro-USD. Reasoning is within 500.
    assert a["total_micro_usd"] == 232
    assert a["tokens"]["reasoning"] == 200
    assert a["components_micro_usd"] == {
        "input": "30.00",
        "cached_input": "2.000",
        "output": "200.0",
    }
    assert a["rates"]["cached_input"] == 0.005


def test_missing_details_prices_and_usage():
    a = llm.accounting(
        "gpt-5-nano", SimpleNamespace(prompt_tokens=1000, completion_tokens=500)
    )
    assert a["total_micro_usd"] == 250
    assert a["tokens"]["cached_input"] is None
    assert llm.accounting("unknown", usage())["status"] == "price_missing"
    assert llm.accounting("gpt-5-nano", None)["status"] == "usage_missing"
    assert llm.accounting("gpt-5-nano-impostor", usage())["status"] == "price_missing"


def test_call_details_persist_and_api_isolates_rfqs(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "costs.db"))
    for sid in (1, 2):
        db.ingest_mail(
            mail_id=sid,
            sender="customer@example.com",
            recipient="team@example.com",
            subject="RFQ",
            body="quote",
            category="rfq",
        )
    response = SimpleNamespace(
        model="gpt-5-nano",
        usage=usage(),
        choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
    )
    monkeypatch.setattr(llm._client.chat.completions, "create", lambda **kw: response)
    llm.complete("extract_rfq", [], 1)
    llm.complete("compose_quote", [], 1)
    llm.complete("extract_rfq", [], 2)
    result = TestClient(app).get("/api/sessions/1").json()
    u = result["llm_usage"]
    assert u["total_micro_usd"] == result["llm_cost_micro_usd"] == 464
    assert u["input_tokens"] == 2000 and u["output_tokens"] == 1000
    assert u["call_count"] == 2
    assert u["calls"][0]["accounting"]["rates"]["input"] == 0.05
    assert db.session_llm_usage(2)["total_micro_usd"] == 232
    assert db.session_llm_usage(999)["total_micro_usd"] == 0


def test_migrate_legacy_records_idempotently(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as c:
        c.executescript(db.SCHEMA)
        c.execute(
            "INSERT INTO llm_calls(session_id,purpose,model,prompt_tokens,completion_tokens,"
            "cost_micro_usd,created_at) VALUES (1,'extract','gpt-5-nano',1000,500,250,'old')"
        )
    db.init_db(path)
    db.init_db(path)
    u = db.session_llm_usage(1)
    assert u["legacy_calls"] == 1 and u["total_micro_usd"] == 250
    db.record_llm_call(
        1,
        purpose="unknown",
        model="unknown",
        prompt_tokens=100,
        completion_tokens=100,
        cost_micro_usd=0,
        latency_ms=1,
        accounting=llm.accounting("unknown", usage()),
    )
    u = db.session_llm_usage(1)
    assert u["total_micro_usd"] is None and u["known_cost_micro_usd"] == 250
    assert u["incomplete_calls"] == 1


def test_failed_call_is_visible_as_incomplete(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "failure.db"))

    def fail(**kwargs):
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(llm._client.chat.completions, "create", fail)
    with pytest.raises(RuntimeError):
        llm.complete("extract_rfq", [], 1)
    u = db.session_llm_usage(1)
    assert u["call_count"] == 1
    assert u["total_micro_usd"] is None
    assert u["calls"][0]["accounting"]["status"] == "request_failed"
