"""Pickup cost estimate when no trucker answered a door-to-port request in time.

Without it the round ended without a reply (`no_trucking`), which always scores 0. The
estimate uses earlier real trucker bids for the same pickup zone: the bid with the nearest
weight, scaled by the square root of the weight ratio (pickup cost grows slower than weight)
and bounded so a far-off weight cannot produce an absurd number. No earlier bid for the zone:
no estimate, the round stays an error as before. Rollback: ESTIMATE_MISSING_TRUCK=false.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

log = logging.getLogger(__name__)

MAX_WEIGHT_RATIO = Decimal(4)  # beyond 4x heavier/lighter an earlier bid says too little
HISTORY_LIMIT = 200


@dataclass(frozen=True)
class PastBid:
    trucker_email: str
    total_eur: Decimal
    weight_kg: float
    session_id: int


@dataclass(frozen=True)
class Estimate:
    total_eur: Decimal
    basis: PastBid


def estimate(history: list[PastBid], weight_kg: float) -> Estimate | None:
    """Pure: nearest-weight bid, sqrt-scaled. None when nothing comparable exists."""
    usable = [b for b in history if b.weight_kg > 0 and b.total_eur > 0]
    if not usable or weight_kg <= 0:
        return None
    weight = Decimal(str(weight_kg))
    nearest = min(usable, key=lambda b: (abs(Decimal(str(b.weight_kg)) - weight), -b.session_id))
    ratio = weight / Decimal(str(nearest.weight_kg))
    if ratio > MAX_WEIGHT_RATIO or ratio < 1 / MAX_WEIGHT_RATIO:
        return None
    total = (nearest.total_eur * ratio.sqrt()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Estimate(total, nearest)


def history_for_zone(zone: str, exclude_session: int) -> list[PastBid]:
    """Real trucker bids (bid_received events) of earlier sessions picking up in `zone`."""
    from app import db
    from app.models import Rfq
    from app.pricing.reference import carriers

    truckers = {c.name: c.email for c in carriers() if c.kind == "trucker"}
    out: list[PastBid] = []
    rows = (
        db.conn()
        .execute(
            "SELECT e.session_id, e.message, e.payload_json, s.request_json FROM events e"
            " JOIN sessions s ON s.id = e.session_id"
            " WHERE e.kind = 'bid_received' AND e.session_id != ?"
            " AND s.request_json IS NOT NULL ORDER BY e.id DESC LIMIT ?",
            (exclude_session, HISTORY_LIMIT * 5),
        )
        .fetchall()
    )
    for row in rows:
        name = str(row["message"] or "").removeprefix("Gebot ").partition(":")[0].strip()
        if name not in truckers:
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
            total = Decimal(str(payload["original_amount"])) * Decimal(
                str(payload.get("fx_rate") or 1)
            )
            rfq = Rfq.model_validate(json.loads(row["request_json"]))
        except Exception:  # noqa: BLE001 - an old or partial row is simply not used
            log.debug("truck estimate: event row of session %s skipped", row["session_id"])
            continue
        if rfq.pickup_zone != zone:
            continue
        out.append(PastBid(truckers[name], total, rfq.weight_kg, int(row["session_id"])))
        if len(out) >= HISTORY_LIMIT:
            break
    return out


def for_zone(zone: str | None, weight_kg: float, session_id: int) -> Estimate | None:
    if not zone:
        return None
    try:
        return estimate(history_for_zone(zone, session_id), weight_kg)
    except Exception:
        log.exception("truck estimate for zone %s failed", zone)
        return None
