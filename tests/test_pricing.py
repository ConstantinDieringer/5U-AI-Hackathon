"""Hand-calculated prices from the start-kit sheets. Every rate-sheet trap gets a case here."""

from decimal import Decimal as D

from app.pricing.pricing import (
    load_lanes,
    price_lane,
)


def lane(airline, origin, dest):
    return next(
        x for x in load_lanes() if (x.airline, x.origin, x.destination) == (airline, origin, dest)
    )


# Yellowtail FRA-DFW: min 109.00 | 0:3.76 45:2.78 100:2.42 250:2.20 500:1.69 ... | fuel .85 sec .25
YT_DFW = ("Yellowtail Air Freight", "FRA", "DFW")


def test_volume_weight_beats_actual_weight():
    # 50 kg but 1 cbm -> 167 kg chargeable: 167 x 2.42 = 404.14; 167 x 1.10 = 183.70 -> 587.84
    p = price_lane(lane(*YT_DFW), 50, 1.0)
    assert p.chargeable_kg == D(167)
    assert p.best_total == D("587.84")


def test_minimum_applies_to_freight_only():
    # 5 kg: 5 x 3.76 = 18.80 < minimum 109.00; surcharges still 5 x 1.10 = 5.50 -> 114.50
    assert price_lane(lane(*YT_DFW), 5).best_total == D("114.50")


def test_higher_break_taken_when_cheaper():
    # Yellowtail HAM-BOG, fuel 1.43 + sec 0.23 = 1.66/kg
    # 999 kg (+500 break 2.44): 2437.56 + 1658.34 = 4095.90
    # billed 1000 kg (+1000 break 1.76): 1760.00 + 1660.00 = 3420.00
    p = price_lane(lane("Yellowtail Air Freight", "HAM", "BOG"), 999)
    assert p.plain_total == D("4095.90")
    assert (p.best_total, p.billed_kg) == (D("3420.00"), D(1000))
