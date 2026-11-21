"""Reference data from the start kit: carrier list and pickup zones. No network, no LLM."""

from __future__ import annotations

import csv
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
        return {row["zone"].strip(): row["airport"].strip() for row in csv.DictReader(f)}


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
    addr = email.strip().lower()
    return next((c for c in carriers() if c.email == addr), None)


def carrier_by_name(name: str) -> Carrier | None:
    key = name.strip().lower()
    return next((c for c in carriers() if c.name.lower() == key), None)


# Names from the team's phase-1 plan (Slack, 14:45), kept as thin aliases.
def airline_email(name: str) -> str | None:
    c = carrier_by_name(name)
    return c.email if c else None


def airport_for_zone(zone: str) -> str | None:
    return pickup_zones().get(zone.strip())
