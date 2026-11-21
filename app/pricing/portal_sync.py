"""Rate portal: lane snapshot (ports + brackets) and live rates as RateOption.

All HTTP goes through app.hackloop (TECH_SPEC section 3). Expected hackloop functions:
    get_ports() -> dict                          # raw /v1/portal/ports
    get_brackets(airline: str) -> dict           # raw /v1/portal/brackets
    get_rate(origin, destination, airline, weight_kg, volume_cbm) -> dict   # raw /v1/portal/rates
They raise on non-2xx. `get_rate` is capped (10 reads/min at 21.11. 15:00), so we cache.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.models import RateOption, Rfq
from app.pricing.pricing import money, resolve_origin, to_minor

log = logging.getLogger(__name__)

PORTAL_LANES = Path(__file__).resolve().parents[2] / "data" / "portal_lanes.json"
CACHE_TTL_S = 300  # a rate_id lives 15 min; reuse a read for at most 5 min

RateFetcher = Callable[[str, str, str, float, float], dict[str, Any]]
_cache: dict[tuple[str, str, str, float, float], tuple[float, dict[str, Any]]] = {}


# ---------- lane snapshot ----------


def build_portal_lanes(
    ports: dict[str, Any], brackets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Validate raw ports/brackets responses and shape them for data/portal_lanes.json."""
    lanes = [
        {
            "origin": str(lane["origin"]).upper(),
            "destination": str(lane["destination"]).upper(),
            "airlines": [{"code": a["code"], "name": a["name"]} for a in lane["airlines"]],
        }
        for lane in ports["lanes"]
    ]
    return {
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "lanes": lanes,
        "brackets": {
            code: [int(b) for b in br["breaks_kg"]] for code, br in sorted(brackets.items())
        },
    }


def sync_portal_lanes(path: Path = PORTAL_LANES) -> dict[str, Any]:
    """Fetch ports + brackets (not rate-limited) and write the snapshot."""
    from app import hackloop

    ports = hackloop.get_ports()
    codes = sorted({a["code"] for lane in ports["lanes"] for a in lane["airlines"]})
    snapshot = build_portal_lanes(ports, {c: hackloop.get_brackets(c) for c in codes})
    Path(path).write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    load_portal_lanes.cache_clear()
    return snapshot


@lru_cache(maxsize=1)
def load_portal_lanes(path: Path = PORTAL_LANES) -> dict[str, Any]:
    if not Path(path).exists():
        return {"lanes": [], "brackets": {}}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def portal_airlines(origin: str, destination: str) -> list[dict[str, str]]:
    """[{code, name}] the portal can price on this lane (empty if the lane is not on the portal)."""
    o, d = origin.upper(), destination.upper()
    for lane in load_portal_lanes()["lanes"]:
        if lane["origin"] == o and lane["destination"] == d:
            return list(lane["airlines"])
    return []


# ---------- live rates ----------


def parse_rate(raw: dict[str, Any]) -> RateOption:
    """Validate one /v1/portal/rates response. Amounts arrive as decimal strings."""
    try:
        total = money(Decimal(str(raw["total"])))
        currency = str(raw["currency"]).upper()
        fx = raw.get("fx_eur_per_usd")
        fx_rate = Decimal(str(fx)) if fx not in (None, "") else None
        chargeable = float(Decimal(str(raw["chargeable_weight_kg"])))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError(f"unexpected portal rate shape: {raw!r}") from exc
    if currency == "EUR":
        total_eur, fx_rate = total, None
    elif fx_rate is not None:
        total_eur = money(total * fx_rate)
    else:
        raise ValueError(f"{currency} rate without fx_eur_per_usd: {raw!r}")
    return RateOption(
        airline=str(raw["airline"]),
        origin=str(raw["origin"]).upper(),
        destination=str(raw["destination"]).upper(),
        transit_days=int(raw["transit_days"]),
        total_minor=to_minor(total),
        currency=currency,
        total_eur_minor=to_minor(total_eur),
        source="portal",
        rate_id=str(raw["rate_id"]),
        valid_until=str(raw["valid_until"]),
        chargeable_kg=chargeable,
        billed_kg=chargeable,
        fx_eur_per_unit=str(fx_rate) if fx_rate is not None else None,
    )


def _fetch_cached(fetch: RateFetcher, key: tuple[str, str, str, float, float]) -> dict[str, Any]:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    raw = fetch(*key)
    _cache[key] = (time.monotonic(), raw)
    return raw


def portal_options(rfq: Rfq, fetch: RateFetcher | None = None) -> list[RateOption]:
    """One portal rate per airline on the lane. Failing airlines are skipped and logged."""
    origin = resolve_origin(rfq)
    if origin is None:
        return []
    if fetch is None:
        from app import hackloop

        fetch = hackloop.get_rate
    dest = rfq.destination.upper()
    out: list[RateOption] = []
    for airline in portal_airlines(origin, dest):
        key = (origin, dest, airline["code"], float(rfq.weight_kg), float(rfq.volume_cbm))
        try:
            out.append(parse_rate(_fetch_cached(fetch, key)))
        except Exception as exc:  # noqa: BLE001 - one failing airline must not lose the quote
            log.warning("portal rate failed %s %s-%s: %s", airline["code"], origin, dest, exc)
    return out


if __name__ == "__main__":
    snap = sync_portal_lanes()
    print(len(snap["lanes"]), "lanes,", len(snap["brackets"]), "airlines")
