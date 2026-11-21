"""The only way to the 5U LLM gateway (OpenAI models only). Every call is logged with its
tokens and cost in llm_calls (via app/db.py), because the score counts gateway spend.

Entry point: complete(purpose, messages, session_id, max_tokens=...).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from openai import OpenAI

from app import db
from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5-nano"  # cheapest allowed model by list price; override with LLM_MODEL
MAX_CALLS_PER_SESSION = 2  # TECH_SPEC section 8: extraction + reply text
PRICES_FILE = Path(__file__).with_name("llm_prices.json")

_client = OpenAI(
    base_url=f"{settings.base_url}/v1/gateway",
    api_key=settings.api_key,
    timeout=20.0,
    max_retries=1,
)
_calls: dict[int, int] = {}  # per-session call count (in memory: resets on restart)
_lock = threading.Lock()


class LlmBudgetError(RuntimeError):
    """A session tried to make more gateway calls than MAX_CALLS_PER_SESSION."""


def model() -> str:
    return settings.llm_model or DEFAULT_MODEL


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


def _reserve(session_id: int | None) -> None:
    if session_id is None:
        return
    with _lock:
        n = _calls.get(session_id, 0)
        if n >= MAX_CALLS_PER_SESSION:
            raise LlmBudgetError(f"session {session_id} already made {n} LLM calls")
        _calls[session_id] = n + 1


def complete(
    purpose: str,
    messages: list[dict[str, Any]],
    session_id: int | None,
    max_tokens: int = 200,
    response_format: dict[str, Any] | None = None,
) -> str:
    """One chat completion through the gateway. Returns the message text and logs the cost."""
    _reserve(session_id)
    name = model()
    kw: dict[str, Any] = {"model": name, "messages": messages, "max_completion_tokens": max_tokens}
    if _is_reasoning(name):
        kw["reasoning_effort"] = "minimal"  # no hidden reasoning tokens: cheaper and faster
    else:
        kw["temperature"] = 0
    if response_format is not None:
        kw["response_format"] = response_format

    start = time.monotonic()
    resp = _client.chat.completions.create(**kw)
    latency_ms = int((time.monotonic() - start) * 1000)

    usage = resp.usage
    pt = usage.prompt_tokens if usage else 0
    ct = usage.completion_tokens if usage else 0
    cost = cost_micro_usd(resp.model or name, pt, ct)
    db.record_llm_call(
        session_id,
        purpose=purpose,
        model=resp.model or name,
        prompt_tokens=pt,
        completion_tokens=ct,
        cost_micro_usd=cost,
        latency_ms=latency_ms,
    )
    text = resp.choices[0].message.content if resp.choices else None
    return text or ""
