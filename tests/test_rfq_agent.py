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


def test_incomplete_door_quote_is_not_sent(stub, monkeypatch) -> None:
    trucker = bids.Carrier("T", "trucker", "t@hackloop.mail", ("51",))
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *args: ([trucker], []))
    monkeypatch.setattr(bids, "wait", lambda *args, **kwargs: [])
    mail = _rfq_mail()
    rfq.handle_rfq(mail, 265)
    assert all(message["to"] != CUSTOMER for message in stub.sent)
    assert db.get_session(265)["status"] == "error"
