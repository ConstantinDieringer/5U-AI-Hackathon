"""Rate portal: lane snapshot (ports + brackets) and live rates as RateOption.

All HTTP goes through app.hackloop (TECH_SPEC section 3). Expected hackloop functions:
    get_ports() -> dict                          # raw /v1/portal/ports
    get_brackets(airline: str) -> dict           # raw /v1/portal/brackets
    get_rate(origin, destination, airline, weight_kg, volume_cbm) -> dict   # raw /v1/portal/rates
They raise on non-2xx. `get_rate` is capped (10 reads/min at 21.11. 15:00), so we cache.

Observed behaviour (21.11. 15:30): the portal prices exactly the weight asked, does not round
the chargeable weight (0.1234 cbm -> 20.6078 kg), and answers 404 for an airline off the lane.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import RateOption, Rfq
from app.pricing.pricing import chargeable_kg, money, resolve_origin, to_minor

log = logging.getLogger(__name__)

SEED_PORTAL_LANES = Path(__file__).resolve().parents[2] / "data" / "portal_lanes.json"
PORTAL_LANES = settings.runtime_dir / "portal_lanes.json"
CACHE_TTL_S = 300  # a rate_id lives 15 min; reuse a read for at most 5 min
SNAPSHOT_MAX_AGE_S = 1800  # 5U changes the setup about hourly; ports/brackets are free
MAX_PARALLEL = 4  # at most 4 airlines per lane

RateFetcher = Callable[[str, str, str, float, float], dict[str, Any]]
CacheKey = tuple[str, str, str, float, float]
_cache: dict[CacheKey, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()
_seen_dataset_version: str | None = None
_last_sync_attempt = float("-inf")  # monotonic seconds
SYNC_RETRY_S = 300
_force_refresh = False
_sync_thread: threading.Thread | None = None
_sync_lock = threading.Lock()


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
    """Fetch ports + brackets (not rate-limited) and write the snapshot atomically."""
    from app import hackloop

    ports = hackloop.get_ports()
    codes = sorted({a["code"] for lane in ports["lanes"] for a in lane["airlines"]})
    snapshot = build_portal_lanes(ports, {c: hackloop.get_brackets(c) for c in codes})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    tmp.replace(path)  # readers never see a half-written file
    load_portal_lanes.cache_clear()
    return snapshot


@lru_cache(maxsize=1)
def load_portal_lanes(path: Path = PORTAL_LANES) -> dict[str, Any]:
    if not Path(path).exists():
        if path == PORTAL_LANES:
            return json.loads(SEED_PORTAL_LANES.read_text(encoding="utf-8"))
        return {"lanes": [], "brackets": {}}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def snapshot_age_s(now: datetime | None = None) -> float:
    synced = load_portal_lanes().get("synced_at")
    if not synced:
        return float("inf")
    return ((now or datetime.now(UTC)) - datetime.fromisoformat(synced)).total_seconds()


def refresh_if_stale(max_age_s: float = SNAPSHOT_MAX_AGE_S) -> None:
    """Re-sync lanes when the snapshot is old. Never raises: a stale snapshot beats none."""
    global _last_sync_attempt, _force_refresh
    now = time.monotonic()
    if (
        not _force_refresh and snapshot_age_s() < max_age_s
    ) or now - _last_sync_attempt < SYNC_RETRY_S:
        return
    _last_sync_attempt = now  # a failing sync must not slow down every RFQ
    try:
        sync_portal_lanes()
        _force_refresh = False
    except Exception as exc:  # noqa: BLE001 - keep quoting with the old snapshot
        log.warning("portal lane sync failed, keeping old snapshot: %s", exc)


def schedule_refresh() -> None:
    """A ports/brackets refresh must not consume the customer's quote time budget."""
    global _sync_thread
    with _sync_lock:
        if _sync_thread is None or not _sync_thread.is_alive():
            _sync_thread = threading.Thread(
                target=refresh_if_stale, name="portal-snapshot", daemon=True
            )
            _sync_thread.start()


def portal_airlines(origin: str, destination: str) -> list[dict[str, str]]:
    """[{code, name}] the portal can price on this lane (empty if the lane is not on the portal)."""
    o, d = origin.upper(), destination.upper()
    for lane in load_portal_lanes()["lanes"]:
        if lane["origin"] == o and lane["destination"] == d:
            return list(lane["airlines"])
    return []


# ---------- live rates ----------


def _note_dataset_version(version: object) -> None:
    """A new dataset_version is the earliest sign of one of 5U's hourly setup changes."""
    global _seen_dataset_version, _force_refresh, _last_sync_attempt
    v = str(version) if version is not None else None
    if v and v != _seen_dataset_version:
        from app import db

        try:
            changed = db.note_dataset_version(v)
        except Exception:
            log.exception("could not persist portal dataset observation")
            changed = _seen_dataset_version is not None
        if _seen_dataset_version is not None:
            log.warning("portal dataset_version changed %s -> %s", _seen_dataset_version, v)
        _seen_dataset_version = v
        if changed:
            with _lock:
                _cache.clear()
            _force_refresh = True
            _last_sync_attempt = float("-inf")


def parse_rate(raw: dict[str, Any]) -> RateOption:
    """Validate one /v1/portal/rates response. Amounts arrive as decimal strings."""
    try:
        total = money(Decimal(str(raw["total"])))
        currency = str(raw["currency"]).strip().upper()
        fx = raw.get("fx_eur_per_usd")
        fx_rate = Decimal(str(fx)) if fx not in (None, "") else None
        chargeable = float(Decimal(str(raw["chargeable_weight_kg"])))
        transit = int(raw["transit_days"])
        airline, rate_id = str(raw["airline"]), str(raw["rate_id"])
        origin, dest = str(raw["origin"]).upper(), str(raw["destination"]).upper()
        valid_until = str(raw["valid_until"])
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"unexpected portal rate shape: {raw!r}") from exc
    if (
        not total.is_finite()
        or total <= 0
        or not Decimal(str(chargeable)).is_finite()
        or chargeable <= 0
        or transit < 0
        or len(currency) != 3
    ):
        raise ValueError(f"implausible portal rate: {raw!r}")
    if currency == "EUR":
        total_eur, fx_rate = total, None
    elif currency == "USD" and fx_rate is not None and fx_rate.is_finite() and fx_rate > 0:
        total_eur = money(total * fx_rate)
    else:
        raise ValueError(f"{currency} rate without a usable fx_eur_per_usd: {raw!r}")
    _note_dataset_version(raw.get("dataset_version"))
    return RateOption(
        airline=airline,
        origin=origin,
        destination=dest,
        transit_days=transit,
        total_minor=to_minor(total),
        currency=currency,
        total_eur_minor=to_minor(total_eur),
        source="portal",
        rate_id=rate_id,
        valid_until=valid_until,
        chargeable_kg=chargeable,
        billed_kg=chargeable,
        fx_eur_per_unit=str(fx_rate) if fx_rate is not None else None,
        fx_source="portal:fx_eur_per_usd" if currency == "USD" else "identity",
        fx_valid_until=valid_until if currency == "USD" else None,
        observed_at=datetime.now(UTC).isoformat(),
        tariff_version=str(raw.get("dataset_version", "")),
    )


def _fetch_cached(fetch: RateFetcher, key: CacheKey) -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_TTL_S]:
            del _cache[k]
        hit = _cache.get(key)
    if hit:
        try:
            if datetime.fromisoformat(hit[1]["valid_until"]) > datetime.now(UTC):
                return hit[1]
        except (KeyError, ValueError, TypeError):
            pass
    raw = fetch(*key)  # outside the lock: network
    with _lock:
        _cache[key] = (time.monotonic(), raw)
    return raw


def _one_rate(
    fetch: RateFetcher, key: CacheKey, result: PortalResult | None = None
) -> RateOption | None:
    origin, dest, code = key[0], key[1], key[2]
    try:
        opt = parse_rate(_fetch_cached(fetch, key))
    except Exception as exc:  # noqa: BLE001 - one failing airline must not lose the quote
        log.warning("portal rate failed %s %s-%s: %s", code, origin, dest, exc)
        if result is not None:
            from app import hackloop

            result.errors.append(f"{code}: {type(exc).__name__}: {exc}")
            if isinstance(exc, hackloop.HackloopError) and exc.status == 503:
                result.email_fallbacks.append(code)
        return None
    if (opt.origin, opt.destination) != (origin, dest):
        log.warning(
            "portal answered %s-%s for %s-%s, ignored", opt.origin, opt.destination, origin, dest
        )
        return None
    return opt


@dataclass
class PortalResult:
    options: list[RateOption] = field(default_factory=list)
    email_fallbacks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def portal_result(
    rfq: Rfq, fetch: RateFetcher | None = None, *, deadline: float | None = None
) -> PortalResult:
    """One portal rate per airline on the lane, fetched in parallel (each call can take up to
    the 10 s timeout). Failing airlines are skipped and logged."""
    origin = resolve_origin(rfq)
    result = PortalResult()
    if origin is None:
        return result
    if fetch is None:
        from app import hackloop

        schedule_refresh()
        fetch = lambda *args: hackloop.get_rate(*args, deadline=deadline)
    dest = rfq.destination.upper()
    weight, volume = float(rfq.weight_kg), float(rfq.volume_cbm)
    keys: list[CacheKey] = []
    for a in portal_airlines(origin, dest):
        keys.append((origin, dest, a["code"], weight, volume))
        nb = next_break(a["code"], chargeable_kg(weight, volume))
        if nb is not None and settings.use_breakpoints:  # next break can be cheaper
            keys.append((origin, dest, a["code"], nb, volume))
    if not keys:
        return result
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(keys))) as pool:
        results = list(pool.map(lambda k: _one_rate(fetch, k, result), keys))
    best: dict[str, RateOption] = {}  # per airline the cheaper of exact weight / next break
    for r in results:
        if r is not None and (
            r.airline not in best or r.total_eur_minor < best[r.airline].total_eur_minor
        ):
            best[r.airline] = r
    result.options = list(best.values())
    return result


def portal_options(rfq: Rfq, fetch: RateFetcher | None = None) -> list[RateOption]:
    return portal_result(rfq, fetch).options


def next_break(code: str, chargeable: Decimal) -> float | None:
    """The airline's lowest weight break above the chargeable weight (portal brackets)."""
    above = [b for b in load_portal_lanes().get("brackets", {}).get(code, []) if b > chargeable]
    return float(min(above)) if above else None


if __name__ == "__main__":
    snap = sync_portal_lanes()
    print(len(snap["lanes"]), "lanes,", len(snap["brackets"]), "airlines")
