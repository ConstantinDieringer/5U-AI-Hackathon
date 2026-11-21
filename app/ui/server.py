"""Sessions UI + CRM dashboard backend.

Design: context/docs/design/aufgabe-c.md (section 6).
Run standalone:  uvicorn app.ui.server:app --port 8000   (no --reload at night)
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
STREAM_POLL_S = 0.5


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    yield


app = FastAPI(title="Austrian Power · Forwarder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
        raise HTTPException(503, "mail client (app/hackloop.py) not available yet") from e

    db.takeover(session_id)
    subject = parent["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    try:
        sent = hackloop.send(
            to=parent["sender"], subject=subject, body=body.text, in_reply_to=parent["id"]
        )
    except Exception as e:
        db.emit(db.Ev.ERROR, f"Manuelle Antwort fehlgeschlagen: {e}", session_id, level="error")
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


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True, "last_event": (db.events_after(0, limit=1) or [{}])[0].get("id")}
