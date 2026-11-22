"""Real webhook protocol against isolated SQLite, with no live API calls."""

import copy
import dataclasses
import hashlib
import hmac
import json
import sqlite3
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


@pytest.mark.core
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
    assert handled == [502]  # the poller only enqueues; the single consumer processes
    assert webhook.process_once() and not webhook.process_once()
    assert handled == [502, 501]
    assert db.delivery_get(502)["status"] == "done" and db.delivery_get(501)["status"] == "done"


@pytest.mark.core
def test_restart_during_dispatch_does_not_duplicate_effects(client):
    webhook.enqueue(message(502), "webhook")
    db.delivery_state(502, "dispatching")
    db.delivery_recover()
    assert db.delivery_get(502)["status"] == "unknown" and not webhook.process_once()


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


# ---------------------------------------------------------------- current words vs quoted history


@pytest.mark.parametrize(
    "body",
    [
        "Hello,\nplease quote:\nFrom: Munich (MUC)\nTo: Chicago (ORD)\nWeight: 300 kg",
        "Bonjour,\nDe : Paris CDG\nÀ : New York JFK\nPoids : 200 kg",
        "Hola,\nDe: Madrid\nA: Lima\n100 kg",
        "Hallo,\nVon: 80331 München\nNach: JFK\n50 kg",
    ],
)
def test_route_labels_are_not_cut_as_quoted_history(body) -> None:
    from app.mail.content import current_message

    assert current_message(body).text == body


@pytest.mark.parametrize(
    "body",
    [
        "Yes book it.\n\nFrom: Team <austrian-power@hackloop.mail>\nSent: Monday\nTo: x\n\nold",
        "Ok\n\nVon: Austrian Power\nGesendet: Montag\nAn: Kunde\nBetreff: Angebot\n\nalt",
        "Ok\n\nDe : Austrian Power\nEnvoyé : lundi\nÀ : client\nObjet : offre\n\nancien",
    ],
)
def test_real_mail_headers_still_start_history(body) -> None:
    from app.mail.content import current_message

    assert current_message(body).text in ("Yes book it.", "Ok")


def test_history_split_stays_fast_on_many_from_lines() -> None:
    from app.mail.content import current_message

    started = time.perf_counter()
    current_message("hi\n" + "From: x\n" * 5000 + " " * 200_000)
    current_message("\n".join([" " * 1000] * 200))  # took 40 s before: signature regex
    assert time.perf_counter() - started < 0.5
