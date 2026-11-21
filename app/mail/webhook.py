"""Signed webhook ingress and one durable inbox shared with fallback polling."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app import db
from app.config import settings
from app.mail import parse

PATH = "/hooks/mailbox"
MAX_BYTES = 1_000_000
_wake = threading.Event()
log = logging.getLogger(__name__)


def signing_secret() -> str:
    if settings.webhook_secret:
        return settings.webhook_secret
    try:
        return settings.webhook_secret_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def verify(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    if not secret or not timestamp.isdecimal() or len(timestamp) > 12:
        return False
    age = time.time() - int(timestamp)
    if age > 300 or age < -60:
        return False
    expected = (
        "sha256="
        + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    )
    return signature.isascii() and hmac.compare_digest(expected, signature)


def enqueue(raw: dict, source: str) -> None:
    mid = parse.message_id(raw)
    db.delivery_enqueue(mid, raw, source)
    _wake.set()


async def receive(request: Request) -> Response:
    secret = signing_secret()
    if not secret:
        raise HTTPException(503, "Mailbox webhook signing secret is not configured")
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "Webhook body too large")
    body = bytes(raw)
    if not verify(
        body,
        request.headers.get("X-Webhook-Timestamp", ""),
        request.headers.get("X-Webhook-Signature", ""),
        secret,
    ):
        raise HTTPException(401, "Invalid webhook signature or timestamp")
    try:
        event = json.loads(body)
        if not isinstance(event, dict):
            raise TypeError("Expected object")
        kind = event.get("event")
        header = request.headers.get("X-Webhook-Event")
        if header and header != kind:
            raise ValueError("Webhook event header mismatch")
        if kind == "ping":
            return Response(status_code=204)
        if kind != "mail.received":
            raise ValueError("Unsupported webhook event")
        message = event.get("message")
        if not isinstance(message, dict):
            raise TypeError("Missing message object")
        mail = parse.mail_from_api(message)
        stamp = datetime.fromisoformat(mail.received_at)
        if (
            mail.id <= 0
            or not mail.sender
            or mail.to.lower() != settings.team_mailbox
            or stamp.tzinfo is None
        ):
            raise ValueError("Invalid mailbox message")
        if any(isinstance(i, bool) for i in mail.references):
            raise ValueError("Invalid references")
        header_id = request.headers.get("X-Webhook-Id")
        if header_id and header_id != str(mail.id):
            raise ValueError("Webhook id mismatch")
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(422, "Malformed mailbox event") from exc
    if not settings.agents_enabled:
        raise HTTPException(503, "Mailbox processing is disabled")
    try:
        await run_in_threadpool(enqueue, message, "webhook")
    except sqlite3.Error as exc:
        raise HTTPException(503, "Mailbox queue is temporarily unavailable") from exc
    return Response(status_code=202)


def process_once() -> bool:
    row = db.delivery_claim(time.time())
    if row is None:
        return False
    mid = row["mail_id"]
    try:
        from app.mail import poller

        poller.handle(json.loads(row["raw_json"]), recover=row["attempts"] > 1)
        db.delivery_state(mid, "done")
    except Exception as exc:  # one malformed mail must not stop delivery
        phase = db.delivery_get(mid)["status"]
        status = (
            "unknown" if phase == "dispatching" else "failed" if row["attempts"] >= 5 else "pending"
        )
        db.delivery_state(
            mid,
            status,
            f"{type(exc).__name__}: {str(exc)[:200]}",
            time.time() + min(60, 2 ** row["attempts"]),
        )
        db.emit(
            "error",
            f"Mail {mid} failed ({status}): {type(exc).__name__}",
            level="error",
            mail_id=mid,
        )
        log.exception("Mail %s delivery failed (%s)", mid, status)
    return True


def run(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if process_once():
                continue
        except Exception:  # transient SQLite faults must not end the worker
            log.exception("Mailbox delivery queue failed")
        _wake.wait(1)
        _wake.clear()
