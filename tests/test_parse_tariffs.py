"""data/tariffs.json as produced by app/pricing/parse_tariffs.py (start kit, 2026-11)."""

import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

from app.pricing.reference import carrier_by_name

DATA = json.loads((Path(__file__).parents[1] / "data" / "tariffs.json").read_text(encoding="utf-8"))
LANES = DATA["lanes"]
ORIGINS = {"FRA", "MUC", "DUS", "HAM", "STR", "CGN"}


def test_lane_counts_per_airline():
    assert len(LANES) == 158
    assert Counter(lane["airline"] for lane in LANES) == {
        "Wayzata Air Cargo": 26,
        "Cobalt Skylift": 21,
        "Elbflug Air Freight": 19,
        "Kestrel Airways": 18,
        "Yellowtail Air Freight": 15,
        "Orion Cargo Air": 14,
        "Vega Skyfreight": 13,
        "Helix Air Freight": 12,
        "Quetzal Air Freight": 11,
        "Danube Wings Cargo": 9,
    }


def test_every_lane_complete_and_positive():
    for lane in LANES:
        assert lane["minimum"] > 0 and lane["fuel"] > 0 and lane["security"] > 0, lane
        assert lane["transit_days"] >= 1, lane
        assert all(b["rate"] > 0 for b in lane["breaks"]), lane


def test_first_break_starts_at_zero():
    # regression: "-45" column headers (Kestrel, Vega, Wayzata) were parsed as 45 kg
    for lane in LANES:
        assert lane["breaks"][0]["from_kg"] == 0, lane


def test_breaks_ascending_and_rates_never_rise():
    for lane in LANES:
        kgs = [b["from_kg"] for b in lane["breaks"]]
        rates = [b["rate"] for b in lane["breaks"]]
        assert kgs == sorted(set(kgs)), lane
        assert all(b <= a for a, b in pairwise(rates)), lane


def test_origins_and_served_destinations():
    for lane in LANES:
        assert lane["origin"] in ORIGINS, lane
        carrier = carrier_by_name(lane["airline"])
        assert carrier is not None, lane["airline"]
        assert lane["destination"] in carrier.serves, lane


def test_spot_checks_against_originals():
    def get(airline, o, d):
        return next(
            x for x in LANES if (x["airline"], x["origin"], x["destination"]) == (airline, o, d)
        )

    # Danube (JPG, transcribed): FRA-DXB 2 days, min 138,86, from 0 kg 2,90 ... fuel 1,12 sec 0,22
    dxb = get("Danube Wings Cargo", "FRA", "DXB")
    assert (dxb["transit_days"], dxb["minimum"], dxb["fuel"], dxb["security"]) == (
        2,
        138.86,
        1.12,
        0.22,
    )
    assert [b["rate"] for b in dxb["breaks"]] == [2.90, 2.23, 2.05, 1.66, 1.45, 1.33]
    # Kestrel (xlsx, D+N transit, surcharges in columns): FRA-BOM D+2, min 134,62, +2000 1,30
    bom = get("Kestrel Airways", "FRA", "BOM")
    assert (bom["transit_days"], bom["minimum"], bom["breaks"][-1]["rate"]) == (2, 134.62, 1.30)
    # Orion (pdf, decimal comma): MUC-HKG 3 days, min 111,30, from 1000 kg 2,15, fuel 0,96
    hkg = get("Orion Cargo Air", "MUC", "HKG")
    assert (hkg["transit_days"], hkg["minimum"], hkg["breaks"][-1]["rate"], hkg["fuel"]) == (
        3,
        111.30,
        2.15,
        0.96,
    )
    # Cobalt (pdf, lane block split across pages): DUS-SYD from 500 kg 2.24, security 0.11
    syd = get("Cobalt Skylift", "DUS", "SYD")
    assert (syd["breaks"][4]["rate"], syd["security"]) == (2.24, 0.11)
