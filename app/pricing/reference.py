"""Reference data from the start kit: carrier list and pickup zones. No network, no LLM."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

KIT_DIR = Path(__file__).resolve().parents[2] / "data" / "start-kit"


@dataclass(frozen=True)
class Carrier:
    name: str
    kind: Literal["airline", "trucker"]
    email: str
    serves: tuple[str, ...]  # IATA codes (airline) or 2-digit pickup zones (trucker)


@lru_cache(maxsize=1)
def carriers(kit_dir: Path = KIT_DIR) -> tuple[Carrier, ...]:
    with open(kit_dir / "carrier-list.csv", encoding="utf-8", newline="") as f:
        return tuple(
            Carrier(
                name=row["name"].strip(),
                kind="trucker" if row["kind"].strip() == "trucker" else "airline",
                email=row["email"].strip().lower(),
                serves=tuple(row["serves"].split()),
            )
            for row in csv.DictReader(f)
        )


@lru_cache(maxsize=1)
def pickup_zones(kit_dir: Path = KIT_DIR) -> dict[str, str]:
    """2-digit zone -> origin airport (IATA)."""
    with open(kit_dir / "pickup_zones.csv", encoding="utf-8", newline="") as f:
        return {
            row["zone"].strip(): row["airport"].strip() for row in csv.DictReader(f)
        }


# German/English spellings customers use for the pickup_zones.csv areas
_AREA_ALIASES = {
    "münchen": "munich",
    "muenchen": "munich",
    "köln": "cologne",
    "koeln": "cologne",
    "düsseldorf": "duesseldorf",
    "mönchengladbach": "moenchengladbach",
    "gießen": "giessen",
    "lüneburg": "lueneburg",
    "tübingen": "tuebingen",
    "böblingen": "boeblingen",
    "frankfurt": "frankfurt am main",
}


@lru_cache(maxsize=1)
def pickup_areas(kit_dir: Path = KIT_DIR) -> dict[str, str]:
    """Area name (lower case) -> 2-digit zone, e.g. 'regensburg' -> '93'."""
    with open(kit_dir / "pickup_zones.csv", encoding="utf-8", newline="") as f:
        return {
            row["area"].strip().lower(): row["zone"].strip()
            for row in csv.DictReader(f)
        }


def zone_for_area(name: str) -> str | None:
    key = " ".join(name.lower().split())
    return pickup_areas().get(_AREA_ALIASES.get(key, key))


def find_area(text: str) -> str | None:
    """First pickup-zone area named in the text (longest names first: 'Munich South' before
    'Munich'). Returns the area as written in pickup_zones.csv."""
    names = {**{a: a for a in pickup_areas()}, **_AREA_ALIASES}
    low = (text or "").lower()
    hits = []
    for spelled, area in names.items():
        m = re.search(rf"(?<![\w-]){re.escape(spelled)}(?![\w-])", low)
        if m:
            hits.append((m.start(), -len(spelled), area))
    if not hits:
        return None
    area = min(hits)[2]
    return next(a for a in _areas_as_written() if a.lower() == area)


@lru_cache(maxsize=1)
def _areas_as_written(kit_dir: Path = KIT_DIR) -> tuple[str, ...]:
    with open(kit_dir / "pickup_zones.csv", encoding="utf-8", newline="") as f:
        return tuple(row["area"].strip() for row in csv.DictReader(f))


def zone_for_zip(zip_code: str) -> str:
    """German ZIP -> pickup zone (its first two digits)."""
    digits = "".join(ch for ch in zip_code if ch.isdigit())
    return digits[:2]


def origin_for_zip(zip_code: str) -> str | None:
    return pickup_zones().get(zone_for_zip(zip_code))


def airlines_serving(destination: str) -> list[Carrier]:
    dest = destination.upper()
    return [c for c in carriers() if c.kind == "airline" and dest in c.serves]


def truckers_for_zone(zone: str) -> list[Carrier]:
    return [c for c in carriers() if c.kind == "trucker" and zone in c.serves]


def carrier_by_email(email: str) -> Carrier | None:
    """Also finds a carrier by the new address it announced (app/mail/address_changes.py)."""
    addr = email.strip().lower()
    found = next((c for c in carriers() if c.email == addr), None)
    if found is None:
        from app.mail.address_changes import canonical

        original = canonical(addr)
        found = next((c for c in carriers() if c.email == original), None)
    return found


def carrier_by_name(name: str) -> Carrier | None:
    key = name.strip().lower()
    return next((c for c in carriers() if c.name.lower() == key), None)


# Names from the team's phase-1 plan (Slack, 14:45), kept as thin aliases.
def airline_email(name: str) -> str | None:
    c = carrier_by_name(name)
    return c.email if c else None


def airport_for_zone(zone: str) -> str | None:
    return pickup_zones().get(zone.strip())
