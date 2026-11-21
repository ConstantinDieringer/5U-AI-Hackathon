"""Portal parsing (real response fixture), lane index and start-kit reference data. No network."""

import json
from pathlib import Path

import pytest

from app.models import Rfq
from app.pricing import portal_sync
from app.pricing.lanes import airlines_for
from app.pricing.portal_sync import parse_rate, portal_airlines, portal_options
from app.pricing.reference import (
    carrier_by_email,
    origin_for_zip,
    truckers_for_zone,
    zone_for_zip,
)

FIX = Path(__file__).parent / "fixtures"
RATE = json.loads((FIX / "portal_rate_eur.json").read_text())


def test_parse_real_eur_rate():
    r = parse_rate(RATE)
    assert (r.airline, r.origin, r.destination, r.transit_days) == (
        "Juniper Cargo Lines",
        "CGN",
        "DEL",
        5,
    )
    assert (r.total_minor, r.currency, r.total_eur_minor) == (44520, "EUR", 44520)
    assert r.rate_id == "R-EM429I1C6zPWNmaz" and r.source == "portal"


def test_parse_usd_rate_converts_with_stated_fx():
    raw = {**RATE, "total": "100.00", "currency": "USD", "fx_eur_per_usd": "0.92"}
    r = parse_rate(raw)
    assert (r.total_minor, r.currency, r.total_eur_minor, r.fx_eur_per_unit) == (
        10000,
        "USD",
        9200,
        "0.92",
    )


def test_parse_rejects_bad_shape():
    with pytest.raises(ValueError):
        parse_rate({"airline": "X"})
    with pytest.raises(ValueError):
        parse_rate({**RATE, "currency": "USD"})  # no fx stated


def test_portal_options_skips_failing_airline(monkeypatch):
    monkeypatch.setattr(portal_sync, "_cache", {})
    calls = []

    def fake_fetch(o, d, airline, kg, cbm):
        calls.append(airline)
        if airline == "UV":
            raise RuntimeError("503 book by email")
        return {**RATE, "airline_code": airline}

    assert [a["code"] for a in portal_airlines("CGN", "DEL")] == ["JP", "UV"]
    opts = portal_options(Rfq(origin="CGN", destination="DEL", weight_kg=120), fetch=fake_fetch)
    assert calls == ["JP", "UV"] and len(opts) == 1
    # second call within the cache window does not hit the rate-limited endpoint for JP again
    portal_options(Rfq(origin="CGN", destination="DEL", weight_kg=120), fetch=fake_fetch)
    assert calls == ["JP", "UV", "UV"]


def test_lane_index_channels():
    by_name = {a.name: a for a in airlines_for("FRA", "DFW")}
    assert by_name["Yellowtail Air Freight"].channel == "sheet"
    assert by_name["Elbflug Air Freight"].channel == "sheet"
    assert all(a.email and a.email.endswith("@hackloop.mail") for a in by_name.values())
    # destinations nobody prices on sheet or portal -> email bids only
    assert {a.channel for a in airlines_for("FRA", "ULN")} == {"email"}


def test_reference_zip_zone_and_carriers():
    assert zone_for_zip("D-60311") == "60" and origin_for_zip("60311") == "FRA"
    assert origin_for_zip("80331") == "MUC" and origin_for_zip("99999") is None
    assert "Taunusfracht" in {t.name for t in truckers_for_zone("60")}
    c = carrier_by_email("Cobalt-Skylift@HACKLOOP.mail")
    assert c is not None and c.kind == "airline" and "JFK" in c.serves
