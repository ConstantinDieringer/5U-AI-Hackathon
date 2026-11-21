"""Lane index: which airlines can price origin -> destination, and through which channel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.pricing.portal_sync import portal_airlines
from app.pricing.pricing import load_lanes
from app.pricing.reference import airlines_serving, carrier_by_name

Channel = Literal["sheet", "portal", "email"]


@dataclass(frozen=True)
class LaneAirline:
    name: str
    channel: Channel  # sheet = priced locally, portal = /v1/portal/rates, email = bid request only
    code: str | None = None  # portal airline code
    email: str | None = None


def airlines_for(origin: str, destination: str) -> list[LaneAirline]:
    """Sheet and portal airlines on the lane, plus every other airline serving the destination
    (those can only be asked for a bid by email)."""
    o, d = origin.upper(), destination.upper()
    found: dict[str, LaneAirline] = {}
    for lane in load_lanes():
        if lane.origin == o and lane.destination == d:
            c = carrier_by_name(lane.airline)
            found[lane.airline] = LaneAirline(lane.airline, "sheet", email=c.email if c else None)
    for a in portal_airlines(o, d):
        if a["name"] not in found:
            c = carrier_by_name(a["name"])
            found[a["name"]] = LaneAirline(a["name"], "portal", a["code"], c.email if c else None)
    for c in airlines_serving(d):
        if c.name not in found:
            found[c.name] = LaneAirline(c.name, "email", email=c.email)
    return list(found.values())
