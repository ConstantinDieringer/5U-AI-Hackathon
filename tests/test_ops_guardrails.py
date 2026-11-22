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
