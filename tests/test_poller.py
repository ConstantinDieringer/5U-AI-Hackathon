"""Poller and parser against the real mail format (fixture from A3), no network."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.mail import parse, poller, store, webhook
from app.models import Category


def poll(since_id: int) -> tuple[int, int]:
    """One poll plus a full drain by the queue consumer (in production `webhook.run`)."""
    result = poller.poll_once(since_id)
    while webhook.process_once():
        pass
    return result

FIX = Path(__file__).parent / "fixtures"
REAL = json.loads((FIX / "poll_ratedesk_reply.json").read_text())


def real_mail(
    mid: int, sender: str = "ratedesk@hackloop.mail", **kw: Any
) -> dict[str, Any]:
    m = copy.deepcopy(REAL["messages"][0])
    m.update(id=mid, **{"from": sender}, **kw)
    return m


class FakeMailbox:
    def __init__(self, mails: list[dict[str, Any]]) -> None:
        self.mails = mails
        self.downloads: list[tuple[int, int]] = []
        self.dispatched: list[tuple[int, str]] = []

    def poll(self, since_id: int, limit: int = 200) -> dict[str, Any]:
        new = [m for m in self.mails if m["id"] > since_id][:limit]
        return {"messages": new, "next_since_id": new[-1]["id"] if new else since_id}

    def get_message(self, mid: int) -> dict[str, Any]:
        raise poller.hackloop.HackloopError("GET", f"/m/{mid}", 404, "not found")

    def get_attachment(self, mid: int, att_id: int) -> bytes:
        self.downloads.append((mid, att_id))
        return b"bytes"


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMailbox:
    db.init_db(str(tmp_path / "t.db"))
    monkeypatch.setattr(store, "init", lambda: None)
    monkeypatch.setattr(
        poller,
        "settings",
        dataclasses.replace(poller.settings, inbox_dir=tmp_path / "inbox"),
    )
    fake = FakeMailbox([])
    for name in ("poll", "get_message", "get_attachment"):
        monkeypatch.setattr(poller.hackloop, name, getattr(fake, name))
    # record instead of starting agents: a real RFQ thread outlived its test, tried the
    # gateway and wrote its error into the next test's new database ("database is locked")
    monkeypatch.setattr(
        poller, "dispatch", lambda mail, category, sid: fake.dispatched.append((mail.id, category))
    )
    return fake


def test_parse_real_mail() -> None:
    m = parse.mail_from_api(REAL["messages"][0])
    assert (m.id, m.sender, m.to) == (
        208,
        "ratedesk@hackloop.mail",
        "austrian-power@hackloop.mail",
    )
    assert m.received_at.startswith("2026-11-21T14:41:37")
    assert m.references == [] and m.thread_root == 208
    assert parse.next_since_id(REAL) == 208


def test_parse_rejects_bad_shape() -> None:
    with pytest.raises(parse.MailFormatError):
        parse.mail_from_api({**REAL["messages"][0], "references": "x"})


def test_each_mail_processed_once_across_restart(box: FakeMailbox, monkeypatch) -> None:
    handled: list[int] = []
    real_emit = store.emit
    monkeypatch.setattr(
        store,
        "emit",
        lambda kind, msg, *a, **k: (
            handled.append(kind),
            real_emit(kind, msg, *a, **k),
        ),
    )
    box.mails = [real_mail(301), real_mail(302, sender="anna@acme.hackloop.mail")]
    since, n = poll(store.get_since_id())
    assert (since, n) == (302, 2) and handled.count("routed") == 2
    assert box.dispatched == [(301, Category.PORTAL_REPLY), (302, Category.RFQ)]

    # "restart": since_id is read back from the DB, and even a replay from 0 does nothing
    assert store.get_since_id() == 302
    assert poll(store.get_since_id()) == (302, 0)
    poll(0)
    assert handled.count("routed") == 2


def test_failing_mail_does_not_stop_the_batch(box: FakeMailbox) -> None:
    broken = real_mail(401)
    broken["references"] = "not a list"
    box.mails = [broken, real_mail(402, sender="anna@acme.hackloop.mail")]
    since, _ = poll(0)
    assert since == 402
    kinds = [e["kind"] for e in db.events_after(0)]
    assert "error" in kinds and kinds.count("routed") == 1
    assert db.get_session(402) is not None  # the good mail opened a customer session
    assert box.dispatched == [(402, Category.RFQ)]  # handed on, no agent thread started


def test_attachments_saved_under_mail_id(box: FakeMailbox, tmp_path: Path) -> None:
    att = [
        {"id": 7, "filename": "../../evil tariff.xlsx", "content_type": "x", "size": 5}
    ]
    box.mails = [
        real_mail(501, sender="kestrel-airways@hackloop.mail", attachments=att)
    ]
    poll(0)
    # Tariff downloads now belong to the durable background worker.
    assert db.pending_tariffs()[0]["mail_id"] == 501
    poller.download_attachments(poller.parse.mail_from_api(box.mails[0]))
    assert (tmp_path / "inbox" / "501" / "7-evil_tariff.xlsx").read_bytes() == b"bytes"
    assert box.downloads == [(501, 7)]


def test_router_category_stored(box: FakeMailbox) -> None:
    box.mails = [real_mail(601)]
    poll(0)
    ev = [e for e in db.events_after(0) if e["kind"] == "mail_in"]
    assert ev[0]["payload"]["category"] == Category.PORTAL_REPLY.value


def _session_of(mail_id: int) -> int | None:
    row = db.conn().execute("SELECT session_id FROM mails WHERE id=?", (mail_id,)).fetchone()
    return row[0] if row else None


def test_poll_only_enqueues_and_a_reply_never_precedes_its_root(box: FakeMailbox) -> None:
    """Orphan sessions came from two queue consumers: the poller drained the queue while
    `webhook.run` did too, so a reply could be handled before its root was stored."""
    customer = "anna@acme.hackloop.mail"
    box.mails = [
        real_mail(801, sender=customer),
        real_mail(802, sender=customer, in_reply_to=801, references=[801]),
    ]
    assert poller.poll_once(0) == (802, 2)
    assert box.dispatched == []  # enqueued only
    assert {db.delivery_get(m)["status"] for m in (801, 802)} == {"pending"}

    while webhook.process_once():
        pass
    assert [mid for mid, _ in box.dispatched] == [801, 802]
    assert _session_of(802) == _session_of(801) is not None


def test_bid_runs_in_pool_and_a_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    import threading

    from app.agents import bids, booking

    db.init_db(str(tmp_path / "t.db"))
    started, release = threading.Event(), threading.Event()

    def slow_bid(mail, sid):
        started.set()
        release.wait(5)
        raise ValueError("unreadable offer")

    monkeypatch.setattr(booking, "on_airline_mail", lambda mail: False)
    monkeypatch.setattr(bids, "on_bid_mail", slow_bid)
    mail = parse.mail_from_api(real_mail(901, sender="kestrel-airways@hackloop.mail"))

    poller.dispatch(mail, Category.CARRIER_BID, None)  # returns while the bid still runs
    assert started.wait(2) and not release.is_set()
    release.set()
    for _ in range(100):
        errors = [e for e in db.events_after(0) if e["kind"] == "error"]
        if errors:
            break
        threading.Event().wait(0.02)
    assert errors and "901" in errors[0]["message"]
def _now_mail(mid: int, **kw: Any) -> dict[str, Any]:
    from datetime import UTC, datetime

    return real_mail(mid, received_at=datetime.now(UTC).isoformat(timespec="seconds"), **kw)


def test_parent_comes_from_our_db_without_an_api_call(box: FakeMailbox) -> None:
    """Our bid request is stored without a sender; the mailbox API would answer 404."""
    db.record_outgoing(
        mail_id=950,
        recipient="kestrel-airways@hackloop.mail",
        subject="Rate request [AP-1]",
        body="...",
        category="bid_request",
    )
    parent = poller._parent(950)
    assert parent is not None and parent.id == 950
    assert parent.sender == poller.settings.team_mailbox
    assert parent.to == "kestrel-airways@hackloop.mail"
    assert poller._parent(951) is None  # unknown here and at the API (404)


def test_headerless_re_reply_joins_the_open_session(box: FakeMailbox) -> None:
    customer = "anna@acme.hackloop.mail"
    box.mails = [
        _now_mail(810, sender=customer, subject="Air freight MUC-LAX"),
        _now_mail(811, sender=customer, subject="Re: Air freight MUC-LAX", in_reply_to=None),
        _now_mail(812, sender=customer, subject="Air freight MUC-LAX"),  # no Re: = new request
    ]
    poll(0)
    assert box.dispatched == [
        (810, Category.RFQ),
        (811, Category.CUSTOMER),
        (812, Category.RFQ),
    ]
    assert _session_of(811) == 810 and _session_of(812) == 812


def test_reply_with_unknown_ancestors_opens_no_session_and_needs_a_person(
    box: FakeMailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(poller.screening, "screen", lambda mail, category: None)  # no LLM
    box.mails = [
        _now_mail(820, sender="anna@acme.hackloop.mail", in_reply_to=777, references=[777]),
    ]
    poll(0)
    assert _session_of(820) is None
    assert db.get_session(820) is None  # no orphan session
    items = [i for i in db.attention_items() if i["kind"] == db.ORPHAN_REPLY]
    assert [i["mail_id"] for i in items] == [820]
    assert any(e["kind"] == db.ORPHAN_REPLY for e in db.events_after(0))
