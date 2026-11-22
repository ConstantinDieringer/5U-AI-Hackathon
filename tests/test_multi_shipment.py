"""Rounds 89/90 (reason one_of_two): one mail asked for two shipments and a separate all-in
price for each "in the same reply". We priced and quoted only the first.

Round 89, mail 25788 (Hofmann Textilwerk): pickup 65996, fastest, GRU 4 pcs 1281 kg 3.76 cbm
and TLV 1 pc 1812 kg 5.75 cbm.
"""

from datetime import UTC, datetime

from app import db
from app.agents import bids, multi_shipment, rfq
from app.mail.outgoing import validate_customer_text
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


def test_schema_asks_for_further_shipments() -> None:
    schema = rfq.RFQ_SCHEMA["json_schema"]["schema"]
    assert "additional_shipments" in schema["required"]
    assert "Never add the shipments together" in rfq.EXTRACT_PROMPT


def test_siblings_keep_pickup_and_take_own_cargo(tmp_path) -> None:
    _init(tmp_path)
    (tlv,) = multi_shipment.siblings(FIRST, BODY, 25788)
    assert (tlv.destination, tlv.weight_kg, tlv.volume_cbm, tlv.pieces) == ("TLV", 1812, 5.75, 1)
    assert (tlv.origin, tlv.pickup_zip, tlv.preference) == ("FRA", "65996", "fastest")
    assert tlv.additional_shipments == []


def test_sibling_with_weight_not_in_mail_is_dropped(tmp_path) -> None:
    _init(tmp_path)
    invented = FIRST.model_copy(
        update={"additional_shipments": [{**FIRST.additional_shipments[0], "weight_kg": 999}]}
    )
    assert multi_shipment.siblings(invented, BODY, 25788) == []


def test_single_shipment_has_no_siblings(tmp_path) -> None:
    _init(tmp_path)
    assert (
        multi_shipment.siblings(FIRST.model_copy(update={"additional_shipments": []}), BODY, 1)
        == []
    )


def test_one_mail_two_separate_totals_passes_style_rules() -> None:
    tlv = FIRST.model_copy(update={"destination": "TLV", "additional_shipments": []})
    body = multi_shipment.quote_body(
        FIRST,
        [
            (FIRST, _option("GRU", "Riverbend Airlines Cargo", 459169, 4), "4.591,69 €", True),
            (tlv, _option("TLV", "Juniper Cargo Lines", 312050, 2), "3.120,50 €", True),
        ],
    )
    validate_customer_text(body)
    assert "4.591,69 €" in body and "3.120,50 €" in body
    assert "GRU" in body and "TLV" in body
    assert "not a combined price" in body
    assert body.startswith("Hi Petra,")


def test_german_body_and_subject() -> None:
    de = FIRST.model_copy(update={"language": "de"})
    tlv = de.model_copy(update={"destination": "TLV", "additional_shipments": []})
    body = multi_shipment.quote_body(
        de,
        [
            (de, _option("GRU", "Riverbend Airlines Cargo", 459169, 4), "4.591,69 €", False),
            (tlv, None, None, False),
        ],
    )
    validate_customer_text(body)
    assert "zweite Sendung nach TLV" in body
    assert multi_shipment.subject("FRA", ["GRU", "TLV"], "de", "x") == (
        "Luftfracht ab FRA nach GRU und TLV"
    )
    assert multi_shipment.subject("FRA", ["GRU", "TLV"], "en", "x") == (
        "Air freight from FRA to GRU and TLV"
    )


def test_bid_slots_keep_trucker_replies_apart(tmp_path, monkeypatch) -> None:
    """Both shipments ask the same trucker; each reply must land in its own shipment."""
    _init(tmp_path)
    sent: list[dict] = []

    def send(to, subject, body, **_k):
        sent.append({"id": 60_000 + len(sent), "to": to, "subject": subject, "body": body})
        return sent[-1]

    monkeypatch.setattr(bids.hackloop, "send", send)
    trucker = bids.Carrier("Kranich Kurier", "trucker", "kranich-kurier@hackloop.mail", ("65",))
    tlv = multi_shipment.siblings(FIRST, BODY, 25788)[0]
    slot2 = bids.slot_for(25788, 1)
    assert bids.owner(slot2) == 25788
    assert bids.request(25788, FIRST, "FRA", [trucker]) == 1
    assert bids.request(25788, tlv, "FRA", [trucker], slot=slot2) == 1
    assert "1812 kg" in sent[1]["subject"] and sent[1]["subject"].startswith("[AP-25788]")

    for request_mail, price in ((sent[0], "300.34"), (sent[1], "410.00")):
        reply_id = request_mail["id"] + 100
        db.ingest_mail(
            mail_id=reply_id,
            sender=trucker.email,
            recipient="austrian-power@hackloop.mail",
            subject=f"Re: {request_mail['subject']}",
            body=f"Our all-in price for road transport to Frankfurt Airport is {price} EUR.",
            category="carrier_bid",
            references=[request_mail["id"]],
            in_reply_to=request_mail["id"],
        )
        bids.on_bid_mail(
            Mail(
                reply_id,
                request_mail["id"],
                trucker.email,
                "austrian-power@hackloop.mail",
                f"Re: {request_mail['subject']}",
                f"Our all-in price for road transport to Frankfurt Airport is {price} EUR.",
                datetime.now(UTC).isoformat(),
                references=[request_mail["id"]],
            ),
            25788,
        )
    assert [str(o.total_eur) for o in bids.offers(25788)] == ["300.34"]
    assert [str(o.total_eur) for o in bids.offers(slot2)] == ["410.00"]
    bids.close(25788)
    bids.close(slot2)


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
