"""Deploy gate: is a push to main (which restarts the only live instance) safe right now?

Safe when the newest RFQ is settled (our quote or reply is out, or its 360 s window is over)
and the next RFQ is more than DEPLOY_GATE_MARGIN_S away. RFQs arrive about every 6 min 4 s;
the spacing is the median gap of recent arrivals (RFQ_INTERVAL_S until enough are known) and
the phase is the newest arrival, so nothing about the schedule is hardcoded.
"""

from __future__ import annotations

import itertools
import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from app import db
from app.config import settings

RFQ_LIFETIME_S = 360
MIN_GAPS = 3  # gaps needed before the measured spacing replaces RFQ_INTERVAL_S


def interval_s(arrivals: list[datetime]) -> float:
    """Median spacing of consecutive arrivals. A missed round shows as a double gap and is
    split into whole intervals; follow-up threads seconds apart are ignored."""
    nominal = settings.rfq_interval_s
    ordered = sorted(arrivals)
    gaps = []
    for a, b in itertools.pairwise(ordered):
        gap = (b - a).total_seconds()
        if gap < 60:
            continue
        rounds = max(1, round(gap / nominal))
        gaps.append(gap / rounds)
    return statistics.median(gaps) if len(gaps) >= MIN_GAPS else nominal


def gate(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    margin = settings.deploy_gate_margin_s
    rows = db.recent_rfqs(12)
    if not rows:
        return {
            "safe": True,
            "reason": "noch keine RFQ empfangen",
            "margin_s": margin,
            "interval_s": settings.rfq_interval_s,
        }
    arrivals = [datetime.fromisoformat(r["created_at"]) for r in rows]
    spacing = interval_s(arrivals)
    newest = rows[0]
    newest_at = datetime.fromisoformat(newest["created_at"])
    age = (now - newest_at).total_seconds()
    answered = bool(newest["first_reply_at"] or newest["quoted"])
    settled = answered or age >= RFQ_LIFETIME_S
    steps = max(1, math.ceil(age / spacing)) if age > 0 else 1
    next_at = newest_at + timedelta(seconds=steps * spacing)
    if next_at <= now:
        next_at += timedelta(seconds=spacing)
    to_next = (next_at - now).total_seconds()
    if not settled:
        reason = f"neueste RFQ ({int(age)} s alt) noch ohne Antwort"
    elif to_next <= margin:
        reason = f"nächste RFQ in {int(to_next)} s"
    else:
        reason = (
            f"nächste RFQ in {int(to_next)} s, neueste beantwortet"
            if answered
            else (f"nächste RFQ in {int(to_next)} s, neueste ohne Antwort abgelaufen")
        )
    return {
        "safe": settled and to_next > margin,
        "reason": reason,
        "newest_session_id": newest["id"],
        "newest_at": newest["created_at"],
        "newest_answered": answered,
        "next_expected_at": next_at.isoformat(timespec="seconds"),
        "seconds_to_next": int(to_next),
        "interval_s": round(spacing, 1),
        "margin_s": margin,
    }
