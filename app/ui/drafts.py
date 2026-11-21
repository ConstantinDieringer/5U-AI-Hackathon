"""Emergency quote draft for a human: when the agent failed, compute the best quote from
everything already known (stored request, rate sheets, portal, carrier bids received in
this session) and return a ready-to-send mail in the house style. Nothing is sent here.

The human checks the draft in the Live tab and sends it with the normal reply button.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from app import db
from app.agents import bids, rfq
from app.mail import language
from app.models import Mail, Rfq
from app.pricing.portal_sync import portal_result
from app.pricing.pricing import resolve_origin, sheet_options, to_minor
from app.pricing.reference import carrier_by_name

log = logging.getLogger(__name__)
PORTAL_BUDGET_S = (
    8.0  # a human is waiting: short portal window, sheets and bids still count
)
_BID = re.compile(r"^Gebot (.+?): ([\d.]+) EUR(?:, ([\d.]+) Tage)?")


def _first_mail(session: dict[str, Any]) -> Mail | None:
    m = next((x for x in session["mails"] if x["direction"] == "in"), None)
    if not m:
        return None
    return Mail(
        id=int(m["id"]),
        thread_root=int(m["id"]),
        sender=str(m["sender"] or ""),
        to=str(m["recipient"] or ""),
        subject=str(m["subject"] or ""),
        body=str(m["body"] or ""),
        received_at=str(m["received_at"]),
    )


def _session_offers(session: dict[str, Any]) -> list[bids.Offer]:
    """Carrier bids this session already received, rebuilt from their events."""
    out = []
    for e in session["events"]:
        if e["kind"] != "bid_received":
            continue
        m = _BID.match(str(e["message"] or ""))
        carrier = carrier_by_name(m[1]) if m else None
        if not (m and carrier):
            continue
        days = float(m[3]) if m[3] else None
        out.append(bids.Offer(carrier, Decimal(m[2]), days, 0))
    return out


def build(session_id: int) -> dict[str, Any]:
    s = db.get_session(session_id)
    if not s:
        raise LookupError("session not found")
    mail = _first_mail(s)
    if mail is None:
        raise ValueError("no customer mail in this session")
    warnings: list[str] = []
    stored = s.get("request")
    try:
        request = Rfq.model_validate(stored) if isinstance(stored, dict) else None
    except Exception:  # noqa: BLE001 - a broken stored request is re-extracted
        request = None
    if request is None:
        warnings.append("Anfrage war nicht gespeichert: neu ausgelesen (1 LLM-Call)")
        request = rfq.extract_rfq(mail, session_id)
        if request is None:
            raise ValueError("Anfrage nicht lesbar: bitte Preis von Hand eintragen")
    # same as the agent: the language of the mail itself, not the LLM's guess from names
    request = request.model_copy(update={"language": language.detect(mail.body)})
    request = rfq.locate_origin(
        rfq.locate_pickup(request, mail, session_id), mail, session_id
    )
    origin = resolve_origin(request)
    if origin is None:
        raise ValueError(
            "kein Abflughafen erkennbar: Ort oder PLZ beim Kunden erfragen"
        )

    air = sheet_options(request)
    try:
        portal = portal_result(request, deadline=time.monotonic() + PORTAL_BUDGET_S)
        air += portal.options
        if portal.errors:
            warnings.append("Portal teilweise nicht erreichbar")
    except Exception as e:  # noqa: BLE001 - sheets and bids still give a draft
        warnings.append(f"Portal nicht erreichbar ({type(e).__name__})")
    from app.agents import carrier_prefs
    from app.mail.router import address

    banned = carrier_prefs.excluded(address(mail.sender))
    liked = carrier_prefs.preferred(address(mail.sender))
    air = [o for o in air if o.airline.lower() not in banned]
    offers = [o for o in _session_offers(s) if o.carrier.name.lower() not in banned]
    chargeable = air[0].chargeable_kg if air else 0.0
    bid_opts = [
        bids.airline_option(o, request, origin, chargeable)
        for o in offers
        if o.carrier.kind == "airline"
    ]
    choice = rfq.best_air(air, bid_opts, request.preference)
    if liked:
        fav = rfq.best_air(
            [o for o in air if o.airline.lower() in liked],
            [o for o in bid_opts if o.airline.lower() in liked],
            request.preference,
        )
        choice = fav or choice
    if banned:
        warnings.append("Vom Kunden ausgeschlossen: " + ", ".join(sorted(banned)))
    if choice is None:
        raise ValueError(f"kein Preis für {origin} → {request.destination} gefunden")
    truck = rfq.best_truck(offers) if request.door_to_port else None
    if request.door_to_port and truck is None:
        warnings.append(
            "Kein Trucker-Gebot: Preis enthält keine Abholung, bitte ergänzen"
        )
    total = choice.total_eur_minor + (to_minor(truck.total_eur) if truck else 0)
    if not air:
        warnings.append("Kein Tarifblatt-/Portalpreis, nur Carrier-Gebote")
    body = rfq.quote_text(request, choice, total, truck, session_id)
    if request.pickup_assumed and truck is not None:
        body = rfq.with_pickup_note(body, request)
    options = sorted(air + bid_opts, key=lambda o: o.total_eur_minor)
    return {
        "body": body,
        "total_minor": total,
        "currency": "EUR",
        "preference": request.preference,
        "choice": {
            k: v
            for k, v in asdict(choice).items()
            if k
            in (
                "airline",
                "origin",
                "destination",
                "transit_days",
                "source",
                "rate_id",
                "valid_until",
                "total_eur_minor",
            )
        },
        "truck": {"name": truck.carrier.name, "eur_minor": to_minor(truck.total_eur)}
        if truck
        else None,
        "options": [
            {
                "airline": o.airline,
                "source": o.source,
                "transit_days": o.transit_days,
                "eur_minor": o.total_eur_minor,
            }
            for o in options
        ],
        "warnings": warnings,
    }
