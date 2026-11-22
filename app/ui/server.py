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
from app.mail import webhook
from app.mail.router import address

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
STREAM_POLL_S = 0.5


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    yield


app = FastAPI(title="Austrian Power · Forwarder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

PUBLIC_PATHS = {
    "/api/health",
    webhook.PATH,
}  # health check or separate mandatory HMAC auth
app.add_api_route(webhook.PATH, webhook.receive, methods=["POST"], include_in_schema=False)


@app.middleware("http")
async def basic_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Protect the dashboard and state-changing requests; the webhook uses its own HMAC."""
    public = request.url.path in PUBLIC_PATHS
    if settings.ui_password and not public:
        header = request.headers.get("authorization", "")
        authenticated = False
        read_only = False
        if header.lower().startswith("bearer ") and len(settings.review_token) >= 24:
            # review agents read sessions, mails and events here instead of polling the
            # 5U mailbox with the team key (one poll per 5 s per key, see DEPLOY.md)
            authenticated = read_only = secrets.compare_digest(
                header[7:].strip(), settings.review_token
            )
        elif header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:], validate=True).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                user, pw = "", ""
            authenticated = secrets.compare_digest(
                user, settings.ui_user
            ) and secrets.compare_digest(pw, settings.ui_password)
        if not authenticated:
            response = Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Austrian Power"'},
            )
        elif read_only and request.method not in {"GET", "HEAD"}:
            response = Response(status_code=403, content="Review token is read-only")
        elif (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("x-requested-with") != "XMLHttpRequest"
        ):
            response = Response(status_code=403, content="Cross-site request blocked")
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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


@app.get("/api/problems")
def api_problems(hours: float = 3) -> dict[str, Any]:
    """Errors and warnings of the last hours, grouped (Übersicht)."""
    return db.problems(hours=max(0.25, min(hours, 24)))


@app.post("/api/problems/ack")
def api_problems_ack(body: dict[str, Any]) -> dict[str, int]:
    """Mark problem groups handled: `{"acks": {group_key: last_event_id}}`. A newer event of
    the same group brings it back."""
    acks = body.get("acks")
    if not isinstance(acks, dict) or not acks or len(acks) > 300:
        raise HTTPException(422, "acks must map 1-300 group keys to event ids")
    return {"acked": db.ack_problems(acks)}


@app.get("/api/context")
def api_context() -> dict[str, str]:
    """Lagebild as markdown for the clipboard (manual help from any AI model)."""
    from app.ui import ai_context

    return {"text": ai_context.system_context()}


@app.get("/api/sessions/{session_id}/context")
def api_session_context(session_id: int) -> dict[str, str]:
    from app.ui import ai_context

    text = ai_context.session_context(session_id)
    if text is None:
        raise HTTPException(404, "session not found")
    return {"text": text}


@app.get("/api/sessions/{session_id}/context.zip")
def api_session_context_zip(session_id: int) -> Response:
    """The session context plus the original attachment files (images, scans, voice)."""
    from app.ui import ai_context

    data = ai_context.session_zip(session_id)
    if data is None:
        raise HTTPException(404, "session not found")
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="session-{session_id}-context.zip"'},
    )


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
    # set when the text came from the emergency draft: recorded as our quote so CRM,
    # booking (rate_id) and tracking work as for an agent quote
    quote: dict[str, Any] | None = None
    mail_id: int | None = None


@app.post("/api/sessions/{session_id}/draft")
def api_draft(session_id: int) -> dict[str, Any]:
    """Emergency quote draft for a human (nothing is sent)."""
    from app.ui import drafts

    try:
        return drafts.build(session_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _manual_quote_pdf(
    session_id: int, s: dict[str, Any], q: dict[str, Any]
) -> list[dict[str, str]] | None:
    """QUOTE_PDF for a Notfall-Quote: rendered by code, never blocks the human's send.
    QUOTE_PDF_ON_REQUEST: only when a customer mail in the session asked for a PDF."""
    from app.config import settings
    from app.mail import quote_pdf

    if not settings.quote_pdf:
        return None
    if settings.quote_pdf_on_request and not _customer_asked_for_pdf(s):
        return None
    try:
        doc = quote_pdf.manual_doc(
            session_id,
            s.get("request"),
            q.get("choice") or {},
            int(q["total_minor"]),
            str(q.get("currency") or "EUR"),
        )
        return [quote_pdf.attachment(doc)]
    except Exception as e:  # noqa: BLE001 - a PDF fault must never block the human's send
        db.emit(
            "quote_pdf_failed",
            f"Angebots-PDF nicht erstellt, Antwort geht ohne Anhang raus: {type(e).__name__}",
            session_id,
            level="error",
        )
        return None


def _customer_asked_for_pdf(s: dict[str, Any]) -> bool:
    from app.mail.pdf_request import asks_for_pdf

    customer = address(s.get("customer") or "")
    return any(
        m.get("direction") == "in"
        and customer
        and address(m.get("sender") or "") == customer
        and asks_for_pdf(m.get("body") or "", m.get("subject") or "")
        for m in s.get("mails") or []
    )


@app.post("/api/sessions/{session_id}/reply")
def api_reply(session_id: int, body: ReplyIn) -> dict[str, Any]:
    """Human reply in the customer's thread. Takes the session over first."""
    s = _require(session_id)
    incoming = [m for m in s["mails"] if m["direction"] == "in"]
    if body.mail_id is not None and db.reply_blocked(body.mail_id):
        raise HTTPException(409, "mail is quarantined as phishing: release it first")
    incoming = [m for m in incoming if m.get("category") != "phishing"]
    if not incoming:
        raise HTTPException(409, "no incoming mail to reply to")
    customer = address(s.get("customer") or "")
    if body.mail_id is None:
        customer_mails = [m for m in incoming if customer and address(m["sender"]) == customer]
        if not customer_mails:
            raise HTTPException(409, "no incoming customer mail to reply to")
        parent = customer_mails[-1]
    else:
        parent = next(
            (m for m in incoming if m["id"] == body.mail_id),
            None,
        )
    if parent is None:
        raise HTTPException(422, "selected mail is not an incoming mail in this session")
    customer_reply = bool(customer and address(parent["sender"]) == customer)
    if body.quote is not None and not customer_reply:
        raise HTTPException(422, "customer quotes can only be sent to the session customer")
    try:
        from app import hackloop  # owned by task A; may not exist yet
    except ImportError as e:
        raise HTTPException(503, "mail client (app/hackloop.py) not available yet") from e

    db.takeover(session_id)
    subject = parent["subject"] or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    q = body.quote
    try:
        sender = hackloop.send_customer if customer_reply else hackloop.send
        review_args: dict[str, Any] = (
            {"session_id": session_id, "allow_manual": True} if customer_reply else {}
        )
        if customer_reply and q and q.get("total_minor") and not s.get("quotes"):
            pdf = _manual_quote_pdf(session_id, s, q)
            if pdf:
                review_args["attachments"] = pdf
        sent = sender(
            to=parent["sender"],
            subject=subject,
            body=body.text,
            in_reply_to=parent["id"],
            **review_args,
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
            subject=sent.get("subject", subject),
            body=sent.get("body", body.text),
            category="manual_reply",
            session_id=session_id,
            in_reply_to=parent["id"],
            raw=sent if isinstance(sent, dict) else None,
        )
    if customer_reply:
        db.mark_manual_reply(session_id)
    db.emit(db.Ev.HUMAN_ACTION, f"Manuelle Antwort an {parent['sender']} gesendet", session_id)
    if q and q.get("total_minor") and not s.get("quotes"):
        c = q.get("choice") or {}
        db.record_quote(
            session_id,
            total_minor=int(q["total_minor"]),
            currency=q.get("currency") or "EUR",
            insurance_minor=int(q.get("insurance_minor") or 0),
            air_total_minor=c.get("total_minor"),
            air_currency=c.get("currency"),
            airline=c.get("airline"),
            origin=c.get("origin"),
            destination=c.get("destination"),
            transit_days=c.get("transit_days"),
            source="manual",
            rate_id=c.get("rate_id"),
            valid_until=c.get("valid_until"),
            alternatives=q.get("options"),
            mail_id=sent_id or None,
        )
        # hand back so the booking agent books on the customer's yes (it stays silent in
        # manual mode); the quote is sent, so the agent cannot quote a second time
        db.handback(session_id, "Notfall-Entwurf")
    return {"ok": True, "mail_id": sent_id}


@app.get("/api/quarantine")
def api_quarantine() -> list[dict[str, Any]]:
    """Mails the phishing gate blocked and nobody has released yet."""
    return db.quarantined()


@app.post("/api/mails/{mail_id}/release")
def api_release(mail_id: int) -> dict[str, Any]:
    """Release a quarantined mail: the normal flow runs, the sender is trusted in its thread."""
    from app.mail import poller

    try:
        return poller.release(mail_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except (RuntimeError, TypeError, ValueError) as e:
        raise HTTPException(409, str(e)) from e


MAX_DB_BYTES = 500 * 1024 * 1024


@app.post("/api/admin/import-db")
async def api_import_db(request: Request) -> dict[str, Any]:
    """Replace the database with an uploaded SQLite file (moving the local DB online).
    Only while AGENTS_ENABLED=false, so nothing polls or sends during the swap.
        curl -u team:PW -H 'X-Requested-With: XMLHttpRequest' --data-binary @data/forwarder.db https://<host>/api/admin/import-db
    """
    if settings.agents_enabled:
        raise HTTPException(409, "set AGENTS_ENABLED=false first")
    data = await request.body()
    if not data.startswith(b"SQLite format 3\x00") or len(data) > MAX_DB_BYTES:
        raise HTTPException(400, "not a SQLite database (or too large)")
    return db.import_database(data)


@app.get("/api/deploy-gate")
def api_deploy_gate() -> dict[str, Any]:
    from app.ui import deploy_gate

    return deploy_gate.gate()


@app.get("/api/unanswered")
def api_unanswered() -> list[dict[str, Any]]:
    return db.unanswered_rounds()


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    from app.mail import rate_limit_watch
    from app.mail.outgoing import EMAIL_STYLE_VERSION
    from app.process import STARTED_AT

    return {
        "ok": True,
        "process_started_at": STARTED_AT.isoformat(timespec="seconds"),
        "rfq_watchdog": settings.rfq_watchdog,
        "email_style_version": EMAIL_STYLE_VERSION,
        "build_revision": settings.build_revision,
        "mailbox_429_last_10min": rate_limit_watch.recent(),
        "email_reviewer_model": settings.llm_review_model,
        "email_review_enabled": settings.email_review_enabled,
        "audio_transcription": {
            "groq_configured": bool(settings.groq_api_key),
            "groq_model": settings.groq_audio_model,
            "local_enabled": settings.local_audio_enabled,
            "local_model": settings.local_audio_model,
        },
        "tracking_notice_version": "required-v1",
        "tracking_notice_recovery_enabled": settings.agents_enabled,
        "thread_rescue_enabled": settings.thread_rescue_enabled,
        "reply_workflow_mode": settings.reply_workflow_mode,
        "reply_workers": settings.reply_workers,
        "thread_rescue_model": settings.llm_rescue_model,
        "llm_max_calls_per_session": settings.llm_max_calls_per_session,
        "phishing_gate_mode": settings.phishing_gate_mode,
        "phishing_gate_llm_mode": settings.phishing_gate_llm_mode,
        "last_event": (db.events_after(0, limit=1) or [{}])[0].get("id"),
    }
