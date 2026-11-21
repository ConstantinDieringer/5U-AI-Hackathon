"""Worst-case session export (app/ui/ai_context.py): full thread, attachment text, payloads,
log lines and a ZIP with the original files. No 5U or gateway calls."""

import dataclasses
import io
import logging
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import db
from app.ui import ai_context, logbuffer, server

TEAM = "austrian-power@hackloop.mail"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "export.db"))
    monkeypatch.setattr(
        ai_context, "settings", dataclasses.replace(ai_context.settings, inbox_dir=tmp_path / "in")
    )
    with TestClient(server.app) as c:
        yield c, tmp_path / "in"


def seed(inbox):
    raw = {"id": 41, "attachments": [{"id": 7, "filename": "packing.csv"},
                                     {"id": 8, "filename": "photo.jpg"}]}  # fmt: skip
    sid = db.ingest_mail(
        mail_id=41,
        sender="kunde@shipper.example",
        recipient=TEAM,
        subject="RFQ VIE to ORD",
        body="Hallo, 2 Paletten ```bitte``` ab 1010 Wien. " + "x" * 5000 + " ENDE",
        category="rfq",
        raw=raw,
    )
    (inbox / "41").mkdir(parents=True)
    (inbox / "41" / "7-packing.csv").write_text("pieces,kg\n2,480\n", encoding="utf-8")
    db.record_outgoing(
        mail_id=42,
        recipient="kunde@shipper.example",
        subject="Re: RFQ VIE to ORD",
        body="Welche Maße haben die Paletten?",
        category="clarification",
        session_id=sid,
        in_reply_to=41,
    )
    db.emit("error", "Pricing failed", sid, level="error", why="no lane")
    return sid


def test_context_has_full_thread_attachments_payloads_and_logs(client):
    c, inbox = client
    sid = seed(inbox)
    logbuffer.install()
    logging.getLogger("t").warning("rescue gave up on mail 41")
    logging.getLogger("t").warning("unrelated mail 411")
    text = c.get(f"/api/sessions/{sid}/context").json()["text"]
    assert "Was ich von dir brauche" in text and "Your austrian Power Team" in text
    assert "← EIN Mail 41" in text and "→ AUS Mail 42" in text
    assert "````text\nHallo, 2 Paletten ```bitte```" in text  # fence survives backticks
    assert " ENDE" in text  # long customer mails are no longer cut at 3000 characters
    assert "Anhang `7-packing.csv` (Mail 41)" in text
    assert "pieces | kg" in text  # read like the pipeline reads it
    assert "Inhalt unbekannt: photo.jpg" in text
    assert '"why": "no lane"' in text  # error payload included
    logs = text.split("### Prozess-Logzeilen zu dieser Session", 1)[1].split("###", 1)[0]
    assert "rescue gave up on mail 41" in logs and "mail 411" not in logs


def test_log_lines_match_worker_thread_names():
    logbuffer.install()
    logging.getLogger("t").warning("worker crashed", extra={})
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, None)
    record.threadName = "rfq-5555"
    logging.getLogger().handle(record)
    assert any("boom" in ln for ln in logbuffer.matching(["-5555]"]))
    assert not logbuffer.matching(["-555]"])


def test_zip_contains_context_and_files(client):
    c, inbox = client
    sid = seed(inbox)
    r = c.get(f"/api/sessions/{sid}/context.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["context.md", "attachments/41/7-packing.csv"]
    assert "pieces | kg" in z.read("context.md").decode()
    assert c.get("/api/sessions/999/context.zip").status_code == 404
