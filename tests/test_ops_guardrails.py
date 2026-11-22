"""Process guardrails: 429 detection, read-only review token, deploy check, secrets scan."""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, deploy_check
from app.mail import rate_limit_watch
from app.ui import alerts, server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "review-" + "x" * 30


@pytest.fixture(autouse=True)
def fresh_watch():
    rate_limit_watch.reset()
    yield
    rate_limit_watch.reset()


# ------------------------------------------------------------- mailbox 429 watch


def test_single_429_does_not_alert():
    sent: list[tuple] = []
    assert not rate_limit_watch.record(0.0, emit=lambda *a, **k: sent.append(a))
    assert not rate_limit_watch.record(10.0, emit=lambda *a, **k: sent.append(a))
    assert sent == []


def test_third_429_in_window_alerts_once_per_window():
    sent: list[tuple] = []

    def emit(*a, **k):
        sent.append((a, k))

    for t in (0.0, 20.0, 40.0, 60.0, 80.0):
        rate_limit_watch.record(t, emit=emit)
    assert len(sent) == 1
    (kind, msg), kw = sent[0]
    assert kind == rate_limit_watch.EVENT_KIND and kw["level"] == "error"
    assert "Team-Key" in msg
    # a new window with fresh hits alerts again
    for t in (700.0, 710.0, 720.0):
        rate_limit_watch.record(t, emit=emit)
    assert len(sent) == 2


def test_old_hits_fall_out_of_window():
    sent: list[tuple] = []
    rate_limit_watch.record(0.0, emit=lambda *a, **k: sent.append(a))
    rate_limit_watch.record(10.0, emit=lambda *a, **k: sent.append(a))
    assert not rate_limit_watch.record(700.0, emit=lambda *a, **k: sent.append(a))
    assert rate_limit_watch.recent(700.0) == 1


def test_rate_limit_event_reaches_sos():
    hit = alerts.classify(
        {"kind": "mailbox_rate_limit", "level": "error", "message": "Mailbox-429 3× (23:40 UTC)"}
    )
    assert hit is not None and hit[0] == "mailbox_429"


# ------------------------------------------------------------- read-only review token


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "ops.db"))
    monkeypatch.setattr(
        server,
        "settings",
        dataclasses.replace(server.settings, ui_user="team", ui_password="pw", review_token=TOKEN),
    )
    with TestClient(server.app) as c:
        yield c


def test_review_token_reads(client):
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/sessions", headers=bearer).status_code == 200
    assert client.get("/api/events", headers=bearer).status_code == 200


def test_review_token_cannot_write(client):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "XMLHttpRequest"}
    r = client.post("/api/sessions/1/reply", json={"text": "x"}, headers=headers)
    assert r.status_code == 403


def test_wrong_or_short_review_token_rejected(client, monkeypatch):
    assert client.get("/api/sessions", headers={"Authorization": "Bearer nope"}).status_code == 401
    monkeypatch.setattr(
        server, "settings", dataclasses.replace(server.settings, review_token="short")
    )
    assert client.get("/api/sessions", headers={"Authorization": "Bearer short"}).status_code == 401


def test_health_shows_429_count(client):
    rate_limit_watch.record(emit=lambda *a, **k: None)
    assert client.get("/api/health").json()["mailbox_429_last_10min"] == 1


# ------------------------------------------------------------- deploy check


@pytest.mark.parametrize(
    ("expected", "running", "ok"),
    [
        ("3f9c2ab1d0e4", "3f9c2ab1d0e4ffff", True),
        ("3F9C2AB", "3f9c2ab1d0e4", True),
        ("3f9c2ab", "4f9c2ab1d0e4", False),
        ("3f9c2ab", "unknown", False),
        ("3f9", "3f9c2ab1d0e4", False),
        ("3f9c2ab", "", False),
    ],
)
def test_deploy_check_matches(expected, running, ok):
    assert deploy_check.matches(expected, running) is ok


# ------------------------------------------------------------- secrets scan

SECRET = re.compile(
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"  # OpenAI
    r"|hk_[A-Za-z0-9]{12,}"  # 5U team key
    r"|xox[abpr]-[A-Za-z0-9-]{10,}"  # Slack token
    r"|hooks\.slack\.com/services/T[A-Z0-9]+/"  # Slack webhook
    r"|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"  # GitHub
)


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.splitlines() if p]


@pytest.mark.core
def test_no_secrets_in_repo():
    hits = []
    for path in _tracked_files():
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if SECRET.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert not hits, "possible secret committed: " + ", ".join(hits[:10])
