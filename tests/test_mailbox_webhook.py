"""Real webhook protocol against isolated SQLite, with no live API calls."""

import copy
import dataclasses
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, hackloop
from app.mail import poller, store, webhook
from app.ui import server

RAW = json.loads((Path(__file__).parent / "fixtures/poll_ratedesk_reply.json").read_text())[
    "messages"
][0]
SECRET = "unit-test-signing-key"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    cfg = dataclasses.replace(
        webhook.settings,
        webhook_secret=SECRET,
        agents_enabled=True,
        webhook_secret_file=tmp_path / "secret",
    )
    monkeypatch.setattr(webhook, "settings", cfg)
    monkeypatch.setattr(
        server,
        "settings",
        dataclasses.replace(server.settings, ui_password="private-ui"),
    )
    monkeypatch.setattr(poller, "_parent", lambda *a: None)
    monkeypatch.setattr(poller, "dispatch", lambda *a: None)
    with TestClient(server.app) as c:
        yield c


def message(mid):
    return {**copy.deepcopy(RAW), "id": mid}


def signed(event, stamp=None, secret=SECRET):
    body = json.dumps(event).encode()
    timestamp = str(int(time.time()) if stamp is None else stamp)
    sig = (
        "sha256="
        + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    )
    return body, {"X-Webhook-Timestamp": timestamp, "X-Webhook-Signature": sig}


def post(client, event, **kwargs):
    body, headers = signed(event, **kwargs)
    return client.post(webhook.PATH, content=body, headers=headers)


def test_signed_ping_bypasses_ui_auth_but_not_hmac(client):
    assert post(client, {"event": "ping", "team": "austrian-power"}).status_code == 204
    assert client.get("/api/stats").status_code == 401
    assert client.post(webhook.PATH, json={"event": "ping"}).status_code == 401


# 62, not 61: the stamp is whole seconds, so int(time.time()) + 61 can lie only 60.01 s
# ahead and pass the 60 s window (seen once in 120 suite runs)
@pytest.mark.parametrize("offset", [-301, 62])
def test_reject_stale_and_future_signatures(client, offset):
    assert post(client, {"event": "ping"}, stamp=int(time.time()) + offset).status_code == 401


def test_raw_body_signature_is_exact(client):
    body, headers = signed({"event": "ping"})
    assert client.post(webhook.PATH, content=body + b" ", headers=headers).status_code == 401


def test_ack_persists_without_processing_or_advancing_cursor(client, monkeypatch):
    monkeypatch.setattr(
        poller, "handle", lambda *a, **kw: pytest.fail("processed inside HTTP request")
    )
    start = time.monotonic()
    r = post(client, {"event": "mail.received", "message": message(501)})
    assert r.status_code == 202 and time.monotonic() - start < 5
    assert db.delivery_get(501)["status"] == "pending"
    assert store.get_since_id() == 0
    assert post(client, {"event": "mail.received", "message": message(501)}).status_code == 202
    assert db.delivery_get(501)["attempts"] == 0


def test_bad_message_and_header_id_rejected(client):
    raw = {"event": "mail.received", "message": {**message(5), "id": True}}
    assert post(client, raw).status_code == 422
    body, headers = signed({"event": "mail.received", "message": message(5)})
    headers["X-Webhook-Id"] = "6"
    assert client.post(webhook.PATH, content=body, headers=headers).status_code == 422
    assert db.delivery_get(5) is None


def test_no_secret_fails_closed_and_disabled_agents_dont_accept_mail(client, monkeypatch):
    monkeypatch.setattr(
        webhook, "settings", dataclasses.replace(webhook.settings, webhook_secret="")
    )
    assert post(client, {"event": "ping"}).status_code == 503
    monkeypatch.setattr(
        webhook,
        "settings",
        dataclasses.replace(webhook.settings, webhook_secret=SECRET, agents_enabled=False),
    )
    assert post(client, {"event": "mail.received", "message": message(6)}).status_code == 503
    assert post(client, {"event": "ping"}).status_code == 204


def test_hot_loaded_secret_file(client, monkeypatch):
    monkeypatch.setattr(
        webhook, "settings", dataclasses.replace(webhook.settings, webhook_secret="")
    )
    webhook.settings.webhook_secret_file.write_text(SECRET)
    assert post(client, {"event": "ping"}).status_code == 204


def test_queue_failure_is_not_acknowledged(client, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(db, "delivery_enqueue", fail)
    assert post(client, {"event": "mail.received", "message": message(6)}).status_code == 503


def test_out_of_order_and_poll_push_overlap_processed_once(client, monkeypatch):
    handled = []
    monkeypatch.setattr(poller, "dispatch", lambda mail, *args: handled.append(mail.id))
    assert post(client, {"event": "mail.received", "message": message(502)}).status_code == 202
    assert webhook.process_once()
    assert post(client, {"event": "mail.received", "message": message(501)}).status_code == 202
    monkeypatch.setattr(
        hackloop,
        "poll",
        lambda *a, **kw: {
            "messages": [message(501), message(502)],
            "next_since_id": 502,
        },
    )
    assert poller.poll_once(0) == (502, 2)
    assert handled == [502, 501]
    assert db.delivery_get(502)["status"] == "done" and db.delivery_get(501)["status"] == "done"


def test_restart_pending_and_processing_resume(client, monkeypatch):
    calls = []
    monkeypatch.setattr(poller, "dispatch", lambda mail, *a: calls.append(mail.id))
    webhook.enqueue(message(502), "webhook")
    db.delivery_claim(time.time())
    db.delivery_recover()
    assert webhook.process_once() and calls == [502]


def test_crash_after_store_before_dispatch_can_resume(client, monkeypatch):
    calls = []
    monkeypatch.setattr(poller, "dispatch", lambda mail, *a: calls.append(mail.id))
    webhook.enqueue(message(501), "webhook")
    db.delivery_claim(time.time())
    from app.mail import parse
    from app.models import Category

    store.save_mail(parse.mail_from_api(message(501)), Category.PORTAL_REPLY, None)
    db.delivery_recover()
    assert webhook.process_once() and calls == [501]


def test_restart_during_dispatch_does_not_duplicate_effects(client):
    webhook.enqueue(message(502), "webhook")
    db.delivery_state(502, "dispatching")
    db.delivery_recover()
    assert db.delivery_get(502)["status"] == "unknown" and not webhook.process_once()


def test_concurrent_duplicate_ingestion_returns_one_new(client):
    from app.mail.parse import mail_from_api
    from app.models import Category

    results = []

    def save():
        results.append(
            store.save_mail(mail_from_api(message(600)), Category.PORTAL_REPLY, None).is_new
        )

    ts = [threading.Thread(target=save) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == [False, True]


def test_poll_fallback_respects_429_without_busy_loop(client, monkeypatch):
    waits = []

    class Stop:
        done = False

        def is_set(self):
            return self.done

        def wait(self, delay):
            waits.append(delay)
            self.done = True

    monkeypatch.setattr(store, "init", lambda: None)

    def fail(*a, **kw):
        e = hackloop.HackloopError("GET", "/v1/mailbox/messages", 429, "limited")
        e.retry_after = 65
        raise e

    monkeypatch.setattr(hackloop, "poll", fail)
    poller.run(Stop())
    assert waits == [65]


def test_repeated_429_never_grows_the_wait(client, monkeypatch):
    """Rounds 47 and 48 went unanswered: every 429 doubled the wait (30, 60, 120, ... 300 s)
    while an RFQ lives 6 minutes. A rate-limit reply must keep the server's wait."""
    waits = []

    class Stop:
        def is_set(self):
            return len(waits) >= 5

        def wait(self, delay):
            waits.append(delay)

    monkeypatch.setattr(store, "init", lambda: None)

    def limited(*a, **kw):
        e = hackloop.HackloopError("GET", "/v1/mailbox/messages", 429, "limited")
        e.retry_after = 30
        raise e

    monkeypatch.setattr(hackloop, "poll", limited)
    poller.run(Stop())
    assert waits == [30, 30, 30, 30, 30]


def test_other_poll_failures_still_back_off(client, monkeypatch):
    waits = []

    class Stop:
        def is_set(self):
            return len(waits) >= 3

        def wait(self, delay):
            waits.append(delay)

    monkeypatch.setattr(store, "init", lambda: None)

    def down(*a, **kw):
        raise hackloop.HackloopError("GET", "/v1/mailbox/messages", 503, "down")

    monkeypatch.setattr(hackloop, "poll", down)
    poller.run(Stop())
    assert waits == [30, 60, 120]


def test_mailbox_requests_without_retry_header_use_30_second_wait(client, monkeypatch):
    import httpx

    waits = []
    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(
            base_url="https://fake.invalid",
            transport=httpx.MockTransport(lambda r: httpx.Response(429)),
        ),
    )
    monkeypatch.setattr(hackloop.time, "sleep", lambda wait: waits.append(wait))
    with pytest.raises(hackloop.HackloopError) as e:
        hackloop.poll(0)
    assert waits == [30, 30] and e.value.retry_after == 30


def test_oversize_event_rejected(client):
    assert client.post(webhook.PATH, content=b"x" * (webhook.MAX_BYTES + 1)).status_code == 413


def test_subscribe_stores_secret_privately_and_does_not_print_it(client, monkeypatch, capsys):
    from app.mail import webhook_admin

    cfg = dataclasses.replace(webhook.settings, webhook_secret="")
    monkeypatch.setattr(webhook_admin, "settings", cfg)
    monkeypatch.setattr(webhook, "settings", cfg)

    def absent():
        raise hackloop.HackloopError("GET", "/v1/mailbox/webhook", 404, "not subscribed")

    monkeypatch.setattr(hackloop, "webhook_status", absent)
    monkeypatch.setattr(hackloop, "webhook_subscribe", lambda url: {"secret": SECRET})
    webhook_admin.subscribe("https://our-host.invalid/hooks/mailbox")
    assert cfg.webhook_secret_file.read_text() == SECRET
    assert post(client, {"event": "ping"}).status_code == 204
    assert SECRET not in capsys.readouterr().out
    assert list(cfg.webhook_secret_file.parent.glob("webhook-*")) == []


def test_existing_subscription_requires_intentional_rotation(client, monkeypatch):
    from app.mail import webhook_admin

    monkeypatch.setattr(
        webhook_admin,
        "settings",
        dataclasses.replace(webhook.settings, webhook_secret=""),
    )
    monkeypatch.setattr(
        hackloop, "webhook_status", lambda: {"url": "https://existing.invalid/hook"}
    )
    monkeypatch.setattr(
        hackloop,
        "webhook_subscribe",
        lambda *a: pytest.fail("rotated existing subscription"),
    )
    with pytest.raises(ValueError, match="already exists"):
        webhook_admin.subscribe("https://our-host.invalid/hooks/mailbox")


def test_subscribe_preserves_once_only_secret_when_activation_fails(client, monkeypatch):
    from app.mail import webhook_admin

    cfg = dataclasses.replace(webhook.settings, webhook_secret="")
    monkeypatch.setattr(webhook_admin, "settings", cfg)
    monkeypatch.setattr(hackloop, "webhook_status", dict)
    monkeypatch.setattr(hackloop, "webhook_subscribe", lambda url: {"secret": SECRET})

    def fail(*args):
        raise OSError("volume rename failed")

    monkeypatch.setattr(webhook_admin.os, "replace", fail)
    with pytest.raises(RuntimeError, match="Do not subscribe again"):
        webhook_admin.subscribe("https://our-host.invalid/hooks/mailbox", replace=True)
    recovery_files = list(cfg.webhook_secret_file.parent.glob("webhook-*"))
    assert len(recovery_files) == 1 and recovery_files[0].read_text() == SECRET


def test_subscribe_timeout_does_not_rotate_again(client, monkeypatch):
    import httpx

    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("ambiguous response", request=request)

    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(base_url="https://fake.invalid", transport=httpx.MockTransport(timeout)),
    )
    with pytest.raises(hackloop.HackloopError):
        hackloop.webhook_subscribe("https://our-host.invalid/hooks/mailbox")
    assert len(calls) == 1


def test_attachment_failure_resumes_after_storage(client, monkeypatch):
    calls = []

    def download(mail):
        calls.append(mail.id)
        if len(calls) == 1:
            raise OSError("temporary download failure")
        return []

    handled = []
    monkeypatch.setattr(poller, "download_attachments", download)
    monkeypatch.setattr(poller, "dispatch", lambda mail, *a: handled.append(mail.id))
    webhook.enqueue(message(501), "webhook")
    assert webhook.process_once() and not handled
    assert db.delivery_get(501)["status"] == "pending"
    db.delivery_state(501, "pending")  # retry is now due
    assert webhook.process_once() and handled == [501]


def test_poll_cursor_does_not_skip_uncommitted_delivery(client, monkeypatch):
    monkeypatch.setattr(
        hackloop,
        "poll",
        lambda *a, **kw: {"messages": [message(501)], "next_since_id": 501},
    )

    def locked(*args):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(db, "delivery_enqueue", locked)
    with pytest.raises(sqlite3.OperationalError):
        poller.poll_once(0)
    assert store.get_since_id() == 0
