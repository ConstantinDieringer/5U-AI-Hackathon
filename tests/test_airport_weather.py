"""Airport weather alerts from the hourly forecast: thresholds, levels and matching."""

import pytest

from app.ui import airport_events, airport_weather


def hours(n=24, **series):
    times = [f"2026-11-22T{h % 24:02d}:00" for h in range(n)]
    base = {
        "weather_code": [3] * n,
        "wind_gusts_10m": [20.0] * n,
        "visibility": [20000.0] * n,
        "snowfall": [0.0] * n,
        "precipitation": [0.0] * n,
    }
    for key, overrides in series.items():
        for i, value in overrides.items():
            base[key][i] = value
    return {"hourly": {"time": times, **base}}


def kinds(alerts):
    return {a.kind: a.level for a in alerts}


def test_calm_weather_gives_no_alert():
    assert airport_weather.alerts_for("FRA", hours()) == []


def test_thunderstorm_with_hail_is_severe_and_suppresses_rain():
    loc = hours(weather_code={14: 95, 15: 96}, precipitation={15: 20.0})
    alerts = airport_weather.alerts_for("MIA", loc)
    assert kinds(alerts) == {"Thunderstorms": "red"}
    assert "with hail" in alerts[0].detail and alerts[0].starts_at == "2026-11-22T14:00Z"


@pytest.mark.parametrize(
    ("series", "expected"),
    [
        ({"wind_gusts_10m": {9: 72.0}}, {"Strong wind": "orange"}),
        ({"wind_gusts_10m": {9: 95.0}}, {"Strong wind": "red"}),
        ({"snowfall": {3: 2.0, 4: 2.0}}, {"Snow": "orange"}),
        ({"visibility": {5: 150.0}}, {"Low visibility": "orange"}),
        ({"visibility": {5: 800.0}}, {"Low visibility": "green"}),
        ({"precipitation": {2: 9.0}}, {"Heavy rain": "orange"}),
        ({"weather_code": {1: 66}}, {"Freezing rain": "orange"}),
    ],
)
def test_thresholds(series, expected):
    assert kinds(airport_weather.alerts_for("ORD", hours(**series))) == expected


def test_malformed_points_are_ignored_and_counts_must_match():
    loc = hours()
    loc["hourly"]["wind_gusts_10m"][3] = None
    loc["hourly"]["visibility"] = []
    assert airport_weather.alerts_for("FRA", loc) == []
    assert airport_weather.alerts_for("FRA", {"hourly": "nope"}) == []
    with pytest.raises(ValueError):
        airport_weather.parse_forecast([hours()], ["FRA", "JFK"])


def test_weather_alerts_join_the_feed(monkeypatch):
    monkeypatch.setattr(airport_events, "_cache", {})
    monkeypatch.setattr(airport_events, "_fetch_gdacs", list)
    monkeypatch.setattr(airport_events, "_fetch_faa", list)
    monkeypatch.setattr(
        airport_events,
        "_fetch_weather",
        lambda codes: airport_weather.parse_forecast(
            [hours(visibility={5: 90.0}) for _ in codes], codes
        ),
    )
    result = airport_events.events_for(["FRA", "MUC"])
    assert [e["title"] for e in result["events"]] == [
        "Low visibility at FRA",
        "Low visibility at MUC",
    ]
    assert result["events"][0]["source"] == "Open-Meteo"
    assert {"name": "Open-Meteo", "ok": True} in result["sources"]
