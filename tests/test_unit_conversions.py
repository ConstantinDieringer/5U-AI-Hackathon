"""Thread 32120: cubic feet became cubic metres and inflated the quote to EUR 102,709.62."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app import db
from app.agents import bid_reader, rfq
from app.mail import lots
from app.mail.rfq_validation import numeric_facts, validate
from app.models import Mail, RateOption, Rfq
from app.pricing.portal_sync import PortalResult
from tests.test_recent_rounds_e2e import build_world

pytestmark = pytest.mark.core

BODY = """Hi there,
Could you send me an all-in price in EUR for the cheapest air freight option from MUC to YYZ?
The import shipment consists of 5 pieces of automotive sensors, with a gross weight of
3258.44 lb and a total volume of 306.18 cubic feet. The goods will be ready on 2026-11-25,
and we'll deliver them to MUC ourselves, so there's no need to arrange pickup.
Please include cargo insurance in the all-in price, on the terms of our frame agreement:
goods value 90000 EUR, insured at 110 % of that value, premium rate 0.45 %.
Cheers,
Klaus Weidner"""


def candidate(volume=306.18):
    return Rfq(
        origin="MUC",
        destination="YYZ",
        weight_kg=1478,
        volume_cbm=volume,
        pieces=5,
        preference="cheapest",
        ready_date="2026-11-25",
        customer_name="Klaus",
    )


@pytest.mark.core
@pytest.mark.parametrize("model_volume", [306.18, 8.67005208953856])
def test_32120_never_relabels_cubic_feet_or_converts_twice(model_volume):
    mail = Mail(32120, 32120, "customer@hackloop.mail", "team", "MUC to YYZ", BODY, db.now())
    checked, draft = validate(mail, candidate(model_volume))
    assert not draft.issues
    assert checked.weight_kg == pytest.approx(1478.0035221028)
    assert checked.volume_cbm == pytest.approx(8.67005208953856)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Total volume 306.18 cubic feet", "8.67005208953856"),
        ("Volume (cu ft): 306.18", "8.67005208953856"),
        ("total volume of 306.18 cuft", "8.67005208953856"),
        ("volume 306,18 ft³", "8.67005208953856"),
        ("Total volume 1000 litres", "1"),
        ("Volume 1000000 cm3", "1"),
        ("Total volume 8.67 cubic metres", "8.67"),
        ("Total volume 8.67 cbm", "8.67"),
    ],
)
def test_volume_units_are_explicit(text, expected):
    assert numeric_facts(text)["volume_cbm"] == Decimal(expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Gross weight 3258.44 pounds", "1478.0035221028"),
        ("Gross weight 1000 grams", "1"),
        ("Gross weight 1000 mg", "0.001"),
        ("Gross weight 1.5 tonnes", "1500"),
    ],
)
def test_mass_units_are_converted_before_pricing(text, expected):
    assert numeric_facts(text)["weight_kg"] == Decimal(expected)


@pytest.mark.parametrize(
    ("dimensions", "expected"),
    [
        ("1200 x 800 x 1000 mm", "3.8400"),
        ("120 x 80 x 100 cm", "3.8400"),
        ("1.2 x 0.8 x 1 m", "3.8400"),
        ("48 x 40 x 50 inches", "6.2926"),
        ("4 x 3 x 5 feet", "6.7960"),
        ("4 x 3 x 5 ft", "6.7960"),
    ],
)
def test_dimension_factor_is_cubed_and_piece_count_applied(dimensions, expected):
    text = f"4 crates, gross weight 300 kg, dimensions {dimensions} each"
    assert numeric_facts(text)["volume_cbm"] == Decimal(expected)


def test_lot_cubic_feet_and_per_piece_volume():
    total = lots.parse_lots("Lot 1: 4 crates, 300 kg gross, each 10 cubic feet")
    assert total.volume_cbm == Decimal("1.13267386368")


@pytest.mark.core
@pytest.mark.parametrize("path", ["normal", "forced", "shadow", "emergency"])
def test_32120_entry_points_use_correct_quantities_and_insured_total(tmp_path, monkeypatch, path):
    world = build_world(tmp_path, monkeypatch)
    world.extraction = candidate().model_dump()
    mail = Mail(
        32120,
        32120,
        "customer@hackloop.mail",
        "team",
        "MUC to YYZ",
        BODY,
        datetime.now(UTC).isoformat(),
    )
    db.ingest_mail(
        mail_id=mail.id,
        sender=mail.sender,
        recipient=mail.to,
        subject=mail.subject,
        body=mail.body,
        category="rfq",
        received_at=mail.received_at,
    )
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *a: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *a: {})
    monkeypatch.setattr(rfq, "_sheets", lambda *a: [])
    priced = []

    def rates(request, **kwargs):
        priced.append(request)
        assert request.volume_cbm == pytest.approx(8.67005208953856)
        assert request.weight_kg == pytest.approx(1478.0035221028)
        return PortalResult(
            [RateOption("Lumen Air Cargo", "MUC", "YYZ", 3, 295601, "EUR", 295601, "portal")]
        )

    monkeypatch.setattr(rfq, "portal_result", rates)
    if path == "normal":
        rfq.handle_rfq(mail, mail.id)
    elif path == "emergency":
        from app.ui import drafts

        db.set_request(mail.id, candidate().model_dump())
        monkeypatch.setattr(drafts, "sheet_options", lambda *a: [])
        monkeypatch.setattr(drafts, "portal_result", rates)
        quote = drafts.build(mail.id)
        assert (quote["total_minor"], quote["insurance_minor"]) == (340151, 44550)
        assert not world.sent
        return
    else:
        rfq._price_and_quote(
            mail,
            mail.id,
            candidate(),
            "MUC",
            time.time(),
            "shadow" if path == "shadow" else "enforce",
            path == "forced",
        )
    [quote] = db.get_session(mail.id)["quotes"]
    assert quote["total_minor"] == 340151 and quote["insurance_minor"] == 44550
    assert len(world.to(mail.sender)) == 1 and len(priced) == 1


@pytest.mark.core
def test_unsupported_cubic_unit_cannot_reach_pricing(tmp_path, monkeypatch):
    build_world(tmp_path, monkeypatch)
    body = BODY.replace("cubic feet", "cubic yards")
    mail = Mail(32120, 32120, "customer@hackloop.mail", "team", "RFQ", body, db.now())
    db.ingest_mail(
        mail_id=mail.id,
        sender=mail.sender,
        recipient=mail.to,
        subject=mail.subject,
        body=mail.body,
        category="rfq",
    )
    requested = []
    monkeypatch.setattr(
        "app.agents.clarification.request", lambda m, sid, draft: requested.append(draft)
    )
    monkeypatch.setattr(
        rfq, "portal_result", lambda *a, **kw: pytest.fail("unknown unit reached pricing")
    )
    assert rfq._price(mail, mail.id, candidate(), "MUC", time.time(), "shadow", True) is None
    assert any(i["field"] == "volume_cbm" for i in requested[0].issues)


@pytest.mark.core
def test_model_cannot_relabel_foreign_bid_as_eur():
    raw = json.dumps({"total": "100", "currency": "EUR", "transit_days": 2})
    assert bid_reader.check(raw, "Total USD 100, 2 days", {"USD", "EUR"}) is None


def test_per_piece_volume_uses_count_and_compound_separators():
    assert numeric_facts("4 crates, each 10 cubic feet")["volume_cbm"] == Decimal("1.13267386368")
    assert numeric_facts("Total volume 1,000.50 cubic feet")["volume_cbm"] == Decimal(
        "28.331005015296"
    )


@pytest.mark.parametrize("volume", ["1,000 cubic feet", "8.67 cbm or volume 20 cubic feet"])
def test_ambiguous_or_conflicting_volumes_need_clarification(volume):
    mail = Mail(
        1,
        1,
        "customer",
        "team",
        "MUC to YYZ",
        "We deliver to MUC ourselves, 5 pieces, 1478 kg gross. Total volume " + volume,
        db.now(),
    )
    _, draft = validate(mail, candidate())
    assert any(i["field"] == "volume_cbm" for i in draft.issues)
