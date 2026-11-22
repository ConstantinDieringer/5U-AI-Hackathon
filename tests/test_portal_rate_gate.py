"""Portal rate gate: the lock only hands out slots of the per-minute window. Before, get_rate
held it for the whole request and every retry sleep, so four parallel reads of one RFQ ran one
after another (up to 10 s each)."""

import threading
import time

import httpx
import pytest

from app import hackloop
from app.agents import booking
from app.pricing import portal_sync


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setattr(hackloop, "_rate_used", 0)
    monkeypatch.setattr(hackloop, "_rate_window_start", 0)
    monkeypatch.setattr(hackloop, "_rate_blocked_until", 0)
    monkeypatch.setattr(hackloop, "_rate_cap", 10)
    monkeypatch.setattr(portal_sync, "_cache", {})


def _client(monkeypatch, respond) -> None:
    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(transport=httpx.MockTransport(respond), base_url="https://test"),
    )


def test_parallel_reads_overlap(monkeypatch) -> None:
    def respond(request):
        time.sleep(0.3)
        return httpx.Response(200, json={"rate_id": "r", "total": "1"})

    _client(monkeypatch, respond)
    threads = [
        threading.Thread(target=hackloop.get_rate, args=("FRA", "JFK", c, 100)) for c in "ABCD"
    ]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert time.monotonic() - t0 < 0.9  # serial would be 1.2 s


def test_full_window_reserves_the_next_one_without_holding_the_lock(monkeypatch) -> None:
    now = time.monotonic()
    monkeypatch.setattr(hackloop, "_rate_window_start", now)
    monkeypatch.setattr(hackloop, "_rate_used", 10)
    slept: list[float] = []
    monkeypatch.setattr(hackloop.time, "sleep", lambda s: slept.append(s))
    hackloop._wait_rate_gate(None)
    assert 59 < slept[0] <= 60
    assert hackloop._rate_used == 1 and hackloop._rate_window_start == pytest.approx(now + 60)
    assert not hackloop._rate_lock.locked()


def test_a_second_full_window_fails_instead_of_sleeping_two_minutes(monkeypatch) -> None:
    monkeypatch.setattr(hackloop, "_rate_window_start", time.monotonic() + 60)
    monkeypatch.setattr(hackloop, "_rate_used", 10)
    with pytest.raises(hackloop.HackloopError):
        hackloop._wait_rate_gate(None)


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


def test_booking_refetch_survives_one_failing_weight(monkeypatch) -> None:
    monkeypatch.setattr(booking, "_airline_code", lambda *a: "XC")
    monkeypatch.setattr(portal_sync, "next_break", lambda code, kg: 300.0)

    def fresh(key, *, stale_rate_id, deadline):
        if key[3] == 100.0:
            raise hackloop.HackloopError("GET", "/v1/portal/rates", 429, "budget")
        return {
            "rate_id": "nb",
            "airline": "X",
            "origin": "FRA",
            "destination": "JFK",
            "chargeable_weight_kg": str(key[3]),
            "total": "9",
            "currency": "EUR",
            "transit_days": 2,
            "valid_until": "2099-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(portal_sync, "fresh_rate", fresh)
    quote = {"origin": "FRA", "destination": "JFK", "airline": "X", "rate_id": "old"}
    assert booking._fresh_rate_id(quote, {"weight_kg": 100, "volume_cbm": 0}) == "nb"
