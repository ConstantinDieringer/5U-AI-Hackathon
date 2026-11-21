"""Lanes an email-only airline told us it flies.

An airline asked for a lane it does not serve replies (real mails 1336, 1338, 1731):
    "We do not serve this lane/zone: STR-EZE.
     We fly CGN-EZE, CGN-SGN, ..., STR-YYZ."
We keep that list per airline (db kv, with the mail time) and stop asking it for lanes outside
it. Entries older than CARRIER_OBSERVATION_TTL_S count as unknown, so an hourly setup change heals
itself: we ask once more and learn the new list from the next refusal.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app import db
from app.config import settings

log = logging.getLogger(__name__)

_REFUSED = re.compile(
    r"do not serve this lane(?:/zone)?:\s*([A-Z]{3})-([A-Z]{3})", re.IGNORECASE
)
_WE_FLY = re.compile(
    r"We fly\s+([A-Z]{3}-[A-Z]{3}(?:\s*,\s*[A-Z]{3}-[A-Z]{3})*)", re.IGNORECASE
)
_KEY = "served_lanes:"


@dataclass(frozen=True)
class Refusal:
    lane: str  # "STR-EZE", the lane we asked for
    served: frozenset[str]  # every lane the airline flies, as "ORG-DST"


def parse_refusal(body: str) -> Refusal | None:
    """The refused lane and the airline's own lane list. None if the mail is no refusal."""
    m = _REFUSED.search(body or "")
    if m is None:
        return None
    lane = f"{m.group(1)}-{m.group(2)}".upper()
    fly = _WE_FLY.search(body)
    served = (
        frozenset(x.strip().upper() for x in fly.group(1).split(","))
        if fly
        else frozenset()
    )
    return Refusal(lane, served - {lane})


def remember(email: str, refusal: Refusal, at: str | None = None) -> None:
    """Store the airline's lane list (only if newer than what we have)."""
    key = _KEY + email.strip().lower()
    at = at or db.now()
    old = _load(key)
    if old is not None and old[1] >= _ts(at):
        return
    db.kv_set(
        key,
        json.dumps(
            {"lanes": sorted(refusal.served), "refused": refusal.lane, "at": at}
        ),
    )


def serves(
    email: str, origin: str, destination: str, now: datetime | None = None
) -> bool | None:
    """True/False from a fresh refusal list, None when we do not know (never asked, or stale)."""
    entry = _load(_KEY + email.strip().lower())
    if entry is None:
        return None
    lanes, at = entry
    age = ((now or datetime.now(UTC)) - at).total_seconds()
    if age > settings.served_lanes_ttl_s:
        return None
    return f"{origin}-{destination}".upper() in lanes


def backfill() -> int:
    """Learn from refusals already in the mailbox (run once at startup). Returns how many."""
    n = 0
    for m in db.incoming_mails_containing("do not serve this lane"):
        r = parse_refusal(m["body"] or "")
        if r is not None and m["sender"]:
            remember(m["sender"], r, at=m["received_at"])
            n += 1
    if n:
        log.info("served lanes learned from %d refusal mail(s)", n)
    return n


def _ts(iso: str) -> datetime:
    t = datetime.fromisoformat(iso)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _load(key: str) -> tuple[frozenset[str], datetime] | None:
    raw = db.kv_get(key)
    if raw is None:
        return None
    try:
        d = json.loads(raw)
        return frozenset(d["lanes"]), _ts(d["at"])
    except (ValueError, KeyError, TypeError):
        log.warning("served lanes entry %s unreadable, ignored", key)
        return None
