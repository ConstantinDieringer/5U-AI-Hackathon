"""One mail, several shipments to different destinations (rounds 89 and 90, `one_of_two`).

    Could you please quote the fastest air freight option for each of these two shipments?
    Both are ready for pickup on 2026-11-28 at our site in 20674 Hamburg, Germany. ...

    1. Destination airport RUH (Riyadh, SA): sports equipment, 6 pieces, gross weight 591 kg,
       volume 1.95 cbm.
    2. Destination airport GRU (Sao Paulo, BR): machine parts, 6 pieces, gross weight 1335 kg,
       volume 10.63 cbm.

    Please send both quotes in the same reply, with a separate all-in price in EUR for each

`split` returns one Shipment per numbered item, by rules only. It applies only when every
item names exactly one destination airport and a gross weight and the destinations differ;
otherwise it returns [] and the mail is handled as one request, exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ITEM = re.compile(
    r"^[ \t]*(?:\(?(\d)[.)]|shipment\s+(\d)\s*[:.-])[ \t]+", re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True)
class Shipment:
    destination: str  # IATA
    weight_kg: float
    volume_cbm: float
    pieces: int | None
    body: str  # the mail with only this shipment's item, for extraction and validation


def _items(body: str) -> list[tuple[int, int]]:
    """(start, end) of each numbered item 1, 2, ... in order; an item ends at the next item or
    at the first blank line."""
    marks = list(_ITEM.finditer(body))
    numbers = [int(m.group(1) or m.group(2)) for m in marks]
    if len(marks) < 2 or numbers != list(range(1, len(marks) + 1)):
        return []
    spans = []
    for i, mark in enumerate(marks):
        limit = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        blank = re.search(r"\n[ \t]*\n", body[mark.start() : limit])
        spans.append((mark.start(), mark.start() + blank.start() if blank else limit))
    return spans


def split(body: str) -> list[Shipment]:
    from app.mail import rules
    from app.mail.rfq_validation import numeric_facts

    spans = _items(body or "")
    if not spans:
        return []
    origins, destinations = rules.airports()
    out: list[Shipment] = []
    for start, end in spans:
        item = body[start:end]
        dest = rules.read("", item, origins, destinations).destination
        facts = numeric_facts(item)
        weight = facts.get("weight_kg")
        if dest is None or weight is None or weight <= 0:
            return []
        # this item only: the other items are cut out, the rest of the mail stays as written
        kept = body[: spans[0][0]]
        kept += _ITEM.sub("", item, count=1).rstrip() + "\n"
        kept += body[spans[-1][1] :]
        pieces = facts.get("pieces")
        out.append(
            Shipment(
                destination=dest,
                weight_kg=float(weight),
                volume_cbm=float(facts.get("volume_cbm") or 0),
                pieces=int(pieces) if pieces else None,
                body=re.sub(r"\n{3,}", "\n\n", kept),
            )
        )
    if len({s.destination for s in out}) != len(out):
        return []
    return out
