"""Conservative local extraction: unknown clauses always use the full extractor."""

import re
from datetime import date

from pydantic import ValidationError

from app.mail.content import current_message
from app.models import Mail, Rfq
from app.pricing.reference import pickup_zones

_NUMBER = r"\d+(?:[.,]\d{1,2})?"
_FIELDS = {
    "origin": r"(?:origin|departure airport):\s*([A-Z]{3})",
    "destination": r"(?:destination|destination airport):\s*([A-Z]{3})",
    "weight_kg": rf"(?:total )?gross weight:\s*({_NUMBER})\s*kg",
    "volume_cbm": rf"(?:total )?volume:\s*({_NUMBER})\s*(?:cbm|m3|m³)",
    "pieces": r"pieces:\s*(\d+)",
    "ready_date": r"ready(?: date)?:\s*(\d{4}-\d{2}-\d{2})",
    "preference": r"preference:\s*(cheapest|fastest)",
}
_NATURAL = re.compile(
    r"(?:Hi|Hello),?\s*Could you quote an all-in price in EUR for the "
    r"(?P<preference>cheapest|fastest) air freight option to (?P<destination>[A-Z]{3})"
    r"(?: \([A-Za-z ,]+\))?\?\s*"
    r"We['’]ll deliver the shipment to (?P<origin>[A-Z]{3}) ourselves\.\s*"
    r"It['’]ll be ready on (?P<ready_date>\d{4}-\d{2}-\d{2}): "
    r"(?P<pieces>\d+) pieces of printed marketing material, "
    rf"(?P<weight_kg>{_NUMBER}) kg gross, (?P<volume_cbm>{_NUMBER}) cbm\."
    r"(?:\s*Thanks\s*(?P<customer_name>[A-Za-zÀ-ž]+)\s*[A-Za-zÀ-ž &.]+)?",
)


def extract_standard(mail: Mail) -> Rfq | None:
    """Accept complete, current, attachment-free supported grammars only."""
    current = current_message(mail.body, mail.subject)
    if mail.attachments or current.history or current.text != current.facts:
        return None
    body = current.facts.strip()
    match = _NATURAL.fullmatch(body)
    data: dict[str, str] = (
        {key: value for key, value in match.groupdict().items() if value is not None}
        if match
        else {}
    )
    if not match:
        for line in body.splitlines():
            line = line.strip()
            if not line or line in {"Hi,", "Hello,", "Please quote air freight.", "Thanks."}:
                continue
            for field, pattern in _FIELDS.items():
                found = re.fullmatch(pattern, line, re.IGNORECASE)
                if found:
                    if field in data:
                        return None
                    data[field] = found[1]
                    break
            else:
                return None
    if not set(_FIELDS) <= data.keys():
        return None
    try:
        date.fromisoformat(data["ready_date"])
        candidate = Rfq.model_validate(
            {
                **data,
                "weight_kg": float(data["weight_kg"].replace(",", ".")),
                "volume_cbm": float(data["volume_cbm"].replace(",", ".")),
            }
        )
    except (ValueError, ValidationError):
        return None
    if candidate.origin not in set(pickup_zones().values()):
        return None
    if candidate.origin == candidate.destination or candidate.volume_cbm <= 0:
        return None
    return candidate
