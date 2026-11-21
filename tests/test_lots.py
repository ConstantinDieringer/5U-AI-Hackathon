"""Lot totals in code (real RFQs 1628, 1834, 1967)."""

import json
from decimal import Decimal

from app import db
from app.agents import rfq
from app.mail.lots import parse_lots
from app.models import Mail

MAIL_1834 = (
    "Hi,\n\nLot 1: 9 pieces, 345 kg gross, 0.58 cbm\nLot 2: 2 pieces, 141 kg gross, 2.04 cbm\n\n"
    "Please quote one all-in price in EUR for both lots shipping together as a single "
    "consignment under one air waybill."
)
MAIL_1967 = (
    "Hi,\n\nCould you quote the cheapest air freight option to LOS (Lagos, NG) for automotive "
    "sensors? Pickup is at our site in 36627 (Fulda), Germany, ready on 2026-11-29. Please "
    "arrange the truck to the airport and quote one all-in price in EUR.\n\n"
    "Lot 1: 4 drums, together 730 kg gross (642 kg net), together 1.68 cbm\n"
    "Lot 2: 4 cartons, each 48 kg gross (41 kg net), each 110 x 110 x 52 cm\n"
    "Lot 3: 5 cases, each 206 kg gross (191 kg net), each 120 x 100 x 8 cm\n\n"
    "Please book all lots as a single consignment under one air waybill.\n\nMira Petersen\n"
    "Nordlicht Audio"
)
MAIL_1628_LOTS = (
    "Lot 1: 1 piece, gross weight 618 kg, volume 0.74 cbm\n"
    "Lot 2: 1 piece, gross weight 380 kg, volume 1.65 cbm\n"
)


def test_real_lot_mails() -> None:
    t = parse_lots(MAIL_1967)
    assert t is not None and t.lots == 3
    assert t.weight_kg == Decimal(1952)  # 730 + 4*48 + 5*206, gross not net
    assert t.volume_cbm == Decimal("4.6768")  # 1.68 + 4*1.1*1.1*0.52 + 5*1.2*1.0*0.08
    t = parse_lots(MAIL_1834)
    assert (t.weight_kg, t.volume_cbm) == (Decimal(486), Decimal("2.62"))
    t = parse_lots(MAIL_1628_LOTS)
    assert (t.weight_kg, t.volume_cbm) == (Decimal(998), Decimal("2.39"))


def test_no_lots_or_unreadable_lot_keeps_llm_values() -> None:
    assert parse_lots("12 pieces, 676 kg gross and 5.79 cbm.") is None
    assert parse_lots("Lot 1: 2 pieces, 100 kg\nLot 2: 1 piece, 50 kg, 0.3 cbm") is None


def test_extraction_uses_lot_sums_when_llm_returns_zero(tmp_path, monkeypatch) -> None:
    db.init_db(str(tmp_path / "t.db"))
    bad = {  # the real gpt-5-nano answer for mail 1967
        "origin": "FRA", "pickup_zip": "36627", "destination": "LOS", "weight_kg": 0,
        "volume_cbm": 0, "pieces": None, "preference": "cheapest", "ready_date": "2026-11-29",
        "language": "de", "customer_name": "Mira",
    }  # fmt: skip
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **k: json.dumps(bad))
    mail = Mail(1967, 1967, "nordlicht-audio@hackloop.mail", "x", "Air freight quote to LOS",
                MAIL_1967, "2026-11-21T15:49:35+00:00")  # fmt: skip
    r = rfq.extract_rfq(mail, 1967)
    assert r is not None and r.weight_kg == 1952 and abs(r.volume_cbm - 4.6768) < 1e-9
