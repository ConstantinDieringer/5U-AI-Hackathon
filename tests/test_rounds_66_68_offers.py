"""Rounds 63 to 68 (21 Nov 2026, from 21:17 UTC): carriers sent their offers only as
attachments (PDF, DOCX, TXT, MP3). Board losses traced to offers we could not read:

- round 66: Grautal's "Total, all-in 382.98 EUR" unread, Taunusfracht picked: 33.08 accuracy
- round 67: Domblick's voice offer (358.64 EUR, 4 h) unread, Schwabenbote picked: 27.36
- round 68: two DOCX tables (" | Total |  |  | 41.80") unread
- rounds 63/65: voice offer / voice RFQ, the gateway refuses audio input

Texts below are the real attachment texts and local Whisper transcripts."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app import db, llm
from app.agents import bids
from app.agents.rfq import best_truck
from app.mail import audio
from app.models import Mail

VOICE_67_DOMBLICK = (
    "Hi, this is the dispatcher at Domblic Courier Deansed. I am calling back about your "
    "pickup. So we collect at postcode 40527 and bring it to Duesseldorf Airport. You gave us "
    "1,728 kilos. We can do that for 358 euros and 64 cents. I say again, 358 euros and 64 "
    "cents and that is all in. The driver can be there four hours after your order. I keep "
    "the price until the 21st of November, at 2156 UTC. You find the offer number in my mail. "
    "You do not need to book. Okay, bye."
)
VOICE_63_BODENSEE = (
    "Good day, Bowdoin Zefract here, with a message for the forwarding team. Collection at "
    "postcode 5, 1252, delivery to Cologne Airport, 1,410 kilos. The base charge is 115 euros. "
    "The weight charge is 225 euros and 60 cents. So the total is 340 euros and 60 cents. "
    "Waiting time after the first 30 minutes is 45 euros for each hour. And that is not part "
    "of the total. We need 12 hours from your order to the collection. The offer is valid "
    "until the 21st of November at 2132 UTC time. The offer number is in the text of our "
    "mail. No booking is necessary. Goodbye."
)
DOCX_68_ISARWELLE = (
    "OFFER TRK-6306\nIsarwelle Spedition  |  isarwelle-spedition@hackloop.mail\n"
    "Lead time: 29 hours from order to collection\nRoute: 84455 (zone 84) to MUC Munich\n"
    "Not part of this offer: waiting time at the pickup address after the first 30 minutes, "
    "45.00 EUR for each hour.\n"
    "Pos | Description | Quantity | Unit price (EUR) | Amount (EUR)\n"
    "1 | Base charge, zone 84 | 1 | 38.00 | 38.00\n"
    "2 | Weight charge | 76 kg | 0.05 | 3.80\n"
    " | Total |  |  | 41.80"
)
PDF_66_GRAUTAL = (
    "Pickup quotation\nGrautal Trucking\nQuotation no. TRK-6120\nPickup postcode 45299\n"
    "Delivery Duesseldorf airport (DUS)\nChargeable weight 1494 kg\nCharge Amount\n"
    "Pickup and delivery to DUS airport 382.98 EUR\nTotal, all-in 382.98 EUR\n"
    "In words three hundred eighty-two euros and ninety-eight cents\nCollection in 7 hours\n"
)


def letter(price: str, hours: int) -> str:
    return (
        f"Our price for this transport is {price} EUR. This price is all-in.\n"
        f"Our driver can be at the pickup address {hours} hours after your order."
    )


def table(total: str, hours: int) -> str:
    return (
        f"OFFER\nLead time {hours} hours from order to collection\n"
        "Pos Description Quantity Unit price (EUR) Amount (EUR)\n"
        f"Total {total}\nNot part of this offer: waiting time, 45.00 EUR for each hour."
    )


@pytest.mark.parametrize(
    "text,price,hours",
    [
        (DOCX_68_ISARWELLE, Decimal("41.80"), 29),
        (PDF_66_GRAUTAL, Decimal("382.98"), 7),
    ],
)
def test_document_totals_from_rounds_66_and_68(text, price, hours):
    assert bids.document_price(text) == (price, "EUR")
    assert bids.parse_transit_days(text) * 24 == pytest.approx(hours)


@pytest.mark.parametrize(
    "spoken,price,hours",
    [
        (VOICE_67_DOMBLICK, Decimal("358.64"), 4),  # said twice, "four" in words
        (VOICE_63_BODENSEE, Decimal("340.60"), 12),  # components + waiting fee + total
    ],
)
def test_spoken_offers(spoken, price, hours):
    text = bids.normalize_spoken(spoken)
    assert bids.spoken_price(text) == (price, "EUR")
    assert bids.parse_transit_days(text) * 24 == pytest.approx(hours)
    assert bids.timing_kind(text) == "collection_lead"


def test_spoken_offer_with_two_different_prices_is_not_guessed():
    text = bids.normalize_spoken("We can do 200 euros. Or for express 300 euros.")
    assert bids.spoken_price(text) is None


@pytest.fixture
def sid(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(bids, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("no gateway call"))
    yield 67
    bids.close(67)


def offer_mail(mail_id: int, sender: str, path) -> Mail:
    return Mail(
        mail_id, mail_id, sender, "team", "Re: [AP-67] Pickup 40527 to DUS airport",
        "Please find our offer attached.", datetime.now(UTC).isoformat(),
        attachments=[{"id": 1, "filename": path.name}],
    )  # fmt: skip


def test_round_67_voice_offer_wins_the_truck_balance(sid, tmp_path, monkeypatch):
    """With Domblick's voice offer read, the 50/50 rule picks it (board best: 27.36 -> 45)."""
    files = {
        "hansekogge-trucking@hackloop.mail": ("o1.txt", table("220.24", 24)),
        "rheinufer-spedition@hackloop.mail": ("o2.txt", table("181.96", 28)),
        "schwabenbote-transporte@hackloop.mail": ("o3.txt", table("215.24", 21)),
        "domblick-kurierdienst@hackloop.mail": ("voice-message.mp3", None),
    }
    monkeypatch.setattr(audio, "_local", lambda *args: VOICE_67_DOMBLICK)
    for n, (sender, (name, text)) in enumerate(files.items()):
        path = tmp_path / str(n) / name
        path.parent.mkdir()
        path.write_bytes(text.encode() if text else b"ID3\x04\x00" + b"\x00" * 50)
        monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None, p=path: [p])
        bids.on_bid_mail(offer_mail(18847 + n, sender, path), sid)
    got = {o.carrier.email: o.total_eur for o in bids.offers(sid)}
    assert got["domblick-kurierdienst@hackloop.mail"] == Decimal("358.64")
    assert len(got) == 4
    assert best_truck(bids.offers(sid)).carrier.email == "domblick-kurierdienst@hackloop.mail"


def test_round_66_grautal_wins_the_truck_balance(sid, tmp_path, monkeypatch):
    files = {
        "bodenseefracht@hackloop.mail": table("130.70", 31),
        "domblick-kurierdienst@hackloop.mail": letter("367.98", 13),
        "taunusfracht@hackloop.mail": letter("284.22", 15),
        "maintal-transport-logistik@hackloop.mail": letter("198.40", 33),
        "grautal-trucking@hackloop.mail": PDF_66_GRAUTAL,
    }
    for n, (sender, text) in enumerate(files.items()):
        path = tmp_path / f"offer{n}.txt"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None, p=path: [p])
        bids.on_bid_mail(offer_mail(18500 + n, sender, path), sid)
    assert len(bids.offers(sid)) == 5
    assert best_truck(bids.offers(sid)).carrier.email == "grautal-trucking@hackloop.mail"


def test_unreadable_reply_counts_as_answered(sid, tmp_path, monkeypatch):
    """Rounds 63/64: a reply we cannot price must not hold the RFQ for the whole bid window."""
    asked = {
        "grautal-trucking@hackloop.mail": PDF_66_GRAUTAL,
        "taunusfracht@hackloop.mail": "Offer attached. Base charge 90 EUR",  # no total
    }
    with bids._cond:
        req = bids._open.setdefault(sid, bids._Request())
        for email in asked:
            req.asked[email] = bids.carrier_by_email(email)
    for n, (sender, text) in enumerate(asked.items()):
        path = tmp_path / f"r{n}.txt"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(bids, "_attachment_paths", lambda mail, sid=None, p=path: [p])
        bids.on_bid_mail(offer_mail(18600 + n, sender, path), sid)
    start = time.monotonic()
    offers = bids.wait(sid, 5.0)
    assert time.monotonic() - start < 1.0
    assert [o.carrier.email for o in offers] == ["grautal-trucking@hackloop.mail"]
