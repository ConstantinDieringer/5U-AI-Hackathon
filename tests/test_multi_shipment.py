"""Rounds 89/90 (reason one_of_two): one mail asked for two shipments and a separate all-in
price for each "in the same reply". We priced and quoted only the first.

Round 89, mail 25788 (Hofmann Textilwerk): pickup 65996, fastest, GRU 4 pcs 1281 kg 3.76 cbm
and TLV 1 pc 1812 kg 5.75 cbm.
"""

from datetime import UTC, datetime

from app import db
from app.agents import bids, rfq
from app.models import Mail, RateOption, Rfq

BODY = """Hi there,

Could you quote the fastest air freight option for each of these two shipments? Both will be
ready for pickup on 2026-11-23 at our site in Wiesbaden, Germany (postcode 65996).

1. GRU (Sao Paulo, BR) — automotive sensors, 4 pieces, gross weight 1281 kg, volume 3.76 cbm.
2. TLV (Tel Aviv, IL) — machine parts, 1 piece, gross weight 1,812 kg, volume 5.75 cbm.

Could you send both quotes in the same reply, with a separate all-in price in EUR for each
shipment rather than a combined total?

Petra Hofmann
"""

FIRST = Rfq(
    origin="FRA",
    pickup_zip="65996",
    destination="GRU",
    weight_kg=1281,
    volume_cbm=3.76,
    pieces=4,
    preference="fastest",
    customer_name="Petra",
    additional_shipments=[
        {
            "origin": None,
            "pickup_zip": None,
            "destination": "TLV",
            "weight_kg": 1812,
            "volume_cbm": 5.75,
            "pieces": 1,
        }
    ],
)


def _option(dest: str, airline: str, total: int, days: int) -> RateOption:
    return RateOption(
        airline=airline,
        origin="FRA",
        destination=dest,
        transit_days=days,
        total_minor=total,
        currency="EUR",
        total_eur_minor=total,
        source="portal",
    )


def _init(tmp_path) -> None:
    db.init_db(str(tmp_path / "t.db"))
    db.ingest_mail(
        mail_id=25788,
        sender="hofmann-textilwerk@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="Air freight quote request: GRU and TLV",
        body=BODY,
        category="rfq",
        received_at=datetime.now(UTC).isoformat(),
    )


def test_handle_quotes_both_shipments_in_one_reply(tmp_path, monkeypatch) -> None:
    """End to end through rfq.handle_rfq: one extraction call, two pricing passes, ONE quote
    mail with a separate all-in total per shipment, and never a second quote."""
    import json

    from app.pricing.portal_sync import PortalResult
    from tests.test_rfq_agent import Stub

    _init(tmp_path)
    stub = Stub()
    extracted = {
        "origin": "FRA",
        "pickup_zip": None,
        "destination": "GRU",
        "weight_kg": 1281,
        "volume_cbm": 3.76,
        "pieces": 4,
        "preference": "fastest",
        "ready_date": "2026-11-23",
        "language": "en",
        "customer_name": "Petra",
        "customer_questions": [],
        "service_constraints": [],
        "attachment_text": "",
        "additional_shipments": FIRST.additional_shipments,
    }
    calls: list[str] = []

    def complete(purpose, *a, **k):
        calls.append(purpose)
        return json.dumps(extracted)

    prices = {"GRU": 459169, "TLV": 312050}
    monkeypatch.setattr(rfq.llm, "complete", complete)
    monkeypatch.setattr(rfq.hackloop, "send", stub.send)
    monkeypatch.setattr(bids.hackloop, "send", stub.send)
    monkeypatch.setattr(rfq, "_bid_carriers", lambda r, o: ([], []))
    monkeypatch.setattr(rfq, "_sheets", lambda r: [])
    monkeypatch.setattr(
        rfq,
        "portal_result",
        lambda r, **kw: PortalResult(
            [_option(r.destination, "Riverbend Airlines Cargo", prices[r.destination], 4)]
        ),
    )
    body = BODY.replace("Wiesbaden, Germany (postcode 65996)", "Frankfurt, we deliver to FRA")
    now = datetime.now(UTC).isoformat()
    mail = Mail(25788, 25788, "hofmann-textilwerk@hackloop.mail", "austrian-power@hackloop.mail",
                "Air freight quote request: GRU and TLV", body, now)  # fmt: skip
    rfq.handle_rfq(mail, 25788)

    quotes = [m for m in stub.sent if m["to"] == "hofmann-textilwerk@hackloop.mail"]
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote["in_reply_to"] == 25788
    assert "4.591,69 €" in quote["body"] and "3.120,50 €" in quote["body"]
    assert quote["subject"] == "Air freight from FRA to GRU and TLV"
    assert calls.count("extract_rfq") == 1
    assert not db.claim_quote_send(25788)
