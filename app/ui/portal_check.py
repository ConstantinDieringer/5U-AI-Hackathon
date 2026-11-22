"""Portal-Check: a person in the UI solves the portal page's sum, we fetch that one rate.

Human in the loop only. The sum image goes to the person as an <img>; nothing here reads or
solves it (code-standards: never bypass the bot challenge). A page check is single-use and
held in memory for PENDING_TTL_S (one replica). The rate is shown to the person and written
to the timeline; it does not enter automatic pricing (the result HTML has no documented shape).
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from app import db, hackloop
from app.config import settings
from app.models import Rfq
from app.pricing.portal_sync import portal_airlines
from app.pricing.pricing import resolve_origin

PENDING_TTL_S = 300.0
MAX_PENDING = 20
ANSWER_RE = re.compile(r"^-?\d{1,4}$")
RATE_ID_RE = re.compile(r"\bR-[A-Za-z0-9]{8,40}\b")  # rate_id format of the portal API


@dataclass(frozen=True)
class Lane:
    origin: str
    destination: str
    weight_kg: str
    volume_cbm: str


@dataclass(frozen=True)
class Pending:
    session_id: int
    airline_code: str
    airline_name: str
    lane: Lane
    challenge: hackloop.PortalUiChallenge
    created: float


_lock = threading.Lock()
_pending: dict[str, Pending] = {}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if data.strip() and not self._skip:
            self.parts.append(data.strip())


def html_text(html: str) -> str:
    """Plain text of the page's result HTML (the UI renders it via textContent only)."""
    p = _Text()
    p.feed(html)
    return " · ".join(p.parts)[:2000]


def _lane(session_id: int) -> tuple[Lane, list[dict[str, str]]]:
    s = db.get_session(session_id)
    if not s:
        raise LookupError("session not found")
    stored = s.get("request")
    if not isinstance(stored, dict):
        raise ValueError("Noch keine erkannte Anfrage in dieser Session")  # noqa: TRY004
    rfq = Rfq.model_validate(stored)
    origin = resolve_origin(rfq)
    if origin is None or not rfq.destination:
        raise ValueError("Abflughafen oder Ziel fehlt in der Anfrage")
    lane = Lane(
        origin=origin,
        destination=rfq.destination.upper(),
        weight_kg=str(rfq.weight_kg),
        volume_cbm=str(rfq.volume_cbm),
    )
    return lane, portal_airlines(lane.origin, lane.destination)


def options(session_id: int) -> dict[str, Any]:
    """Lane and the portal airlines a person can check for this session."""
    lane, airlines = _lane(session_id)
    return {
        "enabled": settings.portal_ui_check,
        "origin": lane.origin,
        "destination": lane.destination,
        "weight_kg": lane.weight_kg,
        "volume_cbm": lane.volume_cbm,
        "airlines": airlines,
    }


def _prune(now: float) -> None:
    for token in [t for t, p in _pending.items() if now - p.created > PENDING_TTL_S]:
        del _pending[token]


def start(session_id: int, airline_code: str) -> dict[str, Any]:
    """Load one fresh page check; returns the sum image for the person."""
    if not settings.portal_ui_check:
        raise ValueError("Portal-Check ist aus (PORTAL_UI_CHECK=false)")
    lane, airlines = _lane(session_id)
    match = next((a for a in airlines if a["code"] == airline_code), None)
    if match is None:
        raise ValueError(
            f"{airline_code} fliegt {lane.origin} nach {lane.destination} nicht im Portal"
        )
    challenge = hackloop.portal_ui_load()
    token = secrets.token_urlsafe(16)
    now = time.monotonic()
    with _lock:
        _prune(now)
        if len(_pending) >= MAX_PENDING:
            raise ValueError("Zu viele offene Portal-Checks, bitte kurz warten")
        _pending[token] = Pending(session_id, match["code"], match["name"], lane, challenge, now)
    db.emit(
        "portal_check_started",
        f"Portal-Check gestartet: {match['name']} {lane.origin} nach {lane.destination}",
        session_id,
        airline=match["code"],
    )
    return {
        "token": token,
        "image": challenge.image,
        "airline": match["name"],
        "expires_in_s": int(PENDING_TTL_S),
    }


def solve(token: str, answer: str) -> dict[str, Any]:
    """Send the person's answer once. Wrong or expired: start a new check."""
    answer = answer.strip()
    if not ANSWER_RE.match(answer):
        raise ValueError("Bitte nur die Zahl eingeben")
    with _lock:
        _prune(time.monotonic())
        pending = _pending.pop(token, None)
    if pending is None:
        raise LookupError("Portal-Check abgelaufen, bitte neu starten")
    lane = pending.lane
    try:
        html = hackloop.portal_ui_submit(
            pending.challenge,
            origin=lane.origin,
            destination=lane.destination,
            airline=pending.airline_code,
            weight_kg=lane.weight_kg,
            volume_cbm=lane.volume_cbm,
            answer=answer,
        )
    except hackloop.HackloopError as exc:
        db.emit(
            "portal_check_failed",
            f"Portal-Check {pending.airline_name}: {exc.detail[:200]}",
            pending.session_id,
            level="warn",
            status=exc.status,
        )
        return {"ok": False, "error": exc.detail[:300]}
    text = html_text(html)
    rate_id = m.group(0) if (m := RATE_ID_RE.search(text)) else None
    db.emit(
        "portal_check_rate",
        f"Portal-Check {pending.airline_name}: {text[:300]}",
        pending.session_id,
        airline=pending.airline_code,
        rate_id=rate_id,
        text=text,
    )
    return {"ok": True, "airline": pending.airline_name, "text": text, "rate_id": rate_id}
