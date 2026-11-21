"""Start-kit CLI and reusable monthly parsers.

Usage: python -m app.pricing.parse_tariffs INPUT_DIR OUTPUT_JSON [--allow-image-gateway]
The exact original kit JPGs reuse the checked-in transcription, without a model call.
New images require the explicit CLI flag; the background tariff agent always extracts them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from app.config import ROOT
from app.pricing.tariff_parser import parse_attachment, parse_images


def parse_kit(directory: Path, *, allow_image_gateway: bool = False) -> dict:
    seed = json.loads((ROOT / "data/tariffs.json").read_text(encoding="utf-8"))
    originals = sorted((ROOT / "data/start-kit").glob("danube*.jpg"))
    original_hashes = {hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
    paths = sorted(
        p
        for p in directory.iterdir()
        if "tariff" in p.name.lower()
        and p.suffix.lower() in (".xlsx", ".docx", ".txt", ".pdf", ".jpg", ".jpeg", ".png")
    )
    images = [p for p in paths if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    documents = [parse_attachment(p) for p in paths if p not in images]
    lanes = []
    periods = []
    if images and {hashlib.sha256(p.read_bytes()).hexdigest() for p in images} == original_hashes:
        lanes.extend(
            lane.copy() for lane in seed["lanes"] if lane.get("source") == "transcribed-from-jpg"
        )
        periods.append((seed["valid_from"], seed["valid_to"]))
    elif images:
        if not allow_image_gateway:
            raise ValueError("New image tariffs require --allow-image-gateway")
        groups: dict[str, list[Path]] = {}
        for p in images:
            groups.setdefault(re.sub(r"-p\d+$", "", p.stem), []).append(p)
        documents.extend(parse_images(group[0], group) for group in groups.values())
    for doc in documents:
        period = (doc.valid_from.isoformat(), doc.valid_to.isoformat())
        periods.append(period)
        for lane in doc.lanes:
            lanes.append(
                {
                    **lane.model_dump(mode="json"),
                    "airline": doc.airline,
                    "currency": doc.currency,
                    "valid_from": period[0],
                    "valid_to": period[1],
                }
            )
    if not lanes:
        raise ValueError("No tariff lanes found")
    return {
        "currency": "EUR",
        "valid_from": min(p[0] for p in periods),
        "valid_to": max(p[1] for p in periods),
        "volume_factor": 167,
        "lanes": lanes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--allow-image-gateway", action="store_true")
    args = parser.parse_args()
    data = parse_kit(args.input, allow_image_gateway=args.allow_image_gateway)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(args.output)
    print(f"Parsed {len(data['lanes'])} lanes")


if __name__ == "__main__":
    main()
