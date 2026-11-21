"""Carrier offer documents: explicit totals and durations across layouts and formats.

Every layout runs through the real read path (app/mail/documents.py, then bid evidence) as
plain text, pipe table text, PDF, Word paragraphs and a Word table. The negative cases must
stay unpriced: a wrong total costs more accuracy than a missing one."""

import io
from decimal import Decimal

import docx
import pytest

from app.agents import bids
from app.mail import documents


def pdf(lines):
    stream = b"BT /F1 10 Tf 40 800 Td 14 TL "
    for line in lines:
        e = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream += f"({e}) Tj T* ".replace("\u202f", "\u00a0").encode("cp1252", "replace")
    stream += b"ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(data))
        data += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
        f"{o:010} 00000 n \n".encode() for o in offs
    )
    return data + f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()


def docx_paras(lines):
    d = docx.Document()
    for line in lines:
        d.add_paragraph(line)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def docx_table(rows):
    d = docx.Document()
    d.add_paragraph("Offer")
    width = max(len(r) for r in rows)
    t = d.add_table(rows=len(rows), cols=width)
    for i, r in enumerate(rows):
        for j in range(width):
            t.cell(i, j).text = r[j] if j < len(r) else ""
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


# name, rows (list of cell lists; a row of one cell = plain line), expected (price, cur, days)
CASES = [
    (
        "r63 elbkante",
        [
            ["OFFER TRK-1234"],
            ["Lead time 29 hours from order to collection"],
            ["Description", "Quantity", "Unit price (EUR)", "Amount (EUR)"],
            ["Base charge", "1", "67.00", "67.00"],
            ["Weight charge", "1410 kg", "0.07", "98.70"],
            ["Total", "", "", "165.70"],
            ["Not part of this offer: waiting time 450.00 EUR for each hour."],
        ],
        ("165.70", "EUR", 29 / 24),
    ),
    (
        "total amount label",
        [["Base", "90.00 EUR"], ["Total amount", "EUR 122.89"], ["Collection within 31 hours"]],
        ("122.89", "EUR", 31 / 24),
    ),
    (
        "total (EUR) header",
        [
            ["Item", "EUR"],
            ["Base", "90.00"],
            ["Fuel", "32.89"],
            ["Total (EUR)", "122.89"],
            ["Transit time: 2 days"],
        ],
        ("122.89", "EUR", 2),
    ),
    (
        "grand total currency after",
        [
            ["Freight", "1 200,00 €"],
            ["Fuel", "34,50 €"],
            ["Grand total", "1 234,50 €"],
            ["Transit 3 days"],
        ],
        ("1234.50", "EUR", 3),
    ),
    (
        "german gesamtpreis netto",
        [
            ["Grundpreis: 80,00 EUR"],
            ["Gesamtpreis (netto): 200,00 EUR"],
            ["Abholung innerhalb von 14 Stunden"],
        ],
        ("200.00", "EUR", 14 / 24),
    ),
    (
        "total due",
        [["Charges"], ["Pickup", "EUR 200.00"], ["TOTAL DUE", "EUR 200.00"], ["Lead time: 12 h"]],
        ("200.00", "EUR", 0.5),
    ),
    (
        "price per kg + total",
        [
            ["Rate per kg: EUR 0.07"],
            ["Price per kg: EUR 0.07"],
            ["Total price: EUR 165.70"],
            ["Transit time 1 day"],
        ],
        ("165.70", "EUR", 1),
    ),
    (
        "validity before transit",
        [["Offer valid for 7 days"], ["All-in price: EUR 310.00"], ["Transit time: 2 days"]],
        ("310.00", "EUR", 2),
    ),
    (
        "usd air",
        [["Air freight FRA-DFW"], ["Total all-in: USD 4,806.39"], ["Transit: 2 days"]],
        ("4806.39", "USD", 2),
    ),
    ("nbsp thousands", [["Total: EUR 1 234,50"], ["Transit 3 days"]], ("1234.50", "EUR", 3)),
    (
        "sum label",
        [["Base 100.00 EUR"], ["Sum 150.00 EUR"], ["Pickup lead time 20 hours"]],
        ("150.00", "EUR", 20 / 24),
    ),
    (
        "summe label",
        [["Grundpreis 100,00 EUR"], ["Summe 150,00 EUR"], ["Abholung in 20 Std."]],
        ("150.00", "EUR", 20 / 24),
    ),
    (
        "subtotal plus total",
        [
            ["Subtotal", "EUR 150.00"],
            ["VAT 0%", "EUR 0.00"],
            ["Total", "EUR 150.00"],
            ["Transit 1 day"],
        ],
        ("150.00", "EUR", 1),
    ),
    (
        "total incl fuel",
        [["Total incl. fuel and security: EUR 2,310.00"], ["Transit 4 days"]],
        ("2310.00", "EUR", 4),
    ),
    (
        "eur amount column",
        [
            ["Description", "Amount"],
            ["Base charge", "EUR 67.00"],
            ["Total", "EUR 165.70"],
            ["Collection lead time", "29 hours"],
        ],
        ("165.70", "EUR", 29 / 24),
    ),
    ("days as words", [["Total: EUR 500.00"], ["Transit time: two days"]], ("500.00", "EUR", 2)),
    (
        "ready within h",
        [["Total all-in 210,30 EUR"], ["We can collect within 18h of your order."]],
        ("210.30", "EUR", 0.75),
    ),
    (
        "optional extras listed after",
        [
            ["Total: EUR 400.00"],
            ["Optional: tail-lift EUR 60.00, Saturday pickup EUR 80.00"],
            ["Transit 10 hours"],
        ],
        ("400.00", "EUR", 10 / 24),
    ),
]


def variants(rows):
    lines = [" ".join(c for c in r if c) for r in rows]
    piped = [" | ".join(r) for r in rows]
    yield "txt", "offer.txt", "\n".join(lines).encode("utf-8")
    yield "txt|", "offer.txt", "\n".join(piped).encode("utf-8")
    yield "pdf", "offer.pdf", pdf(lines)
    yield "docx-p", "offer.docx", docx_paras(lines)
    yield "docx-t", "offer.docx", docx_table(rows)


@pytest.mark.parametrize("name,rows,expected", CASES, ids=[c[0] for c in CASES])
def test_offer_layouts_in_every_format(name, rows, expected):
    price, cur, days = expected
    for kind, fname, data in variants(rows):
        doc = documents.read_bytes(fname, data)
        evidence, got, issue = bids._bid_evidence(
            "Please find our offer attached.", [(fname, doc.text)], []
        )
        assert got == (Decimal(price), cur), (kind, issue, doc.text)
        assert bids.parse_transit_days(evidence) == pytest.approx(days), (kind, evidence)


@pytest.mark.parametrize(
    "text",
    [
        "Base charge 67.00 EUR\nWeight charge 98.70 EUR\nLead time 29 hours",
        "Total: EUR 165.70\nTotal: EUR 180.00",
        "Total weight: 1410 kg\nPrice on request",
        "Offer No: 12345\nAmounts in EUR\nBase 67.00\nTransit 2 days",
        "Offer: TRK-1234\nQuotation number: 4711 EUR",
        "Total excl. fuel: EUR 100.00\nTotal incl. fuel: EUR 120.00",
        "Total: 4 pcs\nRate EUR 0.07 per kg",
        "Total valid until: 30.11.2026",
        "Total discount: 5%\nPrice per kg EUR 0.10",
        "Waiting time total: 450 EUR",
        "Subtotal: EUR 150.00",
        "Price per kg: EUR 0.07\nTransit 1 day",
    ],
)
def test_never_guess_a_total(text):
    assert bids.document_price(text) is None


@pytest.mark.parametrize(
    "text,days",
    [
        ("Offer valid for 7 days\nTransit time: 2 days", 2),
        ("Payment terms 30 days\nCollection within 18 hours", 0.75),
        ("Free storage 3 days\nLead time: 29 hours", 29 / 24),
        ("Transit time: two days", 2),
        ("Abholung in 20 Std.", 20 / 24),
        ("Laufzeit drei Tage", 3),
    ],
)
def test_duration_comes_from_the_timing_line(text, days):
    assert bids.parse_transit_days(bids.document_timing(text)) == pytest.approx(days)
