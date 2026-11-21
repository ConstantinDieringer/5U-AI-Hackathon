"""RFQ inputs, durable clarification lifecycle and existing quote/booking boundaries."""

import dataclasses
from datetime import UTC, datetime

import pytest

from app import db
from app.agents import clarification, rfq
from app.mail.content import current_message
from app.mail.rfq_validation import validate
from app.models import Mail, Rfq


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(
        rfq,
        "settings",
        dataclasses.replace(rfq.settings, rfq_validation_mode="enforce"),
    )
    monkeypatch.setattr(clarification, "settings", rfq.settings)


def mail(body, mid=3627):
    now = datetime.now(UTC).isoformat()
    m = Mail(
        mid,
        mid,
        "auer-maschinenbau@hackloop.mail",
        "team",
        "RE: FW: AMS-HKG old shipment",
        body,
        now,
    )
    db.ingest_mail(
        mail_id=mid,
        sender=m.sender,
        recipient=m.to,
        subject=m.subject,
        body=body,
        category="rfq",
        received_at=now,
    )
    return m


def candidate(**updates):
    return Rfq(
        **{
            "origin": None,
            "destination": "SGN",
            "weight_kg": 572,
            "volume_cbm": 2.29,
            **updates,
        }
    )


BODY = "Please arrange the truck from our warehouse to the departure airport. Cheapest to SGN. The shipment is 11 pieces, 572 kg gross, total volume 2.29 cbm."


def test_3627_old_origin_never_reaches_carriers(monkeypatch):
    m = mail(
        BODY
        + "\nBest greetings from Regensburg\nTobias\n-----Original Message-----\nAMS to HKG, 165 kg, 11.8 cbm"
    )
    sent = []
    monkeypatch.setattr(rfq, "extract_rfq", lambda *a: candidate(origin="AMS"))
    monkeypatch.setattr(
        rfq.bids, "request", lambda *a: pytest.fail("invalid request reached a carrier")
    )
    monkeypatch.setattr(
        rfq,
        "portal_result",
        lambda *a, **kw: pytest.fail("invalid request reached portal"),
    )
    monkeypatch.setattr(
        clarification.hackloop,
        "send",
        lambda *a, **kw: sent.append((a, kw)) or {"id": 9001},
    )
    rfq.handle_rfq(m, m.id)
    row = db.validation_get(m.id)
    assert row["state"] == "awaiting_clarification" and row["send_state"] == "sent"
    assert len(sent) == 1 and sent[0][1]["in_reply_to"] == m.id
    assert not db.get_session(m.id)["quotes"]
    assert db.get_session(m.id)["first_reply_at"] is None


@pytest.mark.parametrize(
    "boundary",
    [
        "-----Original Message-----",
        "-----Weitergeleitete Nachricht-----",
        "On Monday somebody wrote:",
        "Am Montag schrieb jemand:",
    ],
)
def test_history_is_not_evidence(boundary):
    c = current_message(BODY + "\n" + boundary + "\nAMS-HKG, 165 kg", "RE: FW: AMS-HKG")
    assert "AMS" not in c.text and c.subject == "" and "AMS" in c.history


def test_2530_zip_wins_and_dg_attachment_blocks():
    m = mail(
        "Please arrange pickup from our site in 21663 to YYZ. 855 kg gross, total volume 3.74 cbm."
    )
    r, d = validate(
        m,
        candidate(
            origin="YYZ",
            pickup_zip="21663",
            destination="YYZ",
            weight_kg=855,
            volume_cbm=3.74,
        ),
        {
            "SLI.pdf": "Total gross weight (kg): 855\nTotal volume (cbm): 3.74\nDangerous goods: UN1950, Class 2.1"
        },
    )
    assert r.origin == "HAM" and any(i["field"] == "dangerous_goods" for i in d.issues)


def test_2256_xlsx_dg_wins_over_body_nondg():
    m = mail(
        "Please arrange the truck from our site at 61391 to GRU. 715 kg gross, total volume 2.65 cbm. General cargo, non-dangerous."
    )
    _, d = validate(
        m,
        candidate(
            pickup_zip="61391", destination="GRU", weight_kg=715, volume_cbm=2.65
        ),
        {
            "PL.xlsx": "Total gross weight (kg) | 715\nTotal volume (cbm) | 2.65\nDangerous goods: UN1133, Class 3, Adhesives"
        },
    )
    assert [i["field"] for i in d.issues] == ["dangerous_goods"]


def test_correct_lot_totals_and_unreadable_lot_block():
    body = "Please quote FRA to LOS.\nLot 1: 4 drums, together 730 kg gross (642 kg net), together 1.68 cbm\nLot 2: 4 cartons, each 48 kg gross (41 kg net), each 110 x 110 x 52 cm\nLot 3: 5 cases, each 206 kg gross (191 kg net), each 120 x 100 x 8 cm"
    m = mail(body)
    r, d = validate(m, candidate(origin="FRA", destination="LOS"))
    assert r.weight_kg == 1952 and r.volume_cbm == 4.6768 and not d.issues
    m.body += "\nLot 4: unreadable dimensions"
    _, d = validate(m, candidate(origin="FRA", destination="LOS"))
    assert any(i["field"] == "lots" for i in d.issues)


def test_mail_attachment_conflict_and_rounding():
    m = mail("FRA to JFK. 100 kg gross, total volume 2.65 cbm.")
    r, d = validate(
        m,
        candidate(origin="FRA", destination="JFK"),
        {"packing": "Total gross weight (kg): 100\nTotal volume (cbm): 2.646"},
    )
    assert not d.issues and r.weight_kg == 100
    _, d = validate(
        m, r, {"packing": "Total gross weight (kg): 200\nTotal volume (cbm): 2.65"}
    )
    assert any(i["field"] == "weight_kg" and i["code"] == "conflict" for i in d.issues)


def pending(monkeypatch):
    m = mail(BODY)
    _, d = validate(m, candidate())
    sent = []
    monkeypatch.setattr(
        clarification.hackloop,
        "send",
        lambda *a, **kw: sent.append((a, kw)) or {"id": 9001},
    )
    clarification.request(m, m.id, d)
    return m, d, sent


def test_reply_completes_only_requested_fields_and_resumes(monkeypatch):
    m, _d, sent = pending(monkeypatch)
    calls = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda root, sid: calls.append((root, sid)))
    answer = Mail(
        9999,
        m.id,
        m.sender,
        m.to,
        "Re",
        "Pickup ZIP: 21663",
        datetime.now(UTC).isoformat(),
    )
    assert clarification.handle_reply(answer, m.id)
    assert db.validation_get(m.id)["state"] == "ready"
    assert db.get_session(m.id)["request"]["origin"] == "HAM" and len(calls) == 1
    assert clarification.handle_reply(answer, m.id) is False
    # Booking still sees no quote, so even a later yes cannot trigger booking.
    assert not db.get_session(m.id)["quotes"] and len(sent) == 1


def test_yes_is_not_an_acceptance_of_clarification(monkeypatch):
    m, _d, sent = pending(monkeypatch)
    answer = Mail(
        9999,
        m.id,
        m.sender,
        m.to,
        "Re",
        "Yes, go ahead and book it",
        datetime.now(UTC).isoformat(),
    )
    assert clarification.handle_reply(answer, m.id)
    assert db.validation_get(m.id)["state"] == "manual_review"
    assert not db.get_session(m.id)["bookings"] and len(sent) == 1


def test_replay_and_restart_do_not_resend(monkeypatch):
    m, d, sent = pending(monkeypatch)
    clarification.request(m, m.id, d)
    clarification.catch_up()
    assert len(sent) == 1


def test_timeout_never_retries(monkeypatch):
    m = mail(BODY)
    _, d = validate(m, candidate())
    attempts = []

    def send(*a, **kw):
        attempts.append(1)
        raise TimeoutError("response lost")

    monkeypatch.setattr(clarification.hackloop, "send", send)
    clarification.request(m, m.id, d)
    clarification.catch_up()
    clarification.request(m, m.id, d)
    assert attempts == [1] and db.validation_get(m.id)["send_state"] == "unknown"


@pytest.mark.parametrize("pause", ["session", "global", "manual"])
def test_pause_stops_clarification(monkeypatch, pause):
    m = mail(BODY)
    _, d = validate(m, candidate())
    if pause == "session":
        db.set_paused(m.id, True)
    elif pause == "global":
        db.set_global_pause(True)
    else:
        db.takeover(m.id)
    monkeypatch.setattr(
        clarification.hackloop,
        "send",
        lambda *a, **kw: pytest.fail("sent while paused"),
    )
    clarification.request(m, m.id, d)
    assert db.validation_get(m.id)["send_state"] == "pending"


def test_late_reply_does_not_quote(monkeypatch):
    m, _d, sent = pending(monkeypatch)
    monkeypatch.setattr(clarification, "expired", lambda sid: True)
    answer = Mail(
        9999,
        m.id,
        m.sender,
        m.to,
        "Re",
        "Pickup ZIP: 21663",
        datetime.now(UTC).isoformat(),
    )
    assert clarification.handle_reply(answer, m.id)
    assert db.validation_get(m.id)["state"] == "expired" and len(sent) == 1


def test_restart_during_send_is_unknown(monkeypatch):
    m = mail(BODY)
    _, d = validate(m, candidate())
    db.validation_save(
        m.id, "awaiting_clarification", d.model_dump(), dataclasses.asdict(m)
    )
    assert db.clarification_claim(m.id)
    clarification.catch_up()
    assert db.validation_get(m.id)["send_state"] == "unknown"


def test_additive_schema_preserves_existing_data(tmp_path):
    m = mail(BODY)
    db.init_db(str(tmp_path / "test.db"))
    assert db.get_session(m.id)["mails"][0]["body"] == BODY


def test_read_text_pdf_xlsx_docx_sources(monkeypatch, tmp_path):
    import docx
    import openpyxl

    from app.mail import poller
    from app.mail.rfq_sources import read_sources

    txt = tmp_path / "spec.txt"
    txt.write_text("UN1133")
    xlsx = tmp_path / "spec.xlsx"
    book = openpyxl.Workbook()
    book.active.append(["Total gross weight (kg)", 100])
    book.save(xlsx)
    book.close()
    word = tmp_path / "spec.docx"
    doc = docx.Document()
    doc.add_paragraph("Total volume: 2.65")
    doc.save(word)
    m = mail(BODY)
    m.attachments = [{"id": 1}]
    monkeypatch.setattr(poller, "download_attachments", lambda m: [txt, xlsx, word])
    texts, media, issues = read_sources(m)
    assert (
        not media
        and not issues
        and "100" in texts["spec.xlsx"]
        and "UN1133" in texts["spec.txt"]
    )


def test_image_facts_share_the_existing_extraction_call(monkeypatch, tmp_path):
    import json

    from app.mail import poller

    m = mail(
        "Please quote FRA to JFK, with a gross weight of 100 kg and a total volume of 1.0 cbm."
    )
    m.attachments = [{"id": 5, "filename": "packing.png"}]
    image = tmp_path / "packing.png"
    image.write_bytes(b"fake image for mocked gateway")
    monkeypatch.setattr(poller, "download_attachments", lambda m: [image])
    calls = []

    def complete(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(
            {
                "origin": "FRA",
                "destination": "JFK",
                "weight_kg": 100,
                "volume_cbm": 1,
                "attachment_text": "Dangerous goods: UN1950 Class 2.1",
            }
        )

    monkeypatch.setattr(rfq.llm, "complete", complete)
    parsed = rfq.extract_rfq(m, m.id)
    assert parsed and len(calls) == 1
    sources = json.loads(db.kv_get(f"rfq.sources.{m.id}"))
    assert not sources["media"] and "UN1950" in sources["texts"]["image_transcription"]
    _, d = validate(m, parsed, sources["texts"])
    assert any(i["field"] == "dangerous_goods" for i in d.issues)


def test_labelled_weight_of_and_decimal_units():
    from decimal import Decimal

    from app.mail.rfq_validation import numeric_facts

    assert (
        numeric_facts("gross weight of 100 kg, total volume of 1.5 cbm")["weight_kg"]
        == 100
    )
    assert numeric_facts("Gross weight: 2 t")["weight_kg"] == 2000
    assert numeric_facts("Gross weight: 10 lb")["weight_kg"] == Decimal("4.53592370")


def test_unreadable_document_cannot_be_cleared_by_yes(monkeypatch):
    m = mail("FRA to JFK, 100 kg gross, total volume 1 cbm")
    _, d = validate(
        m,
        candidate(origin="FRA", destination="JFK"),
        attachment_issues=[{"field": "attachments", "code": "unreadable"}],
    )
    d.evidence["attachment_texts"] = {}
    monkeypatch.setattr(clarification.hackloop, "send", lambda *a, **kw: {"id": 9001})
    clarification.request(m, m.id, d)
    answer = Mail(
        9999, m.id, m.sender, m.to, "Re", "yes", datetime.now(UTC).isoformat()
    )
    clarification.handle_reply(answer, m.id)
    assert db.validation_get(m.id)["state"] == "manual_review"


def test_shadow_rollback_does_not_continue_pending_quote(monkeypatch):
    m, _d, _sent = pending(monkeypatch)
    monkeypatch.setattr(
        clarification,
        "settings",
        dataclasses.replace(clarification.settings, rfq_validation_mode="shadow"),
    )
    monkeypatch.setattr(
        rfq, "handle_rfq", lambda *a: pytest.fail("rollback must not resume quote")
    )
    answer = Mail(
        9999,
        m.id,
        m.sender,
        m.to,
        "Re",
        "Pickup ZIP: 21663",
        datetime.now(UTC).isoformat(),
    )
    assert clarification.handle_reply(answer, m.id)
    assert db.validation_get(m.id)["state"] == "manual_review"


def test_replay_never_downloads_or_sends(monkeypatch):
    from app.mail import poller
    from app.mail.validation_replay import replay

    m = mail(BODY)
    monkeypatch.setattr(
        poller, "download_attachments", lambda *a: pytest.fail("replay downloaded")
    )
    monkeypatch.setattr(
        clarification.hackloop, "send", lambda *a, **kw: pytest.fail("replay sent")
    )
    rows = replay()
    assert (
        rows[0]["session_id"] == m.id
        and rows[0]["stored_extraction_available"] is False
    )
