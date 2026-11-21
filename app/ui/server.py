"""Sessions UI + CRM dashboard backend.

Design: context/docs/design/aufgabe-c.md (section 6).
Run standalone:  uvicorn app.ui.server:app --port 8000   (no --reload at night)
"""

from __future__ import annotations

import base64
import logging
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.config import settings

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
STREAM_POLL_S = 0.5


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    yield


app = FastAPI(title="Austrian Power · Forwarder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

PUBLIC_PATHS = {"/api/health"}  # for the platform health check


@app.middleware("http")
async def basic_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
    """HTTP Basic auth for everything when UI_PASSWORD is set (always set it online: the UI
    can stop all mail and send replies). EventSource reuses the browser's credentials."""
    if not settings.ui_password or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            user, pw = "", ""
        if secrets.compare_digest(user, settings.ui_user) and secrets.compare_digest(
            pw, settings.ui_password
        ):
            return await call_next(request)
    return Response(
        status_code=401, headers={"WWW-Authenticate": 'Basic realm="Austrian Power"'}
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ------------------------------------------------------------------ read


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    return db.stats()


@app.get("/api/dashboard")
def api_dashboard() -> dict[str, Any]:
    return db.dashboard()


@app.get("/api/overview")
def api_overview(hours: int = 6) -> dict[str, Any]:
    return db.overview(hours=max(0, min(hours, 24 * 7)))


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return db.status_overview()


@app.get("/api/attention")
def api_attention() -> list[dict[str, Any]]:
    return db.attention_items()


@app.get("/api/crm")
def api_crm() -> dict[str, Any]:
    return db.crm()


@app.get("/api/customers/{customer}/sessions")
def api_customer_sessions(customer: str) -> list[dict[str, Any]]:
    return db.customer_sessions(customer)


@app.get("/api/sessions")
def api_sessions(
    status: str | None = None, q: str | None = None, attention: bool = False
) -> list[dict[str, Any]]:
    return db.list_sessions(status=status, q=q, attention=attention)


@app.get("/api/sessions/{session_id}")
def api_session(session_id: int) -> dict[str, Any]:
    s = db.get_session(session_id)
    if not s:
        raise HTTPException(404, "session not found")
    return s


@app.get("/api/unassigned")
def api_unassigned() -> list[dict[str, Any]]:
    return db.list_unassigned()


@app.get("/api/events")
def api_events(since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    """History and polling fallback for the live stream."""
    return db.events_after(since, limit=min(limit, 500))


@app.get("/api/stream", response_class=EventSourceResponse)
def api_stream(request: Request, since: int = 0) -> Iterator[ServerSentEvent]:
    """Live events. Never fails with 4xx/5xx: the browser would stop reconnecting.

    No `event:` field; the client switches on data.kind. Last-Event-ID wins over ?since=.
    Each next() may run on another worker thread, so nothing is held across a yield.
    """
    header = request.headers.get("last-event-id")
    last = int(header) if header and header.isdigit() else since
    yield ServerSentEvent(comment="connected", retry=2000)
    while True:
        try:
            rows = db.events_after(last, limit=200)
        except Exception:
            log.exception("stream poll failed")
            rows = []
        for e in rows:
            last = int(e["id"])
            yield ServerSentEvent(data=e, id=str(last))
        time.sleep(STREAM_POLL_S)


# ------------------------------------------------------------------ actions


def _require(session_id: int) -> dict[str, Any]:
    s = db.get_session(session_id)
    if not s:
        raise HTTPException(404, "session not found")
    return s


@app.post("/api/sessions/{session_id}/pause")
def api_pause(session_id: int) -> dict[str, bool]:
    _require(session_id)
    db.set_paused(session_id, True)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/resume")
def api_resume(session_id: int) -> dict[str, bool]:
    _require(session_id)
    db.set_paused(session_id, False)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/takeover")
def api_takeover(session_id: int) -> dict[str, bool]:
    _require(session_id)
    db.takeover(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/handback")
def api_handback(session_id: int) -> dict[str, bool]:
    _require(session_id)
    db.handback(session_id)
    return {"ok": True}


@app.post("/api/pause-all")
def api_pause_all() -> dict[str, bool]:
    db.set_global_pause(True)
    return {"ok": True}


@app.post("/api/resume-all")
def api_resume_all() -> dict[str, bool]:
    db.set_global_pause(False)
    return {"ok": True}


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@app.post("/api/sessions/{session_id}/reply")
def api_reply(session_id: int, body: ReplyIn) -> dict[str, Any]:
    """Human reply in the customer's thread. Takes the session over first."""
    s = _require(session_id)
    customer_mails = [m for m in s["mails"] if m["direction"] == "in"]
    if not customer_mails:
        raise HTTPException(409, "no incoming mail to reply to")
    parent = customer_mails[-1]
    try:
        from app import hackloop  # owned by task A; may not exist yet
    except ImportError as e:
        raise HTTPException(
            503, "mail client (app/hackloop.py) not available yet"
        ) from e

    db.takeover(session_id)
    subject = parent["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    try:
        sent = hackloop.send(
            to=parent["sender"],
            subject=subject,
            body=body.text,
            in_reply_to=parent["id"],
        )
    except Exception as e:
        db.emit(
            db.Ev.ERROR,
            f"Manuelle Antwort fehlgeschlagen: {e}",
            session_id,
            level="error",
        )
        raise HTTPException(502, f"send failed: {e}") from e
    sent_id = int(sent.get("id", 0)) if isinstance(sent, dict) else 0
    if sent_id:
        db.record_outgoing(
            mail_id=sent_id,
            recipient=parent["sender"],
            subject=subject,
            body=body.text,
            category="manual_reply",
            session_id=session_id,
            in_reply_to=parent["id"],
            raw=sent if isinstance(sent, dict) else None,
        )
    db.mark_manual_reply(session_id)
    db.emit(db.Ev.HUMAN_ACTION, "Manuelle Antwort gesendet", session_id)
    return {"ok": True, "mail_id": sent_id}


MAX_DB_BYTES = 500 * 1024 * 1024


@app.post("/api/admin/import-db")
async def api_import_db(request: Request) -> dict[str, Any]:
    """Replace the database with an uploaded SQLite file (moving the local DB online).
    Only while AGENTS_ENABLED=false, so nothing polls or sends during the swap.
        curl -u team:PW --data-binary @data/forwarder.db https://<host>/api/admin/import-db
    """
    if settings.agents_enabled:
        raise HTTPException(409, "set AGENTS_ENABLED=false first")
    data = await request.body()
    if not data.startswith(b"SQLite format 3\x00") or len(data) > MAX_DB_BYTES:
        raise HTTPException(400, "not a SQLite database (or too large)")
    return db.import_database(data)


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {
        "ok": True,
        "last_event": (db.events_after(0, limit=1) or [{}])[0].get("id"),
    }
