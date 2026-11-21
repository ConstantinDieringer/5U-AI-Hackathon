"""Carrier document evidence reaches a real Offer without live API/model calls."""

import io
from dataclasses import replace
from decimal import Decimal

import docx
import openpyxl
import pytest

from app import db, hackloop
from app.agents import bids
from app.mail import poller
from app.models import Mail

TABLE = (
    "OFFER TRK-1234\nLead time 29 hours from order to collection\n"
    "Description Quantity Unit price (EUR) Amount (EUR)\n"
    "Base charge 1 67.00 67.00\nWeight charge 1410 kg 0.07 98.70\nTotal 165.70\n"
    "Not part of this offer: waiting time 450.00 EUR for each hour."
)


def _pdf(text):
    stream = b"BT /F1 10 Tf 40 800 Td 14 TL "
    for line in text.splitlines():
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream += f"({escaped}) Tj T* ".encode()
    stream += b"ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{o:010} 00000 n \n".encode() for o in offsets[1:])
    return data + f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()


@pytest.fixture(autouse=True)
def source_setup(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(poller, "settings", replace(poller.settings, inbox_dir=tmp_path / "inbox"))
    bids.close(123)
    yield
    bids.close(123)


def _mail(filename="offer.txt", body="Our price offer is attached."):
    return Mail(
        456,
        123,
        "elbkante-logistik@hackloop.mail",
        "austrian-power@hackloop.mail",
        "Re: [AP-123] Pickup to CGN",
        body,
        "2026-11-21T21:17:00+00:00",
        attachments=[{"id": 1, "filename": filename}],
    )


@pytest.mark.parametrize("extension", ["txt", "pdf", "docx", "xlsx"])
def test_document_total_and_collection_lead_reach_offer(extension, monkeypatch):
    if extension == "pdf":
        raw = _pdf(TABLE)
    elif extension == "docx":
        document = docx.Document()
        for line in TABLE.splitlines():
            document.add_paragraph(line)
        out = io.BytesIO()
        document.save(out)
        raw = out.getvalue()
    elif extension == "xlsx":
        book = openpyxl.Workbook()
        for line in TABLE.splitlines():
            book.active.append([line])
        out = io.BytesIO()
        book.save(out)
        book.close()
        raw = out.getvalue()
    else:
        raw = TABLE.encode()
    monkeypatch.setattr(hackloop, "get_attachment", lambda *_: raw)
    bids.on_bid_mail(_mail(f"offer.{extension}"), 123)
    (offer,) = bids.offers(123)
    assert offer.total_eur == Decimal("165.70")
    assert offer.transit_days == 29 / 24 and offer.timing_kind == "collection_lead"
    assert offer.mail_id == 456


@pytest.mark.parametrize(
    "text,expected",
    [
        (TABLE, (Decimal("165.70"), "EUR")),
        (
            "Our price for this transport is 271.10 EUR. Base charge 116 EUR and weight charge 155.10 EUR.",
            (Decimal("271.10"), "EUR"),
        ),
        ("Gesamtpreis: 1.234,56 EUR", (Decimal("1234.56"), "EUR")),
        ("Amount (USD)\nTotal | 250.50", (Decimal("250.50"), "USD")),
        ("Offer 1234\nBase charge 67 EUR\nWeight charge 98.70 EUR", None),
        ("Total 165.70", None),
        ("EUR USD\nTotal 165.70", None),
        ("Total EUR 165.70\nTotal EUR 166.70", None),
    ],
)
def test_only_explicit_unambiguous_document_total(text, expected):
    assert bids.document_price(text) == expected


@pytest.mark.parametrize(
    "body,filename,raw",
    [
        ("Our all-in price is EUR 200", "offer.txt", TABLE.encode()),
        ("Offer attached", "offer.mp3", b"audio without transcript"),
        ("Offer attached", "offer.pdf", b"broken pdf"),
        ("Offer attached", "offer.txt", b"Base charge 67 EUR"),
    ],
)
def test_unreadable_or_conflicting_evidence_never_becomes_offer(body, filename, raw, monkeypatch):
    monkeypatch.setattr(hackloop, "get_attachment", lambda *_: raw)
    bids.on_bid_mail(_mail(filename, body), 123)
    assert bids.offers(123) == []


def test_conflicting_documents_never_choose_the_larger_amount(monkeypatch):
    monkeypatch.setattr(
        hackloop,
        "get_attachment",
        lambda mid, aid: (TABLE if aid == 1 else TABLE.replace("165.70", "200.70")).encode(),
    )
    mail = _mail()
    mail.attachments.append({"id": 2, "filename": "revision.txt"})
    bids.on_bid_mail(mail, 123)
    assert bids.offers(123) == []


@pytest.mark.parametrize("label", ["Total, all-in", "Total all in", "Grand total", "Gesamtbetrag"])
@pytest.mark.parametrize("separator", [" ", "\n", " | "])
def test_explicit_total_label_and_adjacent_table_value(label, separator):
    text = f"Base 90 EUR\nWeight 32.89 EUR\n{label}{separator}122.89 EUR\nWaiting 450 EUR"
    assert bids.document_price(text) == (Decimal("122.89"), "EUR")


def test_missing_total_does_not_scan_past_an_unrelated_row():
    assert bids.document_price("Total\nValid until Friday\nWaiting 450 EUR") is None
