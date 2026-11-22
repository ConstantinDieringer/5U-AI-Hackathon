"""Airport alerts for the dashboard map: feed parsing, distance matching, caching, endpoint."""

import dataclasses

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.ui import airport_events, server

GDACS = {
    "type": "FeatureCollection",
    "features": [
        {  # hurricane 400 km from MIA: counts (cyclone radius 900 km)
            "geometry": {"type": "Point", "coordinates": [-77.0, 23.5]},
            "properties": {
                "eventtype": "TC",
                "eventid": 1001,
                "name": "Tropical Cyclone MELISSA-26",
                "alertlevel": "Red",
                "country": "Bahamas",
                "fromdate": "2026-11-20T00:00:00",
                "todate": "2026-11-23T00:00:00",
                "iscurrent": "true",
                "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1001"},
            },
        },
        {  # earthquake far from every airport we fly
            "geometry": {"type": "Point", "coordinates": [-166.8, -11.9]},
            "properties": {
                "eventtype": "EQ",
                "eventid": 2,
                "alertlevel": "Green",
                "iscurrent": "true",
            },
        },
        {  # finished event is ignored
            "geometry": {"type": "Point", "coordinates": [-80.3, 25.8]},
            "properties": {
                "eventtype": "FL",
                "eventid": 3,
                "alertlevel": "Orange",
                "iscurrent": "false",
            },
        },
        {"geometry": {"type": "Point"}, "properties": {"eventtype": "VO"}},  # malformed: dropped
        {  # foreign link is not passed on
            "geometry": {"type": "Point", "coordinates": [139.8, 35.8]},
            "properties": {
                "eventtype": "EQ",
                "eventid": 4,
                "alertlevel": "Orange",
                "iscurrent": "true",
                "url": {"report": "https://evil.example/x"},
            },
        },
    ],
}

FAA = """<AIRPORT_STATUS_INFORMATION>
<Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List>
<Delay><ARPT>DFW</ARPT><Reason>TM Initiatives:MIT:WX</Reason>
<Arrival_Departure Type="Departure"><Min>16 minutes</Min><Max>30 minutes</Max></Arrival_Departure></Delay>
</Arrival_Departure_Delay_List></Delay_type>
<Delay_type><Name>Ground Stop</Name><Ground_Stop_List>
<Program><ARPT>JFK</ARPT><Reason>thunderstorms</Reason><End_Time>7:45 pm EST</End_Time></Program>
</Ground_Stop_List></Delay_type>
<Delay_type><Name>Airport Closures</Name><Airport_Closure_List>
<Airport><ARPT>LAX</ARPT><Reason>!LAX AD AP CLSD TO NON SKED TRANSIENT GA ACFT</Reason><Reopen>May 28</Reopen></Airport>
<Airport><ARPT>ORD</ARPT><Reason>snow removal</Reason><Reopen>Nov 22 at 12:00 UTC.</Reopen></Airport>
<Airport><ARPT>x1</ARPT><Reason>bad code</Reason></Airport>
</Airport_Closure_List></Delay_type>
</AIRPORT_STATUS_INFORMATION>"""


@pytest.fixture
def feeds(monkeypatch):
    calls = {"gdacs": 0, "faa": 0}

    def gdacs():
        calls["gdacs"] += 1
        return airport_events.parse_gdacs(GDACS)

    def faa():
        calls["faa"] += 1
        return airport_events.parse_faa(FAA)

    monkeypatch.setattr(airport_events, "_fetch_gdacs", gdacs)
    monkeypatch.setattr(airport_events, "_fetch_faa", faa)
    monkeypatch.setattr(airport_events, "_fetch_weather", lambda codes: [])
    monkeypatch.setattr(airport_events, "_cache", {})
    return calls


def test_gdacs_keeps_current_valid_events_and_only_gdacs_links():
    hazards = airport_events.parse_gdacs(GDACS)
    assert [h.id for h in hazards] == ["gdacs-TC-1001", "gdacs-EQ-2", "gdacs-EQ-4"]
    assert hazards[0].level == "red" and hazards[0].radius_km == 900
    assert hazards[0].url.startswith("https://www.gdacs.org/")
    assert hazards[2].url == ""
    assert airport_events.parse_gdacs({"features": "nope"}) == []
    assert airport_events.parse_gdacs(None) == []


def test_faa_reads_stops_delays_and_cargo_relevant_closures():
    notices = {n.iata: n for n in airport_events.parse_faa(FAA)}
    assert set(notices) == {"DFW", "JFK", "ORD"}  # GA-only LAX closure and bad code dropped
    assert (
        notices["DFW"].detail
        == "traffic management, spacing, weather · departure 16 minutes to 30 minutes"
    )
    assert notices["JFK"].kind == "Ground stop" and notices["JFK"].level == "red"
    assert "reopens Nov 22" in notices["ORD"].detail


def test_events_match_airports_by_radius_and_sort_by_severity(feeds):
    result = airport_events.events_for(["MIA", "DFW", "JFK", "FRA", "bad", "MIA"])
    assert result["sources"] == [
        {"name": "GDACS", "ok": True},
        {"name": "FAA", "ok": True},
        {"name": "Open-Meteo", "ok": True},
    ]
    titles = [e["title"] for e in result["events"]]
    # red before orange; within a level the closer event first (a direct airport report is 0 km)
    assert titles == ["Ground stop at JFK", "Tropical Cyclone MELISSA-26", "Delays at DFW"]
    hurricane = result["events"][1]
    assert [a["iata"] for a in hurricane["airports"]] == ["MIA"]
    assert 300 < hurricane["airports"][0]["distance_km"] < 500


def test_feeds_are_cached_and_a_failure_keeps_the_last_good_state(feeds, monkeypatch):
    airport_events.events_for(["MIA"])
    airport_events.events_for(["JFK"])
    assert feeds == {"gdacs": 1, "faa": 1}

    def down():
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(airport_events, "_fetch_gdacs", down)
    monkeypatch.setattr(
        airport_events,
        "settings",
        dataclasses.replace(airport_events.settings, airport_events_ttl_s=0),
    )
    result = airport_events.events_for(["MIA"])
    assert {"name": "GDACS", "ok": False} in result["sources"]
    assert result["events"][0]["title"] == "Tropical Cyclone MELISSA-26"


def test_switched_off_makes_no_calls(feeds, monkeypatch):
    monkeypatch.setattr(
        airport_events,
        "settings",
        dataclasses.replace(airport_events.settings, airport_events=False),
    )
    assert airport_events.events_for(["MIA"]) == {"enabled": False, "sources": [], "events": []}
    assert feeds == {"gdacs": 0, "faa": 0}


def test_endpoint_validates_the_airport_list(feeds, tmp_path):
    db.init_db(str(tmp_path / "events.db"))
    client = TestClient(server.app)
    ok = client.get("/api/airport-events", params={"airports": "mia, dfw"})
    assert ok.status_code == 200
    assert {a["iata"] for e in ok.json()["events"] for a in e["airports"]} == {"MIA", "DFW"}
    too_many = ",".join(f"A{i:02d}" for i in range(301))
    assert client.get("/api/airport-events", params={"airports": too_many}).status_code == 422


def test_rates_endpoint_lists_tariff_breaks_as_decimal_strings(tmp_path):
    db.init_db(str(tmp_path / "rates.db"))
    body = TestClient(server.app).get("/api/rates").json()
    lane = body["tariff"]["lanes"][0]
    assert {
        "airline",
        "origin",
        "destination",
        "minimum",
        "breaks",
        "fuel",
        "security",
        "currency",
    } <= set(lane)
    assert all(isinstance(b["rate"], str) for b in lane["breaks"])
    assert [float(b["from_kg"]) for b in lane["breaks"]] == sorted(
        float(b["from_kg"]) for b in lane["breaks"]
    )
    assert body["portal"]["lanes"] and "airlines" in body["portal"]["lanes"][0]
