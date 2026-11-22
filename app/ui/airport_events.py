"""Events that may affect the airports in our network, for the dashboard map's info box.

Two public, read-only feeds; nothing of ours is sent to them:
- GDACS (Global Disaster Alert and Coordination System): current tropical cyclones, earthquakes,
  volcanoes, floods and wildfires with a position and an alert level. An event counts for an
  airport when it lies within the hazard's radius.
- FAA NAS status: ground stops, ground delay programs, delays and closures at US airports, by IATA
  code. Closures that only affect non-scheduled general aviation are skipped.

Both feeds are cached for `AIRPORT_EVENTS_TTL_S`; a failing feed is reported, never raised.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import httpx

from app.config import settings
from app.ui import airport_weather

log = logging.getLogger(__name__)
T = TypeVar("T")

AIRPORTS = Path(__file__).with_name("static") / "geo" / "airports.json"
IATA = re.compile(r"^[A-Z]{3}$")
TIMEOUT_S = 6.0
HEADERS = {"User-Agent": "forwarder-console/1.0", "Accept": "application/json, application/xml"}

# Radius in km within which a hazard can disrupt an airport, by GDACS event type.
HAZARD_RADIUS_KM = {"TC": 900.0, "VO": 500.0, "EQ": 300.0, "FL": 150.0, "WF": 100.0}
HAZARD_KIND = {
    "TC": "Tropical cyclone",
    "VO": "Volcano",
    "EQ": "Earthquake",
    "FL": "Flood",
    "WF": "Wildfire",
}
LEVEL_RANK = {"red": 0, "orange": 1, "green": 2}
FAA_KIND = {
    "Ground_Stop_List": ("Ground stop", "red"),
    "Ground_Delay_List": ("Ground delay program", "orange"),
    "Arrival_Departure_Delay_List": ("Delays", "orange"),
    "Airport_Closure_List": ("Airport closure", "red"),
}


@dataclass(frozen=True)
class Hazard:
    id: str
    kind: str
    title: str
    level: str
    lat: float
    lon: float
    radius_km: float
    country: str
    starts_at: str
    ends_at: str
    url: str


@dataclass(frozen=True)
class AirportNotice:
    id: str
    kind: str
    iata: str
    level: str
    detail: str
    url: str


_lock = threading.Lock()
_cache: dict[str, tuple[float, list[Any], bool]] = {}


@lru_cache(maxsize=1)
def airport_coords() -> dict[str, tuple[float, float, str]]:
    data = json.loads(AIRPORTS.read_text(encoding="utf-8"))
    coords: dict[str, tuple[float, float, str]] = {}
    for code, row in data.items():
        try:
            coords[code] = (float(row["lat"]), float(row["lon"]), str(row.get("name", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return coords


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def parse_gdacs(payload: object) -> list[Hazard]:
    """Validate the GDACS FeatureCollection; malformed features are dropped one by one."""
    features = payload.get("features") if isinstance(payload, dict) else None
    hazards: list[Hazard] = []
    for feature in features if isinstance(features, list) else []:
        try:
            props, geometry = feature["properties"], feature["geometry"]
            kind = str(props["eventtype"])
            if (
                kind not in HAZARD_RADIUS_KM
                or str(props.get("iscurrent", "true")).lower() != "true"
            ):
                continue
            if geometry.get("type") != "Point":
                continue
            lon, lat = (float(v) for v in geometry["coordinates"][:2])
            level = str(props.get("alertlevel", "Green")).lower()
            urls = props.get("url") if isinstance(props.get("url"), dict) else {}
            hazards.append(
                Hazard(
                    id=f"gdacs-{kind}-{int(props['eventid'])}",
                    kind=HAZARD_KIND[kind],
                    title=str(props.get("name") or props.get("eventname") or HAZARD_KIND[kind])[
                        :160
                    ],
                    level=level if level in LEVEL_RANK else "green",
                    lat=lat,
                    lon=lon,
                    radius_km=HAZARD_RADIUS_KM[kind],
                    country=str(props.get("country") or "")[:80],
                    starts_at=str(props.get("fromdate") or ""),
                    ends_at=str(props.get("todate") or ""),
                    url=str(urls.get("report") or "")
                    if str(urls.get("report") or "").startswith("https://www.gdacs.org/")
                    else "",
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return hazards


FAA_CAUSES = {
    "WX": "weather",
    "VOL": "traffic volume",
    "RWY": "runway",
    "EQ": "equipment",
    "STAFF": "staffing",
    "TM INITIATIVES": "traffic management",
    "MIT": "spacing",
}


def faa_reason(raw: str) -> str:
    """'TM Initiatives:MIT:WX' -> 'traffic management, spacing, weather'; NOTAM text stays as is."""
    if ":" not in raw or len(raw) > 60:
        return raw
    words = [
        FAA_CAUSES.get(part.strip().upper(), part.strip())
        for part in raw.split(":")
        if part.strip()
    ]
    return ", ".join(dict.fromkeys(words))


def _text(node: ET.Element, tag: str) -> str:
    found = node.find(tag)
    return " ".join((found.text or "").split()) if found is not None else ""


def parse_faa(xml_text: str) -> list[AirportNotice]:
    """Read every airport entry of the FAA status XML; GA-only closures are noise for cargo."""
    root = ET.fromstring(xml_text)
    notices: list[AirportNotice] = []
    for list_tag, (kind, level) in FAA_KIND.items():
        for listing in root.iter(list_tag):
            for entry in listing:
                iata = _text(entry, "ARPT").upper()
                if not IATA.match(iata):
                    continue
                reason = _text(entry, "Reason")
                if list_tag == "Airport_Closure_List" and (
                    "NON SKED" in reason or "TRANSIENT GA" in reason
                ):
                    continue
                parts = [faa_reason(reason)]
                span = entry.find("Arrival_Departure")
                if span is not None and _text(span, "Max"):
                    direction = span.get("Type", "").lower()
                    parts.append(
                        f"{direction} {_text(span, 'Min')} to {_text(span, 'Max')}".strip()
                    )
                if _text(entry, "Avg"):
                    parts.append(f"average {_text(entry, 'Avg')}, max {_text(entry, 'Max')}")
                if _text(entry, "End_Time"):
                    parts.append(f"until {_text(entry, 'End_Time')}")
                if _text(entry, "Reopen"):
                    parts.append(f"reopens {_text(entry, 'Reopen')}")
                detail = " · ".join(p for p in parts if p)
                notices.append(
                    AirportNotice(
                        id=f"faa-{list_tag}-{iata}",
                        kind=kind,
                        iata=iata,
                        level=level,
                        detail=detail[:240],
                        url="https://nasstatus.faa.gov/",
                    )
                )
    return notices


def _cached(name: str, fetch: Callable[[], list[T]]) -> tuple[list[T], bool]:
    """Return (value, ok). A fresh failure keeps serving the last good value, marked not ok."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < settings.airport_events_ttl_s:
            return hit[1], hit[2]
    try:
        value, ok = fetch(), True
    except (httpx.HTTPError, ValueError, ET.ParseError) as exc:  # report, never raise
        log.warning("airport events: %s feed failed: %s", name, type(exc).__name__)
        value, ok = (hit[1] if hit else []), False
    with _lock:
        _cache[name] = (now, value, ok)
    return value, ok


def _fetch_gdacs() -> list[Hazard]:
    response = httpx.get(settings.airport_events_gdacs_url, timeout=TIMEOUT_S, headers=HEADERS)
    response.raise_for_status()
    return parse_gdacs(response.json())


WEATHER_BATCH = 50


def _fetch_weather(codes: list[str]) -> list[airport_weather.WeatherAlert]:
    """One forecast request per 50 airports: hourly weather for the next 24 hours."""
    coords = airport_coords()
    known = [c for c in codes if c in coords]
    alerts: list[airport_weather.WeatherAlert] = []
    for i in range(0, len(known), WEATHER_BATCH):
        batch = known[i : i + WEATHER_BATCH]
        response = httpx.get(
            settings.airport_events_weather_url,
            params={
                "latitude": ",".join(f"{coords[c][0]:.4f}" for c in batch),
                "longitude": ",".join(f"{coords[c][1]:.4f}" for c in batch),
                "hourly": "weather_code,wind_gusts_10m,visibility,snowfall,precipitation",
                "forecast_hours": 24,
                "timezone": "UTC",
            },
            timeout=TIMEOUT_S,
            headers=HEADERS,
        )
        response.raise_for_status()
        alerts += airport_weather.parse_forecast(response.json(), batch)
    return alerts


def _fetch_faa() -> list[AirportNotice]:
    response = httpx.get(settings.airport_events_faa_url, timeout=TIMEOUT_S, headers=HEADERS)
    response.raise_for_status()
    return parse_faa(response.text)


def events_for(codes: list[str]) -> dict[str, Any]:
    """Events touching any of the given airports, most severe and closest first."""
    airports = sorted({c for c in codes if IATA.match(c)})
    if not settings.airport_events:
        return {"enabled": False, "sources": [], "events": []}
    hazards, gdacs_ok = _cached("gdacs", _fetch_gdacs)
    notices, faa_ok = _cached("faa", _fetch_faa)
    weather, weather_ok = _cached("weather:" + ",".join(airports), lambda: _fetch_weather(airports))
    coords = airport_coords()
    events: list[dict[str, Any]] = []
    for hazard in hazards:
        near = []
        for code in airports:
            if code not in coords:
                continue
            km = distance_km(hazard.lat, hazard.lon, coords[code][0], coords[code][1])
            if km <= hazard.radius_km:
                near.append({"iata": code, "distance_km": round(km)})
        if near:
            near.sort(key=lambda a: a["distance_km"])
            events.append(
                {
                    "id": hazard.id,
                    "source": "GDACS",
                    "kind": hazard.kind,
                    "title": hazard.title,
                    "level": hazard.level,
                    "detail": hazard.country,
                    "airports": near,
                    "starts_at": hazard.starts_at,
                    "ends_at": hazard.ends_at,
                    "url": hazard.url,
                }
            )
    wanted = set(airports)
    for notice in notices:
        if notice.iata in wanted:
            events.append(
                {
                    "id": notice.id,
                    "source": "FAA",
                    "kind": notice.kind,
                    "title": f"{notice.kind} at {notice.iata}",
                    "level": notice.level,
                    "detail": notice.detail,
                    "airports": [{"iata": notice.iata, "distance_km": 0}],
                    "starts_at": "",
                    "ends_at": "",
                    "url": notice.url,
                }
            )
    for alert in weather:
        events.append(
            {
                "id": f"weather-{alert.kind}-{alert.iata}".replace(" ", "-").lower(),
                "source": "Open-Meteo",
                "kind": alert.kind,
                "title": f"{alert.kind} at {alert.iata}",
                "level": alert.level,
                "detail": alert.detail,
                "airports": [{"iata": alert.iata, "distance_km": 0}],
                "starts_at": alert.starts_at,
                "ends_at": alert.ends_at,
                "url": "https://open-meteo.com/",
            }
        )
    events.sort(key=lambda e: (LEVEL_RANK.get(e["level"], 3), e["airports"][0]["distance_km"]))
    return {
        "enabled": True,
        "sources": [
            {"name": "GDACS", "ok": gdacs_ok},
            {"name": "FAA", "ok": faa_ok},
            {"name": "Open-Meteo", "ok": weather_ok},
        ],
        "events": events,
    }
