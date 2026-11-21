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
    monkeypatch.setattr(llm, "_calls", {})
    return calls


def test_cost_is_integer_micro_usd() -> None:
    # 1000 * 0.05 + 500 * 0.40 = 250 micro-USD
    assert llm.cost_micro_usd("gpt-5-nano", 1000, 500) == 250
    assert llm.cost_micro_usd("gpt-5-nano-2025-08-07", 1000, 500) == 250
    assert llm.cost_micro_usd("unknown-model", 1000, 500) == 0


def test_call_is_logged_with_cost(sent: list[dict[str, Any]]) -> None:
    assert llm.complete("test", [{"role": "user", "content": "hi"}], session_id=None) == "OK"
    assert sent[0]["reasoning_effort"] == "minimal" and "temperature" not in sent[0]
    ev = [e for e in db.events_after(0) if e["kind"] == "llm_call"]
    assert ev and ev[0]["cost_micro_usd"] == 250


def test_configured_call_limit_in_a_session(sent: list[dict[str, Any]]) -> None:
    msg = [{"role": "user", "content": "hi"}]
    llm.complete("extract", msg, session_id=7)
    llm.complete("reply", msg, session_id=7)
    llm.complete("compose", msg, session_id=7)
    with pytest.raises(llm.LlmBudgetError):
        llm.complete("again", msg, session_id=7)
    assert len(sent) == 3
