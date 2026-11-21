"""Poller and parser against the real mail format (fixture from A3), no network."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.mail import parse, poller, store
from app.models import Category

FIX = Path(__file__).parent / "fixtures"
REAL = json.loads((FIX / "poll_ratedesk_reply.json").read_text())


def real_mail(mid: int, sender: str = "ratedesk@hackloop.mail", **kw: Any) -> dict[str, Any]:
    m = copy.deepcopy(REAL["messages"][0])
    m.update(id=mid, **{"from": sender}, **kw)
    return m


class FakeMailbox:
    def __init__(self, mails: list[dict[str, Any]]) -> None:
        self.mails = mails
        self.downloads: list[tuple[int, int]] = []

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
        poller, "settings", dataclasses.replace(poller.settings, inbox_dir=tmp_path / "inbox")
    )
    fake = FakeMailbox([])
    for name in ("poll", "get_message", "get_attachment"):
        monkeypatch.setattr(poller.hackloop, name, getattr(fake, name))
    return fake


def test_parse_real_mail() -> None:
    m = parse.mail_from_api(REAL["messages"][0])
    assert (m.id, m.sender, m.to) == (208, "ratedesk@hackloop.mail", "austrian-power@hackloop.mail")
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
        lambda kind, msg, *a, **k: (handled.append(kind), real_emit(kind, msg, *a, **k)),
    )
    box.mails = [real_mail(301), real_mail(302, sender="anna@acme.hackloop.mail")]
    since, n = poller.poll_once(store.get_since_id())
    assert (since, n) == (302, 2) and handled.count("routed") == 2

    # "restart": since_id is read back from the DB, and even a replay from 0 does nothing
    assert store.get_since_id() == 302
    assert poller.poll_once(store.get_since_id()) == (302, 0)
    poller.poll_once(0)
    assert handled.count("routed") == 2


def test_failing_mail_does_not_stop_the_batch(box: FakeMailbox) -> None:
    broken = real_mail(401)
    broken["references"] = "not a list"
    box.mails = [broken, real_mail(402, sender="anna@acme.hackloop.mail")]
    since, _ = poller.poll_once(0)
    assert since == 402
    kinds = [e["kind"] for e in db.events_after(0)]
    assert "error" in kinds and kinds.count("routed") == 1
    assert db.get_session(402) is not None  # the good mail opened a customer session


def test_attachments_saved_under_mail_id(box: FakeMailbox, tmp_path: Path) -> None:
    att = [{"id": 7, "filename": "../../evil tariff.xlsx", "content_type": "x", "size": 5}]
    box.mails = [real_mail(501, sender="kestrel-airways@hackloop.mail", attachments=att)]
    poller.poll_once(0)
    # Tariff downloads now belong to the durable background worker.
    assert db.pending_tariffs()[0]["mail_id"] == 501
    poller.download_attachments(poller.parse.mail_from_api(box.mails[0]))
    assert (tmp_path / "inbox" / "501" / "7-evil_tariff.xlsx").read_bytes() == b"bytes"
    assert box.downloads == [(501, 7)]


def test_router_category_stored(box: FakeMailbox) -> None:
    box.mails = [real_mail(601)]
    poller.poll_once(0)
    ev = [e for e in db.events_after(0) if e["kind"] == "mail_in"]
    assert ev[0]["payload"]["category"] == Category.PORTAL_REPLY.value
