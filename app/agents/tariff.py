"""Durable tariff jobs, atomic runtime publication, and cache reload."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import db
from app.config import ROOT, settings
from app.mail import parse
from app.mail.router import address
from app.models import Mail
from app.pricing.pricing import reload_lanes
from app.pricing.reference import carrier_by_email
from app.pricing.tariff_parser import TariffDocument, parse_attachment, parse_images

log = logging.getLogger(__name__)
_write_lock = threading.Lock()
_wake = threading.Event()
SEED = ROOT / "data" / "tariffs.json"


@dataclass(frozen=True)
class TariffUpdate:
    document: TariffDocument
    mail_id: int
    attachment_hash: str
    received_at: str


def enqueue(mail: Mail) -> None:
    # Store before advancing the mailbox cursor; one worker resumes pending jobs on restart.
    db.queue_tariff(mail.id, mail.raw)
    _wake.set()


def publish(update: TariffUpdate, path: Path | None = None) -> bool:
    path = path or settings.runtime_dir / "tariffs.json"
    doc = update.document
    document = doc.model_dump(mode="json")
    version = f"{doc.airline}:{update.attachment_hash}"
    with _write_lock:
        data = json.loads((path if path.exists() else SEED).read_text(encoding="utf-8"))
        manifest = data.setdefault("imports", {})
        if version in manifest:
            return False
        stamp = datetime.fromisoformat(update.received_at)
        if stamp.tzinfo is None:
            raise ValueError("Tariff received_at must include timezone")
        for item in document["lanes"]:
            key = (
                doc.airline,
                item["origin"],
                item["destination"],
                document["valid_from"],
                document["valid_to"],
            )
            matches = [
                lane
                for lane in data["lanes"]
                if (
                    lane["airline"],
                    lane["origin"],
                    lane["destination"],
                    lane.get("valid_from", data["valid_from"]),
                    lane.get("valid_to", data["valid_to"]),
                )
                == key
            ]
            if any(
                datetime.fromisoformat(
                    lane.get("received_at", "1970-01-01T00:00:00+00:00")
                )
                > stamp
                for lane in matches
            ):
                continue
            data["lanes"] = [lane for lane in data["lanes"] if lane not in matches]
            data["lanes"].append(
                {
                    **item,
                    "airline": doc.airline,
                    "currency": doc.currency,
                    "valid_from": document["valid_from"],
                    "valid_to": document["valid_to"],
                    "mail_id": update.mail_id,
                    "attachment_hash": update.attachment_hash,
                    "received_at": update.received_at,
                }
            )
        manifest[version] = {
            "mail_id": update.mail_id,
            "received_at": update.received_at,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)
        reload_lanes()
    return True


def process(mail: Mail) -> None:
    from app.mail.poller import download_attachments

    carrier = carrier_by_email(address(mail.sender))
    if carrier is None or carrier.kind != "airline":
        raise ValueError("Tariff sender is not a known airline")
    paths = download_attachments(mail)
    if not paths:
        raise ValueError("Tariff mail has no attachments")
    # Parse and validate the whole mail before publishing any attachment.
    updates = []
    images = [p for p in paths if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    groups = [[p] for p in paths if p not in images] + ([images] if images else [])
    for group in groups:
        digest = hashlib.sha256(
            b"".join(hashlib.sha256(p.read_bytes()).digest() for p in group)
        ).hexdigest()
        runtime = settings.runtime_dir / "tariffs.json"
        if runtime.exists():
            with _write_lock:
                manifest = json.loads(runtime.read_text(encoding="utf-8")).get(
                    "imports", {}
                )
            if f"{carrier.name}:{digest}" in manifest:
                continue
        document = (
            parse_images(group[0], group)
            if group == images
            else parse_attachment(group[0])
        )
        if document.airline.casefold() != carrier.name.casefold():
            raise ValueError("Tariff airline does not match sender")
        for lane in document.lanes:
            if lane.destination not in carrier.serves:
                raise ValueError(
                    f"Tariff destination {lane.destination} is not served by sender"
                )
        document.airline = carrier.name
        updates.append(TariffUpdate(document, mail.id, digest, mail.received_at))
    for update in updates:
        if publish(update):
            db.emit(
                "tariff_updated",
                f"Tariffs updated: {carrier.name}",
                mail_id=mail.id,
                attachment_hash=update.attachment_hash,
                lanes=len(update.document.lanes),
            )


def run(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            jobs = db.pending_tariffs()
        except Exception:
            log.exception("Could not read tariff queue; retrying")
            stop.wait(1)
            continue
        for job in jobs:
            if stop.is_set():
                return
            try:
                process(parse.mail_from_api(json.loads(job["raw_json"])))
            except Exception as exc:
                log.exception("Tariff mail %s failed", job["mail_id"])
                db.emit(
                    "tariff_failed",
                    f"Tariff mail {job['mail_id']}: {type(exc).__name__}: {exc}",
                    level="warn",
                )
                db.finish_tariff(job["mail_id"], str(exc))
            else:
                db.finish_tariff(job["mail_id"])
        _wake.wait(1)
        _wake.clear()
