"""Attachments in every common format: read in code where possible, model only for audio and
scans, and carrier offers that arrive as a file instead of mail text."""

from __future__ import annotations

import email.message
import io
import wave
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app import db, llm
from app.agents import bids
from app.mail import audio, documents
from app.mail.router import route
from app.models import Category, Mail

OFFER = "All-in price: EUR 1.234,50, transit time 3 days"


@pytest.fixture(autouse=True)
def allowed_audio_model(monkeypatch):
    monkeypatch.setattr(
        audio,
        "settings",
        replace(
            audio.settings,
            llm_audio_model="gpt-4o-mini-audio-preview",
        ),
    )
    monkeypatch.setattr(llm, "available_models", lambda **kw: {"gpt-4o-mini-audio-preview"})


def text_pdf(text: str) -> bytes:
    """A minimal one-page PDF with real (extractable) text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    )
    return out.getvalue()


def docx_bytes() -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph("Our offer for your request")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "All-in price", "EUR 1.234,50"
    t.cell(1, 0).text, t.cell(1, 1).text = "Transit time", "3 days"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def xlsx_bytes() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["All-in price", "EUR 1.234,50"])
    wb.active.append(["Transit time", "3 days"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def odt_bytes() -> bytes:
    content = (
        '<?xml version="1.0"?><office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body>'
        f"<office:text><text:p>{OFFER}</text:p></office:text></office:body>"
        "</office:document-content>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)
    return buf.getvalue()


def eml_bytes() -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "sales@carrier.example"
    msg["Subject"] = "Offer"
    msg.set_content("Please find our offer attached.")
    msg.add_attachment(OFFER.encode(), maintype="text", subtype="plain", filename="offer.txt")
    return bytes(msg)


def legacy_doc_bytes() -> bytes:
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200 + OFFER.encode("utf-16-le")


def wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


def png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buf, format="PNG")
    return buf.getvalue()


TEXT_FORMATS = {
    "offer.pdf": lambda: text_pdf(OFFER),
    "offer.docx": docx_bytes,
    "offer.xlsx": xlsx_bytes,
    "offer.odt": odt_bytes,
    "offer.rtf": lambda: (r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 " + OFFER + r"\par}").encode(),
    "offer.csv": lambda: b"item;value\nAll-in price;EUR 1.234,50\nTransit time;3 days\n",
    "offer.txt": lambda: OFFER.encode("cp1252"),
    "offer.eml": eml_bytes,
    "offer.doc": legacy_doc_bytes,
    "offer.msg": lambda: (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        + "__substg1.0_1000".encode("utf-16-le")
        + OFFER.encode("utf-16-le")
    ),
}


@pytest.mark.parametrize("name", sorted(TEXT_FORMATS))
def test_text_formats_are_read_without_model(name):
    doc = documents.read_bytes(name, TEXT_FORMATS[name]())
    assert not doc.needs_model
    assert bids.parse_price(doc.text) == (Decimal("1234.50"), "EUR"), doc.text
    assert bids.parse_transit_days(doc.text) == 3


def test_xls_is_read(tmp_path):
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Offer")
    ws.write(0, 0, "All-in price")
    ws.write(0, 1, "EUR 1.234,50")
    path = tmp_path / "offer.xls"
    wb.save(str(path))
    assert "EUR 1.234,50" in documents.read(path).text


@pytest.mark.parametrize(
    "name,data,kind",
    [
        ("angebot", lambda: text_pdf(OFFER), ".pdf"),  # no extension
        ("offer.txt", docx_bytes, ".docx"),  # wrong extension
        ("voice", lambda: b"ID3\x04\x00" + b"\x00" * 100, ".mp3"),
        ("voice.dat", wav_bytes, ".wav"),
        ("scan", png_bytes, ".png"),
    ],
)
def test_type_comes_from_bytes(name, data, kind):
    assert documents.sniff(name, data()) == kind


def test_audio_and_images_need_the_model():
    assert documents.read_bytes("v.mp3", b"ID3\x04\x00" + b"\x00" * 50).audio[0] == "mp3"
    assert documents.read_bytes("v.wav", wav_bytes()).audio[0] == "wav"
    assert documents.read_bytes("scan.png", png_bytes()).media


def test_unsupported_and_oversize_raise():
    with pytest.raises(ValueError):
        documents.read_bytes("virus.exe", b"MZ\x90\x00" + b"\x00" * 100)
    with pytest.raises(ValueError):
        documents.read_bytes("big.txt", b"x" * (documents.MAX_BYTES + 1))


def test_transcription_sends_input_audio_once_and_caches(monkeypatch, tmp_path):
    calls = []

    def complete(purpose, messages, session_id, **kw):
        calls.append((purpose, messages, kw))
        return OFFER

    monkeypatch.setattr(llm, "complete", complete)
    path = tmp_path / "voice.mp3"
    path.write_bytes(b"ID3\x04\x00" + b"\x00" * 50)
    doc = documents.read(path)
    assert documents.transcribe(doc, 5, "bid_attachment", path) == OFFER
    part = calls[0][1][0]["content"][1]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "mp3"
    assert calls[0][2]["model_name"] == "gpt-4o-mini-audio-preview"
    again = documents.read(path)  # cached: no model needed any more
    assert not again.needs_model and OFFER in again.text
    assert len(calls) == 1


# ---------------------------------------------------------------- carrier offers


@pytest.fixture
def bid_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(bids, "_spawn", lambda fn, *a: fn(*a))
    yield tmp_path
    bids.close(3)


def bid_mail(body="Please find our offer attached.", attachments=True):
    return Mail(
        77, 77, "yellowtail-air-freight@hackloop.mail", "team", "[AP-3] Re: Air freight",
        body, datetime.now(UTC).isoformat(),
        attachments=[{"id": 1, "filename": "x"}] if attachments else [],
    )  # fmt: skip


@pytest.mark.parametrize("name", ["offer.pdf", "offer.docx", "offer.xlsx", "offer.doc"])
def test_offer_in_attachment_becomes_bid_without_model(bid_db, monkeypatch, name):
    path = bid_db / name
    path.write_bytes(TEXT_FORMATS[name]())
    monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None: [path])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("no model call"))
    bids.on_bid_mail(bid_mail(), 3)
    offer = bids.offers(3)[-1]
    assert (offer.total_eur, offer.transit_days) == (Decimal("1234.50"), 3)


def test_voice_offer_is_transcribed(bid_db, monkeypatch):
    path = bid_db / "offer.mp3"
    path.write_bytes(b"ID3\x04\x00" + b"\x00" * 50)
    monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None: [path])
    seen = []

    def complete(purpose, messages, session_id, **kw):
        seen.append((purpose, session_id))
        return "Hi, all-in price would be EUR 980, transit 2 days."

    monkeypatch.setattr(llm, "complete", complete)
    bids.on_bid_mail(bid_mail(), 3)
    assert seen == [("bid_attachment s3", None)]  # outside the session's quote budget
    assert bids.offers(3)[-1].total_eur == Decimal("980.00")


def test_voice_offer_must_agree_with_mail_text(bid_db, monkeypatch):
    path = bid_db / "offer.mp3"
    path.write_bytes(b"ID3\x04\x00" + b"\x00" * 50)
    monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None: [path])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "Our price is EUR 999, 2 days.")
    bids.on_bid_mail(bid_mail(OFFER), 3)
    assert bids.offers(3) == []  # 1234.50 in text vs 999 spoken: never guess


def test_broken_attachment_leaves_bid_unpriced(bid_db, monkeypatch):
    path = bid_db / "offer.exe"
    path.write_bytes(b"MZ" + b"\x00" * 50)
    monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None: [path])
    bids.on_bid_mail(bid_mail(OFFER), 3)
    assert bids.offers(3) == []


def test_waiting_surcharge_is_not_the_total(bid_db, monkeypatch):
    """Round 63 (Elbkante): components, total, and an excluded surcharge in one PDF."""
    table = (
        "Base charge 1 67.00 67.00\nWeight charge 1410 kg 0.07 98.70\nTotal 165.70 EUR\n"
        "Not part of this offer: waiting time 450.00 EUR for each hour.\nLead time 29 hours"
    )
    path = bid_db / "offer.txt"
    path.write_bytes(table.encode())
    monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None: [path])
    bids.on_bid_mail(bid_mail(), 3)
    assert bids.offers(3)[-1].total_eur == Decimal("165.70")


def test_airline_offer_with_attachment_is_bid_not_tariff():
    m = bid_mail()
    m.sender = "kestrel-airways@hackloop.mail"
    assert route(m) == Category.CARRIER_BID  # our [AP-3] tag
    m.subject, m.references = "Re: Air freight", [70]
    assert route(m) == Category.CARRIER_BID  # threaded answer
    m.references = []
    assert route(m) == Category.TARIFF  # a new airline mail with a file is still a tariff
