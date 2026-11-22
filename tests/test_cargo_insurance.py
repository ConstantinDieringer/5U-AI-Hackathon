"""Real failures 26566/26764: required cargo insurance vanished from all-in totals."""

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app import db
from app.agents import bids, rfq
from app.mail.style import amount
from app.models import Mail, RateOption, Rfq
from app.pricing.portal_sync import PortalResult

BODY_26566 = """Hi,

Could you quote the fastest air freight option from DUS to MEX (Mexico City, MX) and confirm
the transit time? Please include the all-in price in EUR.
We'll deliver to DUS ourselves. The shipment is ready on 2026-11-29: 1 piece of printed
marketing material, 176 kg gross, 1.26 cbm.
Please include cargo insurance in the all-in price, on the terms of our frame agreement:
goods value 22000 EUR, insured at 110 % of that value, premium rate 0.40 %.
Sara
Lindqvist Medtech"""

BODY_26764 = """Hi there,

Hope all's well with you. Could you send me an all-in price in EUR for the fastest air freight
option to GRU (Sao Paulo, BR)? The shipment will be ready for pickup on 2026-11-27 at our site
in 22911 (Hamburg North), Germany. Please include arranging the truck from our site to the airport.
It's 6 pieces of electronic control units, with a gross weight of 671 kg and a total volume
of 6.19 cbm. Could you also let me know the earliest arrival you can offer at GRU?

Thanks for taking a look!

We also need cargo insurance inside your all-in price: the goods are worth 81000 EUR, cover
is 110 % of the value and the premium rate is 0.30 % (our frame agreement).
Cheers,
Klaus Weidner
Weidner Spielwaren GmbH, Nuremberg"""


@pytest.fixture(autouse=True)
def database(tmp_path):
    db.init_db(str(tmp_path / "insurance.db"))


def mail(body=BODY_26566, mid=26566):
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender="customer@hackloop.mail",
        recipient="team@hackloop.mail",
        subject="Air freight quote",
        body=body,
        category="rfq",
        received_at=now,
    )
    return Mail(
        mid, mid, "customer@hackloop.mail", "team@hackloop.mail", "Air freight quote", body, now
    )


def request(mid=26566):
    if mid == 26566:
        return Rfq(
            origin="DUS",
            destination="MEX",
            weight_kg=176,
            volume_cbm=1.26,
            pieces=1,
            preference="fastest",
            ready_date="2026-11-29",
        )
    return Rfq(
        origin="HAM",
        pickup_zip="22911",
        destination="GRU",
        weight_kg=671,
        volume_cbm=6.19,
        pieces=6,
        preference="fastest",
        ready_date="2026-11-27",
    )


def install_prices(monkeypatch, req, air_minor, truck_minor=0):
    option = RateOption(
        "Test Air", req.origin, req.destination, 2, air_minor, "EUR", air_minor, "portal"
    )
    offers = []
    if truck_minor:
        offers = [
            bids.Offer(
                bids.Carrier("Test Truck", "trucker", "truck@hackloop.mail", ("22",)),
                Decimal(truck_minor) / 100,
                1,
                99,
            )
        ]
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *a: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *a: {})
    monkeypatch.setattr(rfq, "_sheets", lambda *a: [])
    monkeypatch.setattr(rfq, "portal_result", lambda *a, **kw: PortalResult([option]))
    monkeypatch.setattr(bids, "request", lambda *a, **kw: 0)
    monkeypatch.setattr(bids, "wait", lambda *a, **kw: None)
    monkeypatch.setattr(bids, "offers", lambda *a: offers)
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **kw: "{}")
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, price_adjust_pct=0))
    return option


def test_full_rfq_flow_reads_insurance_after_polite_closing(monkeypatch):
    m, req = mail(BODY_26764, 26764), request(26764)
    install_prices(monkeypatch, req, 304950, 25238)
    monkeypatch.setattr(rfq, "extract_rfq", lambda *a: req)
    sent = []
    monkeypatch.setattr(rfq.hackloop, "send", lambda **kw: sent.append(kw) or {"id": 50002})
    rfq.handle_rfq(m, m.id)
    assert len(sent) == 1 and amount(356918) in sent[0]["body"]
