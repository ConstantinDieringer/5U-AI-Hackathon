"""RFQ agent and carrier bids. No real 5U or LLM calls: hackloop, llm and the portal are stubbed."""

import base64
import dataclasses
import io
import json
import threading
from datetime import UTC, datetime

import pdfplumber
import pytest

from app import db
from app.agents import bids, rfq
from app.mail.style import amount
from app.models import Mail, RateOption
from app.pricing.portal_sync import PortalResult

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "lindqvist-medtech@hackloop.mail"
BODY = (
    "Hi,\n\nCould you quote an all-in price in EUR for the fastest air freight option to ORD "
    "(Chicago, US)? Please arrange trucking from our site in 51447 Bergisch Gladbach, Germany, "
    "to the departure airport. The shipment is ready for pickup on 2026-11-25.\n\n"
    "It's 12 pieces of printed marketing material, 676 kg gross and 5.79 cbm.\n\nSara\n"
    "Lindqvist Medtech"
)  # the first real RFQ, mail 265


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
    yield


# ---------------------------------------------------------------- choice


def opt(airline: str, eur: int, days: int, source: str = "sheet") -> RateOption:
    return RateOption(airline, "CGN", "ORD", days, eur * 100, "EUR", eur * 100, source)


# ---------------------------------------------------------------- whole flow


class Stub:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.next_id = 1000

    def send(self, to, subject, body, in_reply_to=None, references=None, attachments=None):
        self.next_id += 1
        m = {
            "id": self.next_id,
            "from": TEAM,
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
            "references": [],
            "attachments": attachments or [],
            "received_at": datetime.now(UTC).isoformat(),
        }
        self.sent.append(m)
        return m


@pytest.fixture()
def stub(monkeypatch):
    s = Stub()
    extracted = {
        "origin": None,
        "pickup_zip": "51447",
        "destination": "ORD",
        "weight_kg": 676,
        "volume_cbm": 5.79,
        "pieces": 12,
        "preference": "fastest",
        "ready_date": "2026-11-25",
        "language": "en",
        "customer_name": "Sara",
    }
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **k: json.dumps(extracted))
    monkeypatch.setattr(rfq.hackloop, "send", s.send)
    monkeypatch.setattr(bids.hackloop, "send", s.send)
    monkeypatch.setattr(
        rfq,
        "portal_result",
        lambda r, **kw: PortalResult([opt("Aurelia Air Cargo", 2000, 3, "portal")]),
    )
    return s


def _rfq_mail(mid: int = 265) -> Mail:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Fastest air freight to ORD",
        body=BODY,
        category="rfq",
        received_at=now,
    )
    return Mail(mid, mid, CUSTOMER, TEAM, "Fastest air freight to ORD", BODY, now)


def test_handle_rfq_door_to_port_with_trucker_bid(stub, monkeypatch) -> None:
    monkeypatch.setattr(
        rfq,
        "_bid_carriers",
        lambda r, o: (
            [
                bids.Carrier(
                    "Elbkante Logistik",
                    "trucker",
                    "elbkante-logistik@hackloop.mail",
                    ("51",),
                )
            ],
            [],
        ),
    )
    mail = dataclasses.replace(
        _rfq_mail(),
        subject="FW: AMS TLV 104 crates",
        body=f"{BODY}\nPlease send the offer as PDF.",  # QUOTE_PDF_ON_REQUEST
    )

    def reply() -> None:  # the trucker answers while the agent waits
        while not stub.sent:
            pass
        req = stub.sent[0]
        bid = Mail(
            2000,
            2000,
            "elbkante-logistik@hackloop.mail",
            TEAM,
            f"Re: {req['subject']}",
            "Our all-in price is EUR 210.00, pickup tomorrow, 1 day.",
            datetime.now(UTC).isoformat(),
        )
        bids.on_bid_mail(bid, None)  # session found via the [AP-265] tag

    threading.Thread(target=reply, daemon=True).start()
    rfq.handle_rfq(mail, 265)

    quote = stub.sent[-1]
    assert quote["to"] == CUSTOMER and quote["in_reply_to"] == 265
    assert quote["subject"] == "Air freight CGN to ORD"
    assert "trucking to CGN" in quote["body"]
    s = db.get_session(265)
    assert s["status"] == "quoted"
    stored_quote = s["quotes"][0]
    assert stored_quote["air_currency"] == "EUR"
    assert stored_quote["total_minor"] == stored_quote["air_total_minor"] + 21000
    # QUOTE_PDF: the same all-in total as a code-rendered PDF attachment
    [pdf] = quote["attachments"]
    assert pdf["filename"] == "Quotation_AP-265.pdf" and pdf["content_type"] == "application/pdf"
    with pdfplumber.open(io.BytesIO(base64.b64decode(pdf["content_base64"]))) as doc:
        text = doc.pages[0].extract_text() or ""
    assert amount(stored_quote["total_minor"]) in text and "Pickup 51447" in text
    assert not db.claim_quote_send(265)  # never a second quote


def _fra_pek_waits(monkeypatch, fallback: list[str]) -> list[set[str] | None]:
    """Run a FRA-PEK RFQ with an Orion sheet price; return the until_kinds of each wait."""
    extracted = {
        "origin": "FRA",
        "pickup_zip": None,
        "destination": "PEK",
        "weight_kg": 700,
        "volume_cbm": 6.58,
        "pieces": 9,
        "preference": "fastest",
        "ready_date": "2026-11-27",
        "language": "en",
        "customer_name": "Anna",
    }
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **k: json.dumps(extracted))
    lumen = bids.Carrier("Lumen Air Cargo", "airline", "lumen-air-cargo@hackloop.mail", ())
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *args: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *args: {"LM": lumen})
    monkeypatch.setattr(
        rfq, "portal_result", lambda r, **kw: PortalResult(email_fallbacks=list(fallback))
    )
    orion = RateOption("Orion Cargo Air", "FRA", "PEK", 4, 269221, "EUR", 269221, "sheet")
    monkeypatch.setattr(rfq, "_sheets", lambda *args: [orion])
    waits: list[set[str] | None] = []
    monkeypatch.setattr(
        bids, "wait", lambda sid, t, until_kinds=None: waits.append(until_kinds) or []
    )
    mail = dataclasses.replace(_rfq_mail(36098), subject="Air freight quote request: FRA to PEK")
    rfq.handle_rfq(mail, 36098)
    return waits


def test_portal_503_airline_reply_is_awaited_despite_sheet_price(stub, monkeypatch) -> None:
    """Round 121 (#36098): Lumen (portal) answered 503 and was asked by email, but Orion's
    sheet price was quoted at once and rejected too_low. The emailed airline is awaited."""
    assert {"airline"} in _fra_pek_waits(monkeypatch, ["LM"])


def test_sheet_price_without_portal_fallback_does_not_wait_for_airlines(
    stub, monkeypatch
) -> None:
    """Counterexample: no 503 fallback, so the sheet price is quoted without an extra wait."""
    assert all(not (k and "airline" in k) for k in _fra_pek_waits(monkeypatch, []))


def _fra_pek_asked(monkeypatch, stub, fallback: list[str]) -> set[str]:
    """FRA-PEK with the airline cap used up and Lumen missing from the reservation map (stale
    "do not serve" list); returns every carrier address a bid request went to."""
    extracted = {
        "origin": "FRA",
        "pickup_zip": None,
        "destination": "PEK",
        "weight_kg": 700,
        "volume_cbm": 6.58,
        "pieces": 9,
        "preference": "fastest",
        "ready_date": "2026-11-27",
        "language": "en",
        "customer_name": "Anna",
    }
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **k: json.dumps(extracted))
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, max_bid_carriers=1))
    vega = bids.Carrier("Vega Skyfreight", "airline", "vega-skyfreight@hackloop.mail", ())
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *args: ([], [vega]))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *args: {})
    monkeypatch.setattr(
        rfq, "portal_airlines", lambda o, d: [{"code": "LM", "name": "Lumen Air Cargo"}]
    )
    monkeypatch.setattr(
        rfq, "portal_result", lambda r, **kw: PortalResult(email_fallbacks=list(fallback))
    )
    orion = RateOption("Orion Cargo Air", "FRA", "PEK", 4, 269221, "EUR", 269221, "sheet")
    monkeypatch.setattr(rfq, "_sheets", lambda *args: [orion])
    monkeypatch.setattr(bids, "wait", lambda *args, **kwargs: [])
    mail = dataclasses.replace(_rfq_mail(36099), subject="Air freight quote request: FRA to PEK")
    rfq.handle_rfq(mail, 36099)
    return {m["to"] for m in stub.sent if m["to"] != CUSTOMER}


def test_portal_503_airline_is_asked_past_cap_and_stale_lane_list(stub, monkeypatch) -> None:
    """The 503 fallback is the lane's live price: neither a used-up airline cap nor a missed
    reservation may drop it (formerly portal_fallback_blocked, silently unanswerable)."""
    asked = _fra_pek_asked(monkeypatch, stub, ["LM"])
    assert "lumen-air-cargo@hackloop.mail" in asked
    assert "vega-skyfreight@hackloop.mail" in asked


def test_portal_airline_is_not_emailed_without_503(stub, monkeypatch) -> None:
    """Counterexample: the portal priced the lane, so nobody is emailed past the cap."""
    asked = _fra_pek_asked(monkeypatch, stub, [])
    assert "lumen-air-cargo@hackloop.mail" not in asked


def test_incomplete_door_quote_is_not_sent(stub, monkeypatch) -> None:
    trucker = bids.Carrier("T", "trucker", "t@hackloop.mail", ("51",))
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *args: ([trucker], []))
    monkeypatch.setattr(bids, "wait", lambda *args, **kwargs: [])
    mail = _rfq_mail()
    rfq.handle_rfq(mail, 265)
    assert all(message["to"] != CUSTOMER for message in stub.sent)
    assert db.get_session(265)["status"] == "error"


def test_damaged_lithium_rfq_is_refused_not_quoted(stub) -> None:
    """Forbidden cargo (IATA DGR SP A154): a reasoned refusal, no price, no DG question."""
    body = (
        "Hello,\n\nwe need fastest air freight from 40474 Düsseldorf to Dubai DXB.\n"
        "Cargo: 2 boxes, 60x40x40 cm, 45 kg total.\nContent: Defective / swollen "
        "Lithium-Ion battery packs (UN3480) returned for lab failure analysis.\n\nSara"
    )
    mail = dataclasses.replace(_rfq_mail(), body=body, subject="Urgent Air Export DUS -> DXB")
    rfq.handle_rfq(mail, 265)

    [reply] = stub.sent
    assert reply["to"] == CUSTOMER and reply["in_reply_to"] == 265
    assert "A154" in reply["body"] and "€" not in reply["body"]
    s = db.get_session(265)
    assert s["status"] == "lost" and not s["quotes"]


# ---------------------------------------------------------------- origin == destination


# real round 2530 customer mail (attachment line dropped)
_MAIL_2530 = (
    "Hello,\n\nplease quote air freight for this shipment:\n\n"
    "- Pickup: from our site at 21663 Lueneburg, Germany (please arrange the truck)\n"
    "- Destination airport: Toronto (YYZ), CA\n"
    "- Commodity: maintenance spray cans, 4 pieces\n"
    "- Gross weight: 855 kg\n"
    "- Volume: 3.74 cbm\n"
    "- Ready date: 2026-11-26\n"
    "- Service: the cheapest option\n"
    "- The goods are general cargo, non-dangerous\n\n"
    "Please reply with your all-in price in EUR.\n\nSara\nLindqvist Medtech"
)


def _door_rfq(monkeypatch, stub, extracted: dict, body: str, mid: int) -> None:
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **k: json.dumps(extracted))
    monkeypatch.setattr(bids, "wait", lambda *args, **kwargs: [])
    mail = dataclasses.replace(_rfq_mail(mid), subject="Air freight quote request", body=body)
    rfq.handle_rfq(mail, mid)


def test_pickup_zip_never_yields_a_destination_to_destination_air_bid(stub, monkeypatch) -> None:
    """Round 2530: pickup 21663 Lueneburg -> YYZ, the LLM copied YYZ into origin and four
    airlines were asked for YYZ-YYZ. The pickup zone decides: HAM."""
    body = _MAIL_2530
    extracted = {
        "origin": "YYZ",
        "pickup_zip": "21663",
        "destination": "YYZ",
        "weight_kg": 855.0,
        "volume_cbm": 3.74,
        "pieces": 4,
        "preference": "cheapest",
        "ready_date": "2026-11-26",
        "language": "en",
        "customer_name": "Sara",
    }
    _door_rfq(monkeypatch, stub, extracted, body, 2530)
    bid_requests = [m for m in stub.sent if m["to"] != CUSTOMER]
    assert bid_requests
    assert not any("YYZ to YYZ" in m["body"] or "YYZ-YYZ" in m["subject"] for m in bid_requests)
    air = [m for m in bid_requests if "Please quote air freight" in m["body"]]
    assert air and all("from HAM to YYZ" in m["body"] for m in air)
    assert all("to HAM airport" in m["body"] for m in bid_requests if m not in air)


def test_normal_german_pickup_to_yyz_asks_airlines_from_its_zone_airport(stub, monkeypatch) -> None:
    """Counterexample: a Munich pickup with no LLM origin flies MUC-YYZ as before."""
    body = _MAIL_2530.replace("21663 Lueneburg", "80331 Munich").replace("855 kg", "300 kg")
    extracted = {
        "origin": None,
        "pickup_zip": "80331",
        "destination": "YYZ",
        "weight_kg": 300,
        "volume_cbm": 3.74,
        "pieces": 4,
        "preference": "cheapest",
        "ready_date": "2026-11-26",
        "language": "en",
        "customer_name": "Sara",
    }
    _door_rfq(monkeypatch, stub, extracted, body, 2531)
    air = [m for m in stub.sent if "Please quote air freight" in m["body"]]
    assert air and all("from MUC to YYZ" in m["body"] for m in air)


def test_no_air_bid_or_option_when_origin_is_the_destination(stub) -> None:
    """Guard behind resolve_origin: no bid mail and no rate option for an airport to itself."""
    from app.models import Rfq
    from app.pricing.pricing import resolve_origin

    _rfq_mail(2532)
    # Frankfurt pickup to FRA: the zone airport is the destination, so ask instead
    same = Rfq(pickup_zip="60311", destination="FRA", weight_kg=100, volume_cbm=0.5)
    assert resolve_origin(same) is None
    airline = bids.Carrier("Lumen Air Cargo", "airline", "lumen-air-cargo@hackloop.mail", ("FRA",))
    assert bids.request(2532, same, "FRA", [airline]) == 0
    assert stub.sent == []
    with pytest.raises(ValueError):
        RateOption("Lumen Air Cargo", "YYZ", "yyz", 3, 100, "EUR", 100, "bid")
