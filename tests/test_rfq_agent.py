"""RFQ agent and carrier bids. No real 5U or LLM calls: hackloop, llm and the portal are stubbed."""

import dataclasses
import json
import re
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app import db
from app.agents import bids, rfq
from app.models import Mail, RateOption, Rfq

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


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("text", "amount", "cur"),
    [
        ("Our all-in price: EUR 1,234.50, transit 2 days", "1234.50", "EUR"),
        ("Preis: 1.234,50 EUR inkl. Maut", "1234.50", "EUR"),
        ("Pickup 180 EUR\nTotal all-in: 245.00 EUR", "245.00", "EUR"),
        ("We can do it for € 99", "99.00", "EUR"),
        ("Rate USD 2,000.00", "2000.00", "USD"),
    ],
)
def test_parse_price(text: str, amount: str, cur: str) -> None:
    assert bids.parse_price(text) == (Decimal(amount), cur)


def test_parse_price_none() -> None:
    assert bids.parse_price("Thanks, we will get back to you.") is None


def test_parse_transit() -> None:
    assert bids.parse_transit_days("transit time 3 days") == 3
    assert bids.parse_transit_days("Laufzeit 2 Tage") == 2
    assert bids.parse_transit_days("pickup within 24 hours") == 1
    assert bids.parse_transit_days("no time given") is None


# ---------------------------------------------------------------- choice


def opt(airline: str, eur: int, days: int, source: str = "sheet") -> RateOption:
    return RateOption(airline, "CGN", "ORD", days, eur * 100, "EUR", eur * 100, source)


def test_best_air_fastest_and_balanced_bid() -> None:
    air = [opt("A", 950, 5), opt("B", 1500, 3)]
    bids_ = [
        opt("X", 900, 9, "bid"),
        opt("Y", 1000, 2, "bid"),
        opt("Z", 1400, 3, "bid"),
    ]  # Y is the balanced bid
    assert rfq.best_air(air, bids_, "fastest").airline == "Y"
    assert (
        rfq.best_air(air, bids_, "cheapest").airline == "A"
    )  # X loses the balanced bid pick


def test_best_truck_cheapest_when_no_transit() -> None:
    c = bids.Carrier("T1", "trucker", "t1@hackloop.mail", ("51",))
    d = bids.Carrier("T2", "trucker", "t2@hackloop.mail", ("51",))
    offers = [
        bids.Offer(c, Decimal(300), None, 1),
        bids.Offer(d, Decimal(250), None, 2),
    ]
    assert rfq.best_truck(offers).carrier.name == "T2"


# ---------------------------------------------------------------- text


def _amounts(text: str) -> list[str]:
    return re.findall(r"(?:EUR|USD)\s*[\d.,]+|[\d.,]+\s*(?:EUR|USD)", text)


def test_quote_text_has_exactly_one_total() -> None:
    r = Rfq(
        pickup_zip="51447",
        destination="ORD",
        weight_kg=676,
        volume_cbm=5.79,
        preference="fastest",
        customer_name="Sara",
    )
    o = opt("Cobalt Skylift", 2345, 2)
    t = bids.Offer(
        bids.Carrier("T", "trucker", "t@hackloop.mail", ("51",)), Decimal(200), 1, 9
    )
    text = rfq.quote_text(r, o, 254500, t, 1)
    assert _amounts(text) == ["EUR 2,545.00"]
    assert "Hi Sara" in text and "51447" in text and "all-in" in text
    de = rfq.quote_text(r.model_copy(update={"language": "de"}), o, 254500, None, 0)
    assert _amounts(de) == ["2.545,00 EUR"]


MAIL_1628 = (
    "Hello,\n\nCould you please quote the fastest air freight option from MUC to LAX (Los "
    "Angeles, US) for our furniture samples? The cargo will be ready for pickup on 2026-11-29, "
    "and we will deliver it to Munich airport (MUC) ourselves.\n\n"
    "Lot 1: 1 piece, gross weight 618 kg, volume 0.74 cbm\n"
    "Lot 2: 1 piece, gross weight 380 kg, volume 1.65 cbm\n\n"
    "Please send both lots together as one consignment under a single air waybill. Could you "
    "give us one all-in price in EUR for the complete shipment and confirm the earliest "
    "available departure and arrival?\n\n"
    "Viele Gruesse / Many greetings\nDeniz Yilmaz\nRheintal Elektronik GmbH"
)  # real mail 1628: asks for earliest departure and arrival


def test_quote_answers_earliest_departure_and_arrival() -> None:
    from app.mail import language

    r = Rfq(
        origin="MUC",
        destination="LAX",
        weight_kg=998,
        volume_cbm=2.39,
        preference="fastest",
        ready_date="2026-11-29",
        language=language.detect(MAIL_1628),
        customer_name="Deniz",
    )
    o = RateOption("Fjordline Air Cargo", "MUC", "LAX", 3, 320358, "EUR", 320358, "portal")
    today = datetime(2026, 11, 21).date()
    text = rfq.quote_text(r, o, 320358, None, 0, today=today)
    assert _amounts(text) == ["EUR 3,203.58"]
    assert "Earliest departure from MUC is 29 Nov 2026, arriving in LAX on 2 Dec 2026." in text

    de = rfq.quote_text(r.model_copy(update={"language": "de"}), o, 320358, None, 0, today=today)
    assert _amounts(de) == ["3.203,58 EUR"]
    assert "Frühester Abflug ab MUC ist am 29.11.2026, Ankunft in LAX am 02.12.2026." in de


def test_schedule_door_to_port_and_past_ready_date() -> None:
    today = datetime(2026, 11, 21).date()
    door = Rfq(pickup_zip="51447", destination="ORD", weight_kg=676, ready_date="2026-11-25")
    assert rfq.schedule(door, 2, today) == (
        datetime(2026, 11, 26).date(),
        datetime(2026, 11, 28).date(),
    )  # +1 day for the trucking leg
    past = door.model_copy(update={"pickup_zip": None, "origin": "CGN", "ready_date": "2026-11-01"})
    assert rfq.schedule(past, 2, today)[0] == today  # never before today
    assert rfq.schedule(door.model_copy(update={"ready_date": None}), 2, today) is None
    assert rfq.schedule(door.model_copy(update={"ready_date": "next week"}), 2, today) is None


# ---------------------------------------------------------------- whole flow


class Stub:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.next_id = 1000

    def send(self, to, subject, body, in_reply_to=None, references=None):
        self.next_id += 1
        m = {
            "id": self.next_id,
            "from": TEAM,
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
            "references": [],
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
        rfq, "portal_options", lambda r: [opt("Aurelia Air Cargo", 2000, 3, "portal")]
    )
    monkeypatch.setattr(
        rfq, "settings", dataclasses.replace(rfq.settings, quote_target_s=5.0)
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
    mail = _rfq_mail()

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
    assert "trucking to CGN" in quote["body"]
    s = db.get_session(265)
    assert s["status"] == "quoted"
    assert not db.claim_quote_send(265)  # never a second quote


def test_old_rfq_is_skipped(stub) -> None:
    mail = _rfq_mail()
    mail.received_at = "2026-11-21T15:00:00+00:00"
    rfq.handle_rfq(mail, 265)
    assert stub.sent == []
