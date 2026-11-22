"""Operational weather at our airports over the next 24 hours, from the Open-Meteo forecast.

Turns hourly forecasts into at most one alert per airport and phenomenon: thunderstorms,
freezing rain, snow, strong gusts, low visibility and heavy rain. Thresholds follow what slows
cargo handling and ramp work; they live here as data so they can be tuned in one place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

THUNDER = {95: "orange", 96: "red", 99: "red"}  # WMO codes; 96/99 come with hail
FREEZING = {56, 57, 66, 67}
# (threshold, level), most severe first
GUSTS_KMH = ((90.0, "red"), (65.0, "orange"), (50.0, "green"))
SNOW_CM = ((10.0, "red"), (3.0, "orange"), (0.5, "green"))
RAIN_MM_H = ((15.0, "red"), (8.0, "orange"), (4.0, "green"))
FOG_M = ((200.0, "orange"), (1000.0, "green"))


@dataclass(frozen=True)
class WeatherAlert:
    iata: str
    kind: str
    level: str
    detail: str
    starts_at: str
    ends_at: str


def _num(values: object, n: int) -> list[float]:
    """The hourly series as floats; missing or malformed points become 0."""
    out: list[float] = []
    for v in values[:n] if isinstance(values, list) else []:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out + [0.0] * (n - len(out))


def _band(value: float, bands: tuple[tuple[float, str], ...], below: bool = False) -> str | None:
    for limit, level in bands:
        if (value < limit) if below else (value >= limit):
            return level
    return None


def _span(times: list[str], hit: Callable[[int], bool]) -> tuple[str, str]:
    hours = [i for i in range(len(times)) if hit(i)]
    return (times[hours[0]] + "Z", times[hours[-1]] + "Z") if hours else ("", "")


def _hour(stamp: str) -> str:
    return stamp[11:16] + " UTC" if len(stamp) >= 16 else ""


def alerts_for(iata: str, location: dict[str, Any]) -> list[WeatherAlert]:
    hourly = location.get("hourly") if isinstance(location, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        return []
    times = [str(t) for t in hourly["time"]]
    n = len(times)
    if not n:
        return []
    codes = [int(c) for c in _num(hourly.get("weather_code"), n)]
    gusts = _num(hourly.get("wind_gusts_10m"), n)
    snow = _num(hourly.get("snowfall"), n)
    rain = _num(hourly.get("precipitation"), n)
    vis_raw = hourly.get("visibility")
    visibility = _num(vis_raw, n) if isinstance(vis_raw, list) and vis_raw else [99999.0] * n
    alerts: list[WeatherAlert] = []

    storm = [i for i in range(n) if codes[i] in THUNDER]
    if storm:
        level = "red" if any(THUNDER[codes[i]] == "red" for i in storm) else "orange"
        hail = " with hail" if level == "red" else ""
        alerts.append(
            WeatherAlert(
                iata,
                "Thunderstorms",
                level,
                f"Thunderstorms{hail} expected from {_hour(times[storm[0]])} for about {len(storm)} h",
                times[storm[0]] + "Z",
                times[storm[-1]] + "Z",
            )
        )
    ice = [i for i in range(n) if codes[i] in FREEZING]
    if ice:
        alerts.append(
            WeatherAlert(
                iata,
                "Freezing rain",
                "orange",
                f"Freezing rain or drizzle from {_hour(times[ice[0]])}, de-icing likely",
                times[ice[0]] + "Z",
                times[ice[-1]] + "Z",
            )
        )
    peak = max(range(n), key=lambda i: gusts[i])
    level = _band(gusts[peak], GUSTS_KMH)
    if level:
        start, end = _span(times, lambda i: gusts[i] >= GUSTS_KMH[-1][0])
        alerts.append(
            WeatherAlert(
                iata,
                "Strong wind",
                level,
                f"Gusts up to {round(gusts[peak])} km/h, strongest around {_hour(times[peak])}",
                start,
                end,
            )
        )
    total_snow = sum(snow)
    level = _band(total_snow, SNOW_CM)
    if level:
        start, end = _span(times, lambda i: snow[i] > 0)
        alerts.append(
            WeatherAlert(
                iata,
                "Snow",
                level,
                f"{total_snow:.1f} cm of snow expected over the next 24 hours",
                start,
                end,
            )
        )
    low = min(range(n), key=lambda i: visibility[i])
    level = _band(visibility[low], FOG_M, below=True)
    if level:
        foggy = sum(1 for v in visibility if v < FOG_M[-1][0])
        start, end = _span(times, lambda i: visibility[i] < FOG_M[-1][0])
        freezing = " Freezing fog." if 48 in codes else ""
        alerts.append(
            WeatherAlert(
                iata,
                "Low visibility",
                level,
                f"Visibility down to {round(visibility[low])} m around {_hour(times[low])}, {foggy} h below 1 km.{freezing}",
                start,
                end,
            )
        )
    wettest = max(range(n), key=lambda i: rain[i])
    level = _band(rain[wettest], RAIN_MM_H)
    if level and not storm:
        start, end = _span(times, lambda i: rain[i] >= RAIN_MM_H[-1][0])
        alerts.append(
            WeatherAlert(
                iata,
                "Heavy rain",
                level,
                f"Up to {rain[wettest]:.0f} mm per hour around {_hour(times[wettest])}",
                start,
                end,
            )
        )
    return alerts


def parse_forecast(payload: object, codes: list[str]) -> list[WeatherAlert]:
    """Open-Meteo answers one object per requested coordinate, in request order."""
    locations = payload if isinstance(payload, list) else [payload]
    if len(locations) != len(codes):
        raise ValueError("forecast does not match the requested airports")
    return [a for code, loc in zip(codes, locations, strict=True) for a in alerts_for(code, loc)]
