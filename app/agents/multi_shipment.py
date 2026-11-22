"""Several shipments in one customer mail (board reason one_of_two).

The customer asks for separate prices for two or more shipments "in the same reply". The
extraction puts the first shipment in the usual top-level fields and every further one in
`Rfq.additional_shipments`. This module turns those items into checked sibling requests and
writes the one combined quote mail. Pricing stays in app.agents.rfq (one pass per shipment,
each with its own bid slot); no LLM is used here.
"""

from __future__ import annotations

import logging
import re

from pydantic import ValidationError

from app import db
from app.mail.style import SIGNATURE
from app.models import RateOption, Rfq

log = logging.getLogger(__name__)

_ORDINALS = {
    "en": ["first", "second", "third", "fourth", "fifth"],
    "de": ["erste", "zweite", "dritte", "vierte", "fünfte"],
}
_SEPARATOR = re.compile(r"(?<=\d)[.,'   ](?=\d{3}(?!\d))")


def _numbers(text: str) -> set[str]:
    """Every number in the text with thousands separators removed (1,812 / 1.812 -> 1812)."""
    plain = _SEPARATOR.sub("", text or "")
    return set(re.findall(r"\d+(?:[.,]\d+)?", plain))


def _stated(value: float, numbers: set[str]) -> bool:
    whole = f"{value:.0f}" if float(value).is_integer() else None
    candidates = {f"{value:g}", f"{value:g}".replace(".", ",")}
    if whole:
        candidates.add(whole)
    return bool(candidates & numbers)


def siblings(rfq: Rfq, body: str, sid: int) -> list[Rfq]:
    """Checked requests for every further shipment. An item whose weight is not written in the
    mail, or that does not validate, is dropped with a warning (never guessed)."""
    numbers = _numbers(body)
    result: list[Rfq] = []
    for index, item in enumerate(rfq.additional_shipments, start=2):
        if not isinstance(item, dict):
            continue
        data = rfq.model_dump()
        data.update(
            additional_shipments=[],
            destination=item.get("destination"),
            weight_kg=item.get("weight_kg"),
            volume_cbm=item.get("volume_cbm") or 0.0,
            pieces=item.get("pieces"),
            customer_questions=[],
            service_constraints=[],
        )
        if item.get("origin"):
            data["origin"] = item["origin"]
        if item.get("pickup_zip"):
            data["pickup_zip"] = item["pickup_zip"]
        try:
            sibling = Rfq.model_validate(data)
        except ValidationError as exc:
            db.emit(
                "multi_shipment_dropped",
                f"Sendung {index} nicht verwendbar: {str(exc)[:160]}",
                sid,
                level="warn",
            )
            continue
        if not _stated(sibling.weight_kg, numbers):
            db.emit(
                "multi_shipment_dropped",
                f"Sendung {index}: Gewicht {sibling.weight_kg:g} kg steht nicht in der Mail",
                sid,
                level="warn",
            )
            continue
        result.append(sibling)
    if result:
        db.emit(
            "multi_shipment",
            f"{1 + len(result)} Sendungen in einer Mail, jede wird einzeln bepreist",
            sid,
            destinations=[rfq.destination, *(s.destination for s in result)],
        )
    return result


def _lang(rfq: Rfq) -> str:
    return "de" if (rfq.language or "").lower().startswith("de") else "en"


def subject(origin: str, destinations: list[str], language_code: str, fallback: str) -> str:
    """One subject naming every destination, e.g. 'Air freight FRA to GRU and TLV'."""
    codes = [d for d in destinations if re.fullmatch(r"[A-Z]{3}", d or "")]
    if len(codes) != len(destinations) or not re.fullmatch(r"[A-Z]{3}", origin or ""):
        return fallback
    if language_code.lower().startswith("de"):
        return f"Luftfracht ab {origin} nach {' und '.join(codes)}"
    return f"Air freight from {origin} to {' and '.join(codes)}"


def _paragraph(
    lang: str,
    position: int,
    rfq: Rfq,
    option: RateOption,
    price: str,
    with_truck: bool,
) -> str:
    ordinal = _ORDINALS[lang][min(position, len(_ORDINALS[lang]) - 1)]
    days = option.transit_days
    if lang == "de":
        text = (
            f"Die {ordinal} Sendung geht nach {option.destination}. Wir bieten Luftfracht von "
            f"{option.origin} nach {option.destination} mit {option.airline}"
        )
        if with_truck:
            text += (
                f", inklusive Abholung in {rfq.pickup_label} und Lkw Transport zum Flughafen "
                f"{option.origin}"
            )
        return (
            f"{text}, zum All in Gesamtpreis von {price}. Die Laufzeit beträgt {days} "
            f"{'Tag' if days == 1 else 'Tage'} ab Abflug."
        )
    text = (
        f"The {ordinal} shipment goes to {option.destination}. We can offer air freight from "
        f"{option.origin} to {option.destination} with {option.airline}"
    )
    if with_truck:
        text += f", including pickup at {rfq.pickup_label} and trucking to {option.origin} airport"
    return (
        f"{text}, for an all in total of {price}. Transit time is {days} "
        f"{'day' if days == 1 else 'days'} from departure."
    )


def quote_body(
    first: Rfq,
    shipments: list[tuple[Rfq, RateOption | None, str | None, bool]],
) -> str:
    """One mail with a separate all in price per shipment. `shipments` holds, in mail order,
    (request, chosen option or None, formatted EUR price or None, trucking included)."""
    lang = _lang(first)
    name = first.customer_name
    count = len(shipments)
    parts: list[str] = []
    if lang == "de":
        parts.append(f"Hallo {name}," if name else "Guten Tag,")
        parts.append(
            "vielen Dank für Ihre Anfrage. Hier sind die Angebote für alle "
            f"{count} Sendungen in einer Antwort, mit einem eigenen All in Preis je Sendung."
        )
    else:
        parts.append(f"Hi {name}," if name else "Hello,")
        parts.append(
            f"thanks for your request. Here are the offers for all {count} shipments in one "
            "reply, with a separate all in price for each shipment."
        )
    for position, (rfq, option, price, with_truck) in enumerate(shipments):
        if option is None or price is None:
            ordinal = _ORDINALS[lang][min(position, 4)]
            parts.append(
                f"Für die {ordinal} Sendung nach {rfq.destination} haben wir gerade keinen "
                "bestätigten Preis."
                if lang == "de"
                else f"For the {ordinal} shipment to {rfq.destination} we do not have a "
                "confirmed price right now."
            )
            continue
        parts.append(_paragraph(lang, position, rfq, option, price, with_truck))
    if lang == "de":
        parts.append(
            "Jeder Preis gilt einzeln für seine Sendung und ist kein Gesamtpreis. Wenn das für "
            "Sie passt, antworten Sie einfach auf diese Mail und wir buchen sofort."
        )
    else:
        parts.append(
            "Each price is a separate all in total for its shipment, not a combined price. "
            "If that works for you, just reply to this email and we will book right away."
        )
    parts.append(SIGNATURE)
    return "\n\n".join(parts) + "\n"
