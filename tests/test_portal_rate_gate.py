"""Portal rate gate: the lock only hands out slots of the per-minute window. Before, get_rate
held it for the whole request and every retry sleep, so four parallel reads of one RFQ ran one
after another (up to 10 s each)."""

import time

import httpx
import pytest

from app import hackloop
from app.pricing import portal_sync


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setattr(hackloop, "_rate_used", 0)
    monkeypatch.setattr(hackloop, "_rate_window_start", float("-inf"))
    monkeypatch.setattr(hackloop, "_rate_blocked_until", 0)
    monkeypatch.setattr(hackloop, "_rate_cap", 10)
    monkeypatch.setattr(portal_sync, "_cache", {})


def _client(monkeypatch, respond) -> None:
    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(transport=httpx.MockTransport(respond), base_url="https://test"),
    )


def test_long_retry_after_fails_fast_but_stays_honored(monkeypatch) -> None:
    _client(monkeypatch, lambda r: httpx.Response(429, headers={"Retry-After": "600"}))
    t0 = time.monotonic()
    with pytest.raises(hackloop.HackloopError):
        hackloop.get_rate("FRA", "JFK", "X", 100)
    assert time.monotonic() - t0 < 2
    assert hackloop._rate_blocked_until > time.monotonic() + 500


def test_fresh_rate_reuses_cache_but_never_the_refused_rate(monkeypatch) -> None:
    calls: list[tuple] = []

    def get_rate(*args, deadline=None):
        calls.append(args)
        assert deadline is not None
        return {
            "rate_id": f"new{len(calls)}",
            "total": "5",
            "valid_until": "2999-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(hackloop, "get_rate", get_rate)
    key = ("FRA", "JFK", "XC", 100.0, 0.0)
    portal_sync._cache[key] = (
        time.monotonic(),
        {"rate_id": "old", "total": "5", "valid_until": "2999-01-01T00:00:00+00:00"},
    )
    deadline = time.monotonic() + 30
    assert portal_sync.fresh_rate(key, stale_rate_id="old", deadline=deadline)["rate_id"] == "new1"
    assert portal_sync.fresh_rate(key, stale_rate_id="old", deadline=deadline)["rate_id"] == "new1"
    assert len(calls) == 1
