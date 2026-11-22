"""Read-only rate catalogue for the console's Rates tab.

Combines the airline tariff sheet that pricing uses (weight breaks, minimum, surcharges) with the
portal snapshot (which airlines fly a lane and their weight brackets). Rates travel as decimal
strings with their currency, never as floats.
"""

from __future__ import annotations

from typing import Any

from app.pricing import portal_sync, pricing


def _amount(value: object) -> str:
    return format(value, "f")


def tariff_lanes() -> list[dict[str, Any]]:
    """Every tariff-sheet lane, cheapest weight break last, as pricing sees it right now."""
    return [
        {
            "airline": lane.airline,
            "origin": lane.origin,
            "destination": lane.destination,
            "transit_days": lane.transit_days,
            "currency": lane.currency,
            "minimum": _amount(lane.minimum),
            "breaks": [{"from_kg": _amount(kg), "rate": _amount(rate)} for kg, rate in lane.breaks],
            "fuel": _amount(lane.fuel),
            "security": _amount(lane.security),
            "valid_from": lane.valid_from.isoformat(),
            "valid_to": lane.valid_to.isoformat(),
            "received_at": lane.received_at,
        }
        for lane in pricing.load_lanes()
    ]


def portal_lanes() -> dict[str, Any]:
    snapshot = portal_sync.load_portal_lanes()
    brackets = snapshot.get("brackets") or {}
    lanes = [
        {
            "origin": lane.get("origin", ""),
            "destination": lane.get("destination", ""),
            "airlines": [
                {
                    "code": airline.get("code", ""),
                    "name": airline.get("name", ""),
                    "brackets_kg": brackets.get(airline.get("code", ""), []),
                }
                for airline in lane.get("airlines") or []
            ],
        }
        for lane in snapshot.get("lanes") or []
    ]
    return {"synced_at": snapshot.get("synced_at"), "lanes": lanes}


def catalogue() -> dict[str, Any]:
    return {
        "tariff": {
            "source": "runtime" if pricing.RUNTIME_TARIFFS.exists() else "seed",
            "lanes": tariff_lanes(),
        },
        "portal": portal_lanes(),
    }
