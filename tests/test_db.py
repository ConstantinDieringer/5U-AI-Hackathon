"""Tests for app/db.py. No real 5U calls. Run: python -m pytest -q"""

import threading

import pytest

from app import db

CUSTOMER = "anna@shipper.example"
TEAM = "austrian-power@hackloop.mail"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
    yield


def rfq(mail_id=100, **kw):
    args = {
        "mail_id": mail_id,
        "sender": CUSTOMER,
        "recipient": TEAM,
        "subject": "RFQ FRA-DFW 40kg",
        "body": "Hi, cheapest please",
        "category": "rfq",
    }
    args.update(kw)
    return db.ingest_mail(**args)


def test_claim_blocked_by_pause_takeover_and_global_pause():
    a, b, c = rfq(1), rfq(2), rfq(3)
    db.set_paused(a, True)
    db.takeover(b)
    assert db.claim_quote_send(a) is False
    assert db.claim_quote_send(b) is False
    db.set_global_pause(True)
    assert db.claim_quote_send(c) is False and db.is_paused(c)
    db.set_global_pause(False)
    assert db.claim_quote_send(c) is True


def test_claim_is_atomic_under_race():
    sid = rfq()
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(db.claim_quote_send(sid))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1
