"""Refresh the dashboard's pinned public map data and offline fallback. No API key."""

import json
import re
from pathlib import Path

import httpx

AIRPORT_REVISION = "a89e4e0dda9d9878337e2da190ebe1ef1d6af705"
WORLD_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json"
TARGET = Path(__file__).resolve().parents[1] / "app/ui/static/geo"


def main() -> None:
    TARGET.mkdir(exist_ok=True)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        world = client.get(WORLD_URL)
        world.raise_for_status()
        topology = world.json()
        assert topology["type"] == "Topology" and topology["objects"]["countries"]
        (TARGET / "world.json").write_text(json.dumps(topology, separators=(",", ":")))
        base = f"https://raw.githubusercontent.com/mwgg/Airports/{AIRPORT_REVISION}"
        response = client.get(base + "/airports.json")
        response.raise_for_status()
        airports = {}
        for item in response.json().values():
            code = item.get("iata", "")
            if (
                re.fullmatch(r"[A-Z]{3}", code)
                and -90 <= item["lat"] <= 90
                and -180 <= item["lon"] <= 180
            ):
                airports[code] = {k: item[k] for k in ("name", "city", "lat", "lon")}
        (TARGET / "airports.json").write_text(
            json.dumps(airports, separators=(",", ":")), encoding="utf-8"
        )
        license_text = client.get(base + "/LICENSE")
        license_text.raise_for_status()
        (TARGET / "NOTICE.txt").write_text(
            f"World boundaries: Natural Earth public-domain data via {WORLD_URL}\n"
            f"Airport coordinates: https://github.com/mwgg/Airports at {AIRPORT_REVISION}\n\n"
            + license_text.text,
            encoding="utf-8",
        )
    print(f"Bundled {len(airports)} airport coordinates and world geometry")


if __name__ == "__main__":
    main()
