"""Quote as a PDF attachment (5U requirement, 21.11 night): deterministic, no LLM.

Every value comes from the priced, already chosen option; nothing is extracted or worded by a
model. The PDF is written by hand (one page, Helvetica, WinAnsi encoding) so no new
dependency and no font files are needed. Labels follow the request language (de/en/fr/es/it,
English otherwise).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from app.mail.style import amount, written_date

if TYPE_CHECKING:
    from app.agents.bids import Offer
    from app.models import CargoInsurance, RateOption, Rfq

CONTENT_TYPE = "application/pdf"

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Air freight quotation",
        "ref": "Reference",
        "date": "Date",
        "customer": "Customer",
        "route": "Route",
        "airline": "Airline",
        "transit": "Air transit",
        "days": "days",
        "pickup": "Pickup",
        "cargo": "Cargo",
        "pieces": "pieces",
        "chargeable": "Chargeable weight",
        "total": "All-in total",
        "valid": "Valid until",
        "note": "One all-in price including all charges listed above. Reply to our email to book.",
        "file": "Quotation",
    },
    "de": {
        "title": "Angebot Luftfracht",
        "ref": "Referenz",
        "date": "Datum",
        "customer": "Kunde",
        "route": "Strecke",
        "airline": "Fluggesellschaft",
        "transit": "Flugzeit",
        "days": "Tage",
        "pickup": "Abholung",
        "cargo": "Sendung",
        "pieces": "Packstücke",
        "chargeable": "Frachtpflichtiges Gewicht",
        "total": "Gesamtpreis all-in",
        "valid": "Gültig bis",
        "note": "Ein Gesamtpreis inklusive aller oben genannten Leistungen. Zum Buchen einfach auf unsere Mail antworten.",
        "file": "Angebot",
    },
    "fr": {
        "title": "Offre de fret aérien",
        "ref": "Référence",
        "date": "Date",
        "customer": "Client",
        "route": "Trajet",
        "airline": "Compagnie aérienne",
        "transit": "Transit aérien",
        "days": "jours",
        "pickup": "Collecte",
        "cargo": "Marchandise",
        "pieces": "colis",
        "chargeable": "Poids taxable",
        "total": "Total tout compris",
        "valid": "Valable jusqu'au",
        "note": "Un prix tout compris pour les prestations ci-dessus. Répondez à notre e-mail pour réserver.",
        "file": "Offre",
    },
    "es": {
        "title": "Oferta de carga aérea",
        "ref": "Referencia",
        "date": "Fecha",
        "customer": "Cliente",
        "route": "Ruta",
        "airline": "Aerolínea",
        "transit": "Tránsito aéreo",
        "days": "días",
        "pickup": "Recogida",
        "cargo": "Carga",
        "pieces": "bultos",
        "chargeable": "Peso tasable",
        "total": "Total todo incluido",
        "valid": "Válida hasta",
        "note": "Un precio todo incluido para los servicios indicados. Responda a nuestro correo para reservar.",
        "file": "Oferta",
    },
    "it": {
        "title": "Offerta di trasporto aereo",
        "ref": "Riferimento",
        "date": "Data",
        "customer": "Cliente",
        "route": "Tratta",
        "airline": "Compagnia aerea",
        "transit": "Transito aereo",
        "days": "giorni",
        "pickup": "Ritiro",
        "cargo": "Merce",
        "pieces": "colli",
        "chargeable": "Peso tassabile",
        "total": "Totale tutto incluso",
        "valid": "Valida fino al",
        "note": "Un prezzo tutto incluso per i servizi indicati. Risponda alla nostra e-mail per prenotare.",
        "file": "Offerta",
    },
}


@dataclass(frozen=True)
class QuoteDoc:
    """Exactly the facts printed on the PDF; built only from code-priced values."""

    reference: str
    lang: str
    issued: date
    customer: str | None
    origin: str
    destination: str
    airline: str
    transit_days: int | None
    pickup: str | None
    pieces: int | None
    weight_kg: float
    volume_cbm: float
    chargeable_kg: float
    total_minor: int
    currency: str
    valid_until: date | None
    insurance: CargoInsurance | None = None


def _lang(code: str | None) -> str:
    base = (code or "en").lower()[:2]
    return base if base in _LABELS else "en"


def _iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _kg(value: float) -> str:
    return f"{value:g} kg"


def quote_doc(
    session_id: int,
    rfq: Rfq,
    choice: RateOption,
    total_minor: int,
    truck: Offer | None,
    currency: str = "EUR",
) -> QuoteDoc:
    """The printable facts of the quote that is about to be sent. `total_minor` is the same
    all-in total the mail states (air plus trucking, any configured adjustment applied)."""
    pickup = None
    if truck is not None:
        pickup = rfq.pickup_city or rfq.pickup_zip or None
    return QuoteDoc(
        reference=f"AP-{session_id}",
        lang=_lang(rfq.language),
        issued=datetime.now(UTC).date(),
        customer=rfq.customer_name or None,
        origin=choice.origin,
        destination=choice.destination,
        airline=choice.airline,
        transit_days=choice.transit_days if choice.transit_days > 0 else None,
        pickup=pickup,
        pieces=rfq.pieces,
        weight_kg=rfq.weight_kg,
        volume_cbm=rfq.volume_cbm,
        chargeable_kg=choice.billed_kg or choice.chargeable_kg,
        total_minor=total_minor,
        currency=currency,
        valid_until=_iso_date(choice.valid_until),
        insurance=rfq.insurance,
    )


def _number(value: object) -> float:
    try:
        return max(0.0, float(str(value)))
    except ValueError:
        return 0.0


def manual_doc(
    session_id: int,
    request: dict[str, object] | None,
    choice: dict[str, object],
    total_minor: int,
    currency: str,
) -> QuoteDoc:
    """The same document for a human's Notfall-Quote from the UI (stored session request and
    the draft's chosen option). Unknown or malformed fields are left off the PDF."""
    req = request or {}
    pieces = req.get("pieces")
    transit = choice.get("transit_days")
    return QuoteDoc(
        reference=f"AP-{session_id}",
        lang=_lang(str(req.get("language") or "en")),
        issued=datetime.now(UTC).date(),
        customer=str(req["customer_name"]) if req.get("customer_name") else None,
        origin=str(choice.get("origin") or req.get("origin") or ""),
        destination=str(choice.get("destination") or req.get("destination") or ""),
        airline=str(choice.get("airline") or ""),
        transit_days=int(transit) if isinstance(transit, int | float) and transit > 0 else None,
        pickup=str(req.get("pickup_city") or req.get("pickup_zip") or "") or None,
        pieces=int(pieces) if isinstance(pieces, int) and pieces > 0 else None,
        weight_kg=_number(req.get("weight_kg")),
        volume_cbm=_number(req.get("volume_cbm")),
        chargeable_kg=_number(choice.get("billed_kg") or choice.get("chargeable_kg")),
        total_minor=total_minor,
        currency=currency or "EUR",
        valid_until=_iso_date(str(choice["valid_until"]) if choice.get("valid_until") else None),
    )


def _rows(doc: QuoteDoc) -> list[tuple[str, str]]:
    t = _LABELS[doc.lang]
    rows: list[tuple[str, str]] = [
        (t["ref"], doc.reference),
        (t["date"], written_date(doc.issued, doc.lang)),
    ]
    if doc.customer:
        rows.append((t["customer"], doc.customer))
    if doc.origin and doc.destination:
        rows.append((t["route"], f"{doc.origin} - {doc.destination}"))
    if doc.airline:
        rows.append((t["airline"], doc.airline))
    if doc.transit_days is not None:
        rows.append((t["transit"], f"{doc.transit_days} {t['days']}"))
    if doc.pickup:
        rows.append((t["pickup"], doc.pickup))
    cargo = [_kg(doc.weight_kg)] if doc.weight_kg else []
    if doc.volume_cbm:
        cargo.append(f"{doc.volume_cbm:g} cbm")
    if doc.pieces:
        cargo.insert(0, f"{doc.pieces} {t['pieces']}")
    if cargo:
        rows.append((t["cargo"], ", ".join(cargo)))
    if doc.chargeable_kg:
        rows.append((t["chargeable"], _kg(doc.chargeable_kg)))
    if doc.valid_until is not None:
        rows.append((t["valid"], written_date(doc.valid_until, doc.lang)))
    if doc.insurance is not None:
        from app.pricing.insurance import notice

        rows.append(("", notice(doc.insurance, doc.lang)))
    return rows


# ---------------------------------------------------------------- minimal PDF writer


def _pdf_text(value: str) -> bytes:
    """A PDF string literal in WinAnsi (cp1252, contains €); unknown characters become '?'."""
    raw = value.encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return [*lines, current] if current else lines


def _content(doc: QuoteDoc) -> bytes:
    t = _LABELS[doc.lang]
    ops: list[bytes] = []

    def text(x: int, y: int, size: int, value: str, bold: bool = False) -> None:
        font = b"/F2" if bold else b"/F1"
        ops.append(
            b"BT " + font + b" %d Tf %d %d Td " % (size, x, y) + _pdf_text(value) + b" Tj ET"
        )

    text(56, 780, 20, t["title"], bold=True)
    text(56, 758, 10, "Austrian Power")
    ops.append(b"0.6 G 56 745 m 539 745 l S 0 G")
    y = 715
    for label, value in _rows(doc):
        text(56, y, 11, label)
        text(230, y, 11, value)
        y -= 20
    y -= 10
    ops.append(b"0.95 g 50 %d 495 34 re f 0 g" % (y - 12))
    text(56, y, 14, t["total"], bold=True)
    text(330, y, 14, amount(doc.total_minor, doc.currency), bold=True)
    y -= 45
    for line in _wrap(t["note"], 90):
        text(56, y, 9, line)
        y -= 13
    return b"\n".join(ops)


def render(doc: QuoteDoc) -> bytes:
    """One A4 page as PDF bytes. Deterministic for the same `doc`."""
    stream = _content(doc)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def filename(doc: QuoteDoc) -> str:
    return f"{_LABELS[doc.lang]['file']}_{doc.reference}.pdf"


def attachment(doc: QuoteDoc) -> dict[str, str]:
    """The mailbox API's attachment shape (`attachments[]` on POST /v1/mailbox/messages)."""
    return {
        "filename": filename(doc),
        "content_type": CONTENT_TYPE,
        "content_base64": base64.b64encode(render(doc)).decode("ascii"),
    }
