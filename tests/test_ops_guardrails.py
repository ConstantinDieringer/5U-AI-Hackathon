"""Read-only dashboard access and repository secret scanning."""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.ui import server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "review-" + "x" * 30


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


def test_review_token_cannot_write(client):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "XMLHttpRequest"}
    r = client.post("/api/sessions/1/reply", json={"text": "x"}, headers=headers)
    assert r.status_code == 403


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


def test_every_mailbox_429_counts_even_when_the_retry_succeeds(monkeypatch, tmp_path):
    """Log 01:47 to 02:00 UTC: 40 mailbox 429s, each hidden by a successful retry, so the
    second-reader alarm (3 in 10 min) never fired."""
    import httpx

    from app import hackloop
    from app.mail import rate_limit_watch

    answers = iter([429, 200, 429, 200, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(answers)
        return httpx.Response(status, headers={"Retry-After": "0"}, json={"messages": []})

    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(base_url="https://x.invalid", transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(hackloop.time, "sleep", lambda s: None)
    db.init_db(str(tmp_path / "rate.db"))
    monkeypatch.setattr(rate_limit_watch, "_hits", type(rate_limit_watch._hits)())
    monkeypatch.setattr(rate_limit_watch, "_last_alert", None)
    for _ in range(3):
        hackloop._request("GET", "/v1/mailbox/messages")
    assert rate_limit_watch.recent() == 3
    alerts = db.conn().execute(
        "SELECT COUNT(*) FROM events WHERE kind = ?", (rate_limit_watch.EVENT_KIND,)
    ).fetchone()[0]
    assert alerts == 1  # the second-reader alarm fires


def test_rejected_poll_without_retry_after_retries_within_seconds(monkeypatch, tmp_path):
    """Live 22.11 07:06 UTC: 21 mailbox 429s in 10 min, every poll rejected. Without a
    Retry-After header each rejected poll slept 30 s twice and the poller another 30 s, so
    the mailbox went ~90 s unread per attempt. Retries now sit just past the 5 s slot."""
    import httpx

    from app import hackloop
    from app.mail import poller, rate_limit_watch

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    sleeps: list[float] = []
    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(base_url="https://x.invalid", transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(hackloop.time, "sleep", sleeps.append)
    db.init_db(str(tmp_path / "rate.db"))
    monkeypatch.setattr(rate_limit_watch, "_hits", type(rate_limit_watch._hits)())
    monkeypatch.setattr(rate_limit_watch, "_last_alert", None)
    try:
        hackloop.poll(0)
    except hackloop.HackloopError as exc:
        failure = exc
    assert failure.status == 429
    assert sleeps and max(sleeps) < 10
    assert failure.retry_after < 10
    assert poller.RATE_LIMIT_RETRY_S < 10
    # counterexample: an explicit Retry-After from 5U is still honoured as given
    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(
            base_url="https://x.invalid",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(429, headers={"Retry-After": "20"}, json={})
            ),
        ),
    )
    sleeps.clear()
    try:
        hackloop.poll(0)
    except hackloop.HackloopError as exc:
        failure = exc
    assert sleeps == [20.0, 20.0] and failure.retry_after == 20.0
