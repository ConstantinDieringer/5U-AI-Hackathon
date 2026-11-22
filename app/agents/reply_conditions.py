"""Conditions are proved from the frozen priced shipment, not from model assertions."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

from app.mail.reply_intent import Condition


def _valid(raw: str | None) -> bool:
    if not raw:
        return True
    try:
        stamp = datetime.fromisoformat(raw)
        return stamp.tzinfo is not None and stamp > datetime.now(UTC)
    except ValueError:
        return False


def check(condition: Condition, quote: dict) -> tuple[bool | None, str]:
    request = quote.get("request_snapshot") or {}
    audit = quote.get("pricing_snapshot") or {}
    if not request or not audit or not _valid(quote.get("valid_until")):
        return None, "Die Angebotsgrundlage ist nicht vollständig oder nicht mehr gültig"
    if condition.kind == "pickup":
        if condition.value and condition.value not in condition.evidence:
            return None, "Der Abholort ist nicht durch die Bedingung belegt"
        from app.agents.prebook import _dates

        if _dates(condition.evidence) or re.search(r"\b\d{1,2}:\d{2}\b", condition.evidence):
            return None, "Ein genauer Abholtermin ist noch nicht bestätigt"
        truck = audit.get("trucking")
        included = bool(truck and request.get("pickup_zip"))
        return included and (
            not condition.value or condition.value == request.get("pickup_zip")
        ), "Abholung im aktuellen Gesamtpreis"
    if condition.kind == "price_cap":
        try:
            cap = json.loads(condition.value)
            if type(cap["amount_minor"]) is not int or cap["amount_minor"] < 0:
                raise ValueError("invalid cap")
            if cap["currency"] != quote["currency"]:
                return None, "Die Preisgrenze verwendet eine andere Währung"
            from app.agents.bids import _AMOUNT, _number
            from app.pricing.pricing import to_minor

            amounts = []
            for match in _AMOUNT.finditer(condition.evidence):
                number = _number(match["num1"] or match["num2"])
                currency = (match["cur1"] or match["cur2"]).upper()
                currency = {"€": "EUR", "$": "USD"}.get(currency, currency)
                if number is not None:
                    amounts.append((to_minor(number), currency))
            if amounts != [(cap["amount_minor"], cap["currency"])]:
                return None, "Die Preisgrenze ist nicht durch den Kundentext belegt"
            return quote["total_minor"] <= cap[
                "amount_minor"
            ], "Gesamtpreis innerhalb der genannten Preisgrenze"
        except (KeyError, TypeError, ValueError):
            return None, "Die Preisgrenze ist nicht eindeutig"
    if condition.kind in ("cheapest", "fastest"):
        pattern = (
            r"cheapest|cheaper|lowest|economical|günstig|billig|preiswert"
            if condition.kind == "cheapest"
            else r"fastest|quickest|schnell"
        )
        if not re.search(pattern, condition.evidence, re.IGNORECASE):
            return None, "Das Vergleichskriterium ist nicht durch den Kundentext belegt"
        selected = audit.get("selected") or {}
        options = [
            o
            for o in audit.get("options", [])
            if o.get("origin") == quote.get("origin")
            and o.get("destination") == quote.get("destination")
            and _valid(o.get("valid_until"))
            and _valid(o.get("fx_valid_until"))
        ]
        if not options or audit.get("request") != request or selected not in options:
            return None, "Es fehlen gültige Vergleichsangebote für diesen Sendungsstand"
        if request.get("pickup_zip") and not audit.get("trucking"):
            return None, "Ein belegter Vorlaufpreis fehlt"
        if selected.get("airline") != quote.get("airline"):
            return None, "Der Vergleich gehört zu einem anderen Angebot"
        key = "total_eur_minor" if condition.kind == "cheapest" else "transit_days"
        if any(o.get(key) is None for o in options):
            return None, "Die Vergleichsgrundlage ist unvollständig"
        # Every compared option has the same origin/pickup and therefore the same truck cost.
        return selected[key] == min(
            o[key] for o in options
        ), "Beste passende Option unter den geprüften Angeboten"
    if condition.kind in ("arrival_by", "departure_by"):
        # Earliest/planned dates are NOT confirmed service guarantees.
        field = "confirmed_arrival" if condition.kind == "arrival_by" else "confirmed_departure"
        try:
            confirmed = audit.get(field)
            if not confirmed:
                return None, "Der gewünschte Termin ist noch nicht verbindlich bestätigt"
            return date.fromisoformat(confirmed) <= date.fromisoformat(
                condition.value
            ), "Bestätigter Termin erfüllt die Bedingung"
        except ValueError:
            return None, "Die Terminbedingung ist nicht eindeutig"
    return None, "Diese Buchungsbedingung benötigt noch eine Bestätigung"
