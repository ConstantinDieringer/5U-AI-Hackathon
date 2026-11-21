"""llm.py without network: cost math, per-session budget, logging to llm_calls."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import db, llm


def fake_response(model: str, pt: int, ct: int, text: str = "OK") -> Any:
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct),
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
    )


@pytest.fixture
def sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    db.init_db(str(tmp_path / "t.db"))
    calls: list[dict[str, Any]] = []

    def create(**kw: Any) -> Any:
        calls.append(kw)
        return fake_response("gpt-5-nano-2025-08-07", 1000, 500)

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    return calls


def test_cost_is_integer_micro_usd() -> None:
    # 1000 * 0.05 + 500 * 0.40 = 250 micro-USD
    assert llm.cost_micro_usd("gpt-5-nano", 1000, 500) == 250
    assert llm.cost_micro_usd("gpt-5-nano-2025-08-07", 1000, 500) == 250
    assert llm.cost_micro_usd("gpt-5-2025-08-07", 1000, 500) == 6250
    assert llm.cost_micro_usd("unknown-model", 1000, 500) == 0


def test_call_is_logged_with_cost(sent: list[dict[str, Any]]) -> None:
    assert llm.complete("test", [{"role": "user", "content": "hi"}], session_id=None) == "OK"
    assert sent[0]["reasoning_effort"] == "minimal" and "temperature" not in sent[0]
    ev = [e for e in db.events_after(0) if e["kind"] == "llm_call"]
    assert ev and ev[0]["cost_micro_usd"] == 250


def test_configured_call_limit_in_a_session(sent: list[dict[str, Any]]) -> None:
    msg = [{"role": "user", "content": "hi"}]
    for index in range(llm.MAX_CALLS_PER_SESSION):
        llm.complete(f"call_{index}", msg, session_id=7)
    with pytest.raises(llm.LlmBudgetError):
        llm.complete("again", msg, session_id=7)
    assert len(sent) == llm.MAX_CALLS_PER_SESSION


def test_budget_survives_a_restart(
    sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count lives in the db: restart cannot bypass the configured limit."""
    import importlib

    msg = [{"role": "user", "content": "hi"}]
    for index in range(llm.MAX_CALLS_PER_SESSION):
        llm.complete(f"call_{index}", msg, session_id=8)
    importlib.reload(llm)  # what a restart of run.py does to module state
    monkeypatch.setattr(  # the reloaded client must not reach the network either
        llm._client.chat.completions, "create", lambda **kw: sent.append(kw)
    )
    try:
        with pytest.raises(llm.LlmBudgetError):
            llm.complete("again", msg, session_id=8)
    finally:
        importlib.reload(llm)
    assert len(sent) == llm.MAX_CALLS_PER_SESSION


def test_calls_logged_before_the_counter_count(sent: list[dict[str, Any]]) -> None:
    for index in range(llm.MAX_CALLS_PER_SESSION + 1):
        db.record_llm_call(
            9,
            purpose=f"legacy_{index}",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_micro_usd=0,
            latency_ms=1,
        )
    with pytest.raises(llm.LlmBudgetError):
        llm.complete("again", [{"role": "user", "content": "hi"}], session_id=9)
    assert sent == []


def test_rescue_has_a_separate_persisted_budget_after_normal_budget_is_spent(sent):
    msg = [{"role": "user", "content": "hi"}]
    for index in range(llm.MAX_CALLS_PER_SESSION):
        llm.complete(f"normal_{index}", msg, session_id=10)
    for _ in range(llm.settings.thread_rescue_max_calls):
        assert llm.complete("thread_rescue", msg, session_id=10) == "OK"
    with pytest.raises(llm.LlmBudgetError):
        llm.complete("thread_rescue", msg, session_id=10)
    with pytest.raises(llm.LlmBudgetError):
        llm.complete("normal_again", msg, session_id=10)
    assert db.llm_calls_used(10) == llm.MAX_CALLS_PER_SESSION
    assert len(sent) == llm.MAX_CALLS_PER_SESSION + llm.settings.thread_rescue_max_calls


def test_rescue_spend_does_not_take_away_remaining_composer_calls(sent):
    msg = [{"role": "user", "content": "hi"}]
    llm.complete("thread_rescue", msg, session_id=11)
    for index in range(llm.MAX_CALLS_PER_SESSION):
        llm.complete(f"normal_{index}", msg, session_id=11)
    assert db.llm_calls_used(11) == llm.MAX_CALLS_PER_SESSION
