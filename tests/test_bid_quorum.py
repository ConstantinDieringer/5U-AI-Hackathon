"""bids.wait with BID_GRACE_S: production replies (21.11, 361 carrier mails) arrive 34 to 45 s
after our request, every later reply belonged to a later request. One carrier that never
answers held the quote to the pricing deadline. With a quorum and a grace period the wait ends
once the cluster is in; BID_GRACE_S=0 keeps today's behaviour."""

import dataclasses
import threading
import time
from decimal import Decimal

import pytest

from app import config, db
from app.agents import bids
from app.pricing.reference import Carrier

SID = 991


def _carrier(n: int, kind: str = "airline") -> Carrier:
    return Carrier(f"C{n}", kind, f"c{n}@hackloop.mail", ("FRA",))  # type: ignore[arg-type]


@pytest.fixture()
def world(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(db, "emit", lambda kind, *a, **k: events.append(kind))

    def setup(grace: float, quorum: int, carriers: list[Carrier]) -> list[str]:
        monkeypatch.setattr(
            config,
            "settings",
            dataclasses.replace(config.settings, bid_grace_s=grace, bid_quorum=quorum),
        )
        with bids._cond:
            bids._open[SID] = bids._Request(asked={c.email: c for c in carriers})
        return events

    yield setup
    bids.close(SID)


def _answer(carrier: Carrier, after_s: float, decline: bool = False) -> None:
    def run() -> None:
        time.sleep(after_s)
        with bids._cond:
            req = bids._open[SID]
            if decline:
                req.declined.add(carrier.email)
            else:
                req.offers.append(bids.Offer(carrier, Decimal(100), 2.0, 1))
            req.last_answer_at = time.monotonic()
            bids._cond.notify_all()

    threading.Thread(target=run, daemon=True).start()


def test_silent_carrier_no_longer_holds_the_quote(world) -> None:
    cs = [_carrier(i) for i in range(3)]
    events = world(0.3, 2, cs)
    _answer(cs[0], 0.05)
    _answer(cs[1], 0.1, decline=True)
    t0 = time.monotonic()
    got = bids.wait(SID, 5.0)
    assert time.monotonic() - t0 < 1.5
    assert [o.carrier for o in got] == [cs[0]]
    assert events == ["bid_quorum"]


def test_reply_inside_grace_restarts_it_and_is_kept(world) -> None:
    cs = [_carrier(i) for i in range(4)]
    world(0.3, 2, cs)
    _answer(cs[0], 0.05)
    _answer(cs[1], 0.1)
    _answer(cs[2], 0.3)  # before the grace period after the second reply ends
    assert {o.carrier for o in bids.wait(SID, 5.0)} == set(cs[:3])


def test_grace_zero_waits_for_everyone_as_before(world) -> None:
    cs = [_carrier(i) for i in range(3)]
    events = world(0.0, 2, cs)
    _answer(cs[0], 0.02)
    _answer(cs[1], 0.04)
    t0 = time.monotonic()
    bids.wait(SID, 0.6)
    assert time.monotonic() - t0 >= 0.55
    assert events == []


def test_quorum_needs_an_offer_of_every_asked_kind(world) -> None:
    air = [_carrier(i) for i in range(3)]
    truck = _carrier(9, "trucker")
    world(0.1, 2, air + [truck])
    _answer(air[0], 0.02)
    _answer(air[1], 0.04)
    _answer(truck, 0.5)
    got = bids.wait(SID, 3.0)
    assert truck in {o.carrier for o in got}


def test_all_answered_returns_without_grace(world) -> None:
    cs = [_carrier(i) for i in range(2)]
    events = world(5.0, 2, cs)
    _answer(cs[0], 0.02)
    _answer(cs[1], 0.04)
    t0 = time.monotonic()
    bids.wait(SID, 10.0)
    assert time.monotonic() - t0 < 1.0
    assert events == []
