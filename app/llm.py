"""The only way to the 5U LLM gateway (OpenAI models only). Every call is logged with its
tokens and cost in llm_calls (via app/db.py), because the score counts gateway spend.

Entry point: complete(purpose, messages, session_id, max_tokens=...).
"""

from __future__ import annotations

import json
import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from openai import OpenAI

from app import db
from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.6-luna"  # user-selected model; override with LLM_MODEL
MAX_CALLS_PER_SESSION = min(8, settings.llm_max_calls_per_session)
PRICES_FILE = Path(__file__).with_name("llm_prices.json")

_client = OpenAI(
    base_url=f"{settings.base_url}/v1/gateway",
    api_key=settings.api_key,
    timeout=20.0,
    max_retries=1,
)


class LlmBudgetError(RuntimeError):
    """A session tried to make more gateway calls than configured."""


def model() -> str:
    return settings.llm_model or DEFAULT_MODEL


def available_models(*, timeout_s: float = 3.0) -> set[str]:
    """Gateway's actual allowlist; discovery spends no completion/session budget."""
    client = _client.with_options(timeout=timeout_s, max_retries=0)
    return {item.id for item in client.models.list().data}


def _is_reasoning(name: str) -> bool:
    return name.startswith(("gpt-5", "gpt-6", "o"))


def _prices(path: Path = PRICES_FILE) -> dict[str, dict[str, float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def cost_micro_usd(name: str, prompt_tokens: int, completion_tokens: int) -> int:
    """List-price cost in micro-USD (1 USD = 1 000 000), rounded once, here."""
    prices = _prices()
    price = prices.get(name) or next(  # dated names like gpt-5-nano-2025-08-07
        (v for k, v in sorted(prices.items(), key=lambda kv: -len(kv[0])) if name.startswith(k)),
        None,
    )
    if price is None:
        log.warning("no price for model %s: cost logged as 0", name)
        return 0
    usd_per_m = (
        Decimal(str(price["input"])) * prompt_tokens
        + Decimal(str(price["output"])) * completion_tokens
    )  # USD per 1M tokens * tokens = micro-USD
    return int(usd_per_m.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def accounting(name: str, usage: Any) -> dict[str, Any]:
    """Snapshot usage and list rates; cached input and reasoning are subsets, not extras."""
    prices = _prices()
    price = prices.get(name) or next(
        (
            v
            for k, v in sorted(prices.items(), key=lambda kv: -len(kv[0]))
            if re.fullmatch(re.escape(k) + r"-\d{4}-\d{2}-\d{2}", name)
        ),
        None,
    )
    if usage is None:
        return {"status": "usage_missing", "rates": price, "currency": "USD"}
    pt, ct = usage.prompt_tokens, usage.completion_tokens
    pd = getattr(usage, "prompt_tokens_details", None)
    cd = getattr(usage, "completion_tokens_details", None)
    cached = getattr(pd, "cached_tokens", None)
    reasoning = getattr(cd, "reasoning_tokens", None)
    tokens = {"input": pt, "cached_input": cached, "output": ct, "reasoning": reasoning}
    result: dict[str, Any] = {
        "status": "estimated" if price else "price_missing",
        "currency": "USD",
        "tokens": tokens,
        "rates": price,
        "prompt_details": pd.model_dump() if hasattr(pd, "model_dump") else None,
        "completion_details": cd.model_dump() if hasattr(cd, "model_dump") else None,
    }
    if price is None:
        return result
    cached_count = min(pt, max(0, cached or 0))
    components = {
        "input": Decimal(str(price["input"])) * (pt - cached_count),
        "cached_input": Decimal(str(price["cached_input"])) * cached_count,
        "output": Decimal(str(price["output"])) * ct,
    }
    result["components_micro_usd"] = {k: str(v) for k, v in components.items()}
    result["total_micro_usd"] = int(
        sum(components.values()).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    return result


def _reserve(session_id: int | None) -> None:
    """Per-session call budget, counted in the db (survives a restart of run.py)."""
    if session_id is None:
        return
    if db.reserve_llm_call(session_id, MAX_CALLS_PER_SESSION) is None:
        raise LlmBudgetError(f"session {session_id} already made {MAX_CALLS_PER_SESSION} LLM calls")


def complete(
    purpose: str,
    messages: list[dict[str, Any]],
    session_id: int | None,
    max_tokens: int = 200,
    response_format: dict[str, Any] | None = None,
    model_name: str | None = None,
    timeout_s: float | None = None,
) -> str:
    """One chat completion through the gateway. Returns the message text and logs the cost.
    `model_name` overrides LLM_MODEL for configured retry, rescue and compose calls.
    `timeout_s` caps the whole call (no retry), for checks that must not delay a mail."""
    if purpose == "thread_rescue" and session_id is not None:
        if db.reserve_rescue_call(session_id, settings.thread_rescue_max_calls) is None:
            raise LlmBudgetError(f"session {session_id} exhausted its thread rescue budget")
    else:
        _reserve(session_id)
    name = model_name or model()
    kw: dict[str, Any] = {
        "model": name,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if _is_reasoning(name):
        if name.startswith("gpt-5.6"):
            # Luna does not support the legacy 'minimal' effort. Leave room for reasoning
            # as well as schema output; reasoning tokens count against this same limit.
            kw["reasoning_effort"] = "medium" if purpose == "thread_rescue" else "low"
            kw["max_completion_tokens"] = max(
                max_tokens, 4096 if purpose == "thread_rescue" else 2048
            )
        else:
            kw["reasoning_effort"] = "minimal"
    else:
        kw["temperature"] = 0
    if response_format is not None:
        kw["response_format"] = response_format

    start = time.monotonic()
    try:
        client = (
            _client if timeout_s is None else _client.with_options(timeout=timeout_s, max_retries=0)
        )
        resp = client.chat.completions.create(**kw)
    except Exception:
        db.record_llm_call(
            session_id,
            purpose=purpose,
            model=name,
            prompt_tokens=0,
            completion_tokens=0,
            cost_micro_usd=0,
            latency_ms=int((time.monotonic() - start) * 1000),
            accounting={"status": "request_failed", "currency": "USD"},
        )
        raise
    latency_ms = int((time.monotonic() - start) * 1000)

    usage = resp.usage
    pt = usage.prompt_tokens if usage else 0
    ct = usage.completion_tokens if usage else 0
    breakdown = accounting(resp.model or name, usage)
    cost = breakdown.get("total_micro_usd", 0)
    db.record_llm_call(
        session_id,
        purpose=purpose,
        model=resp.model or name,
        prompt_tokens=pt,
        completion_tokens=ct,
        cost_micro_usd=cost,
        latency_ms=latency_ms,
        accounting=breakdown,
    )
    text = resp.choices[0].message.content if resp.choices else None
    return text or ""
