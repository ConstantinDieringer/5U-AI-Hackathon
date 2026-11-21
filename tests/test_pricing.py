"""Hand-calculated prices from the start-kit sheets. Every rate-sheet trap gets a case here."""

from datetime import date
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from app.models import RateOption, Rfq
from app.pricing.pricing import chargeable_kg, load_lanes, pick, price_lane, sheet_options


def lane(airline, origin, dest):
    return next(
        x for x in load_lanes() if (x.airline, x.origin, x.destination) == (airline, origin, dest)
    )


# Yellowtail FRA-DFW: min 109.00 | 0:3.76 45:2.78 100:2.42 250:2.20 500:1.69 ... | fuel .85 sec .25
YT_DFW = ("Yellowtail Air Freight", "FRA", "DFW")


def test_normal_weight():
    # 120 kg -> +100 break: 120 x 2.42 = 290.40; surcharges 120 x 1.10 = 132.00 -> 422.40
    p = price_lane(lane(*YT_DFW), 120)
    assert p.plain_total == p.best_total == D("422.40")
    assert p.billed_kg == D(120)


def test_volume_weight_beats_actual_weight():
    # 50 kg but 1 cbm -> 167 kg chargeable: 167 x 2.42 = 404.14; 167 x 1.10 = 183.70 -> 587.84
    p = price_lane(lane(*YT_DFW), 50, 1.0)
    assert p.chargeable_kg == D(167)
    assert p.best_total == D("587.84")


def test_minimum_applies_to_freight_only():
    # 5 kg: 5 x 3.76 = 18.80 < minimum 109.00; surcharges still 5 x 1.10 = 5.50 -> 114.50
    assert price_lane(lane(*YT_DFW), 5).best_total == D("114.50")


def test_higher_break_not_taken_when_surcharges_eat_the_saving():
    # 90 kg: 90 x 2.78 + 90 x 1.10 = 250.20 + 99.00 = 349.20
    # billed at 100: 100 x 2.42 + 100 x 1.10 = 242.00 + 110.00 = 352.00 -> stay at 90 kg
    p = price_lane(lane(*YT_DFW), 90)
    assert (p.best_total, p.billed_kg) == (D("349.20"), D(90))


def test_higher_break_taken_when_cheaper():
    # Yellowtail HAM-BOG, fuel 1.43 + sec 0.23 = 1.66/kg
    # 999 kg (+500 break 2.44): 2437.56 + 1658.34 = 4095.90
    # billed 1000 kg (+1000 break 1.76): 1760.00 + 1660.00 = 3420.00
    p = price_lane(lane("Yellowtail Air Freight", "HAM", "BOG"), 999)
    assert p.plain_total == D("4095.90")
    assert (p.best_total, p.billed_kg) == (D("3420.00"), D(1000))


def test_breakpoint_switch_off_uses_plain_total():
    rfq = Rfq(origin="HAM", destination="BOG", weight_kg=999)
    yt = next(
        o for o in sheet_options(rfq, use_breakpoints=False) if o.airline.startswith("Yellow")
    )
    assert (yt.total_minor, yt.billed_kg) == (409590, 999.0)


def test_first_bracket_below_45kg_kestrel():
    # regression for the "-45" header: Kestrel CGN-MIA min 76.00, 0:3.05, fuel .69 sec .24
    # 30 kg: 30 x 3.05 = 91.50 (> min) + 30 x 0.93 = 27.90 -> 119.40
    assert price_lane(lane("Kestrel Airways", "CGN", "MIA"), 30).plain_total == D("119.40")


def test_two_airlines_on_one_lane_cheapest_and_fastest():
    # FRA-DFW 120 kg: Yellowtail 422.40 (2 days) vs Elbflug 120 x 1.57 + 120 x 0.88 = 294.00 (3 days)
    opts = sheet_options(Rfq(origin="FRA", destination="DFW", weight_kg=120))
    assert {o.airline: o.total_minor for o in opts} == {
        "Yellowtail Air Freight": 42240,
        "Elbflug Air Freight": 29400,
    }
    assert pick(opts, "cheapest").airline == "Elbflug Air Freight"
    assert pick(opts, "fastest").airline == "Yellowtail Air Freight"
    assert all(o.currency == "EUR" and o.source == "sheet" for o in opts)


def test_origin_from_pickup_zip():
    # ZIP 60311 -> zone 60 (Frankfurt) -> FRA
    opts = sheet_options(Rfq(pickup_zip="60311", destination="DFW", weight_kg=120))
    assert {o.origin for o in opts} == {"FRA"} and len(opts) == 2


def test_unknown_lane_gives_no_options():
    assert sheet_options(Rfq(origin="FRA", destination="ULN", weight_kg=100)) == []


def _opt(name, total, days):
    cents = int(D(total) * 100)
    return RateOption(name, "FRA", "XXX", days, cents, "EUR", cents, "bid")


def test_pick_balanced_50_50():
    # price range 100..300, transit 1..5 -> scores A .5, B .5, C .125 + .125 = .25
    opts = [_opt("A", "100", 5), _opt("B", "300", 1), _opt("C", "150", 2)]
    assert pick(opts, "balanced").airline == "C"


def test_pick_ties_and_errors():
    assert pick([], "cheapest") is None
    opts = [_opt("slow", "100", 5), _opt("fast", "100", 2)]
    assert pick(opts, "cheapest").airline == "fast"
    with pytest.raises(ValueError):
        pick(opts, "best")


def test_tariff_validity_window():
    # start-kit sheets are valid 2026-11-01 .. 2026-11-30
    rfq = Rfq(origin="FRA", destination="DFW", weight_kg=120)
    assert len(sheet_options(rfq, on=date(2026, 11, 30))) == 2
    assert sheet_options(rfq, on=date(2026, 12, 1)) == []


def test_chargeable_weight_is_not_rounded_like_the_portal():
    # portal: 10.3 kg, 0.1234 cbm -> chargeable "20.6078"
    assert chargeable_kg(10.3, 0.1234) == D("20.6078")


@pytest.mark.parametrize(
    "bad",
    [
        {"destination": "Frankfurt", "weight_kg": 10},
        {"destination": "DFW", "weight_kg": 0},
        {"destination": "DFW", "weight_kg": 10, "volume_cbm": -1},
        {"destination": "", "weight_kg": 10},
    ],
)
def test_rfq_rejects_invalid_llm_output(bad):
    with pytest.raises(ValidationError):
        Rfq(**bad)


def test_rfq_normalises_codes():
    r = Rfq(origin=" fra ", destination="dfw", weight_kg=1, pickup_zip=" ")
    assert (r.origin, r.destination, r.pickup_zip) == ("FRA", "DFW", None)


def test_rfq_accepts_icao_codes_and_drops_bad_origin_with_zip():
    # live round 4 (mail 937): the LLM wrote origin "EDDL" -> the RFQ failed
    r = Rfq.model_validate({"origin": "EDDL", "pickup_zip": "51896", "destination": "KORD",
                            "weight_kg": 679})
    assert (r.origin, r.destination) == ("DUS", "ORD")
    r = Rfq.model_validate({"origin": "XXXX", "pickup_zip": "51896", "destination": "ORD",
                            "weight_kg": 679})
    assert r.origin is None and r.pickup_zip == "51896"
