"""Vendor the pinned MapLibre browser modules and licenses without a build dependency."""

import io
import tarfile
from pathlib import Path

import httpx

VERSION = "6.19.0"


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "app/ui/static/vendor/maplibre"
    target.mkdir(parents=True, exist_ok=True)
    response = httpx.get(
        f"https://registry.npmjs.org/maplibre-gl/-/maplibre-gl-{VERSION}.tgz", timeout=60
    )
    response.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(response.content)) as archive:
        for name in (
            "dist/maplibre-gl.mjs",
            "dist/maplibre-gl-worker.mjs",
            "dist/maplibre-gl.css",
            "LICENSE.txt",
        ):
            source = archive.extractfile("package/" + name)
            if source is None:
                raise ValueError("package is missing " + name)
            (target / Path(name).name).write_bytes(source.read())
    print(f"Vendored MapLibre GL JS {VERSION}")


if __name__ == "__main__":
    main()
