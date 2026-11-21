"""Deterministic pricing for the rate-sheet airlines and option selection. No LLM.

Rules (identical wording on all 10 sheets):
- chargeable weight = max(actual weight, volume_cbm x 167)
- rate per kg from the weight break of the billed weight
- the minimum applies to the freight only, before surcharges
- fuel + security per kg of the billed weight

Break-point optimisation: billing at the lower bound of a higher break can be cheaper
(surcharges then also apply to that higher weight, so compare all-in totals). Whether 5U's
scorer counts this as "best price" is NOT confirmed -> `use_breakpoints` switch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

from app.models import RateOption, Rfq
from app.pricing.reference import origin_for_zip

VOLUME_FACTOR = Decimal(167)
CENT = Decimal("0.01")
TARIFFS = Path(__file__).resolve().parents[2] / "data" / "tariffs.json"


def money(x: Decimal) -> Decimal:
    """The one place where amounts are rounded (to cents)."""
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def to_minor(x: Decimal) -> int:
    """Rounded amount -> integer cents (the unit of RateOption and the DB)."""
    return int(money(x) * 100)


def dec(x: float | str) -> Decimal:
    return Decimal(str(x))


@dataclass(frozen=True)
class SheetLane:
    airline: str
    origin: str
    destination: str
    transit_days: int
    minimum: Decimal
    breaks: tuple[tuple[Decimal, Decimal], ...]  # (from_kg, rate per kg), ascending
    fuel: Decimal
    security: Decimal
    currency: str = "EUR"


@dataclass(frozen=True)
class PricedLane:
    lane: SheetLane
    chargeable_kg: Decimal
    plain_total: Decimal  # billed at the chargeable weight
    best_total: Decimal  # cheapest of plain and every higher break
    billed_kg: Decimal  # weight behind best_total


@lru_cache(maxsize=4)
def load_lanes(path: Path = TARIFFS) -> tuple[SheetLane, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    currency = data.get("currency", "EUR")
    return tuple(
        SheetLane(
            airline=lane["airline"],
            origin=lane["origin"],
            destination=lane["destination"],
            transit_days=int(lane["transit_days"]),
            minimum=dec(lane["minimum"]),
            breaks=tuple(sorted((dec(b["from_kg"]), dec(b["rate"])) for b in lane["breaks"])),
            fuel=dec(lane["fuel"]),
            security=dec(lane["security"]),
            currency=currency,
        )
        for lane in data["lanes"]
    )


def chargeable_kg(weight_kg: float, volume_cbm: float = 0.0) -> Decimal:
    return max(dec(weight_kg), dec(volume_cbm) * VOLUME_FACTOR)


def _rate(lane: SheetLane, kg: Decimal) -> Decimal:
    rate = lane.breaks[0][1]
    for from_kg, r in lane.breaks:
        if kg >= from_kg:
            rate = r
    return rate


def _total(lane: SheetLane, billed_kg: Decimal) -> Decimal:
    freight = max(lane.minimum, _rate(lane, billed_kg) * billed_kg)
    return money(freight + (lane.fuel + lane.security) * billed_kg)


def price_lane(lane: SheetLane, weight_kg: float, volume_cbm: float = 0.0) -> PricedLane:
    cw = chargeable_kg(weight_kg, volume_cbm)
    plain = _total(lane, cw)
    best, billed = plain, cw
    for from_kg, _ in lane.breaks:
        if from_kg > cw:
            t = _total(lane, from_kg)
            if t < best:
                best, billed = t, from_kg
    return PricedLane(lane, cw, plain, best, billed)


def resolve_origin(rfq: Rfq) -> str | None:
    if rfq.origin:
        return rfq.origin.upper()
    return origin_for_zip(rfq.pickup_zip) if rfq.pickup_zip else None


def sheet_options(rfq: Rfq, use_breakpoints: bool = True) -> list[RateOption]:
    """All rate-sheet offers for the RFQ's lane (airport to airport, trucking not included)."""
    origin = resolve_origin(rfq)
    if origin is None:
        return []
    dest = rfq.destination.upper()
    out: list[RateOption] = []
    for lane in load_lanes():
        if lane.origin != origin or lane.destination != dest:
            continue
        p = price_lane(lane, rfq.weight_kg, rfq.volume_cbm)
        total = p.best_total if use_breakpoints else p.plain_total
        billed = p.billed_kg if use_breakpoints else p.chargeable_kg
        out.append(
            RateOption(
                airline=lane.airline,
                origin=origin,
                destination=dest,
                transit_days=lane.transit_days,
                total_minor=to_minor(total),
                currency=lane.currency,
                total_eur_minor=to_minor(total),  # all sheets are EUR
                source="sheet",
                chargeable_kg=float(p.chargeable_kg),
                billed_kg=float(billed),
            )
        )
    return out


def pick(options: list[RateOption], mode: str) -> RateOption | None:
    """mode: 'cheapest' | 'fastest' | 'balanced' (price and transit 50/50, min-max normalised).

    Ties: cheapest -> faster wins; fastest -> cheaper wins.
    """
    if not options:
        return None
    if mode == "cheapest":
        return min(options, key=lambda o: (o.total_eur_minor, o.transit_days))
    if mode == "fastest":
        return min(options, key=lambda o: (o.transit_days, o.total_eur_minor))
    if mode != "balanced":
        raise ValueError(f"unknown mode: {mode}")
    prices = [Decimal(o.total_eur_minor) for o in options]
    days = [Decimal(o.transit_days) for o in options]

    def norm(v: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
        return Decimal(0) if hi == lo else (v - lo) / (hi - lo)

    return min(
        options,
        key=lambda o: (
            norm(Decimal(o.total_eur_minor), min(prices), max(prices)) / 2
            + norm(Decimal(o.transit_days), min(days), max(days)) / 2,
            o.total_eur_minor,
        ),
    )


if __name__ == "__main__":
    import sys

    o, d, kg = sys.argv[1], sys.argv[2], float(sys.argv[3])
    cbm = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    rfq = Rfq(origin=o, destination=d, weight_kg=kg, volume_cbm=cbm)
    opts = sheet_options(rfq)
    for x in sorted(opts, key=lambda x: x.total_eur_minor):
        print(x)
    for m in ("cheapest", "fastest", "balanced"):
        print(m, "->", pick(opts, m))
