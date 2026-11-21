"""RFQ agent and carrier bids. No real 5U or LLM calls: hackloop, llm and the portal are stubbed."""

import base64
import dataclasses
import io
import json
import re
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pdfplumber
import pytest

from app import db
from app.agents import bids, rfq
from app.mail.style import amount
from app.models import Mail, RateOption, Rfq
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
    assert rfq.best_air(air, bids_, "cheapest").airline == "A"  # X loses the balanced bid pick


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
    return re.findall(r"(?:EUR|USD|€)\s*[\d.,]+|[\d.,]+\s*(?:EUR|USD|€)", text)


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
    t = bids.Offer(bids.Carrier("T", "trucker", "t@hackloop.mail", ("51",)), Decimal(200), 1, 9)
    text = rfq.quote_text(r, o, 254500, t, 1)
    assert _amounts(text) == ["2.545 €"]
    assert "Hi Sara" in text and "51447" in text and "all-in" in text
    de = rfq.quote_text(r.model_copy(update={"language": "de"}), o, 254500, None, 0)
    assert _amounts(de) == ["2.545 €"]


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
    assert _amounts(text) == ["3.203,58 €"]
    assert (
        "Earliest departure from MUC is 29 November 2026, arriving in LAX on 2 December 2026."
        in text
    )

    de = rfq.quote_text(r.model_copy(update={"language": "de"}), o, 320358, None, 0, today=today)
    assert _amounts(de) == ["3.203,58 €"]
    assert (
        "Frühester Abflug ab MUC ist am 29. November 2026, Ankunft in LAX am 2. Dezember 2026."
        in de
    )


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


def test_rfq_preserves_questions_and_constraints() -> None:
    parsed = Rfq.model_validate(
        {
            "destination": "LAX",
            "weight_kg": 998,
            "customer_questions": [" Earliest departure? ", "Earliest departure?"],
            "service_constraints": ["One consignment", " Single AWB "],
        }
    )
    assert parsed.customer_questions == ["Earliest departure?"]
    assert parsed.service_constraints == ["One consignment", "Single AWB"]


def test_extract_repairs_invalid_structured_output(monkeypatch) -> None:
    valid = {
        "origin": "MUC",
        "pickup_zip": None,
        "destination": "LAX",
        "weight_kg": 998,
        "volume_cbm": 2.39,
        "pieces": 2,
        "preference": "fastest",
        "ready_date": "2026-11-29",
        "language": "en",
        "customer_name": "Deniz",
        "customer_questions": ["Confirm earliest departure and arrival"],
        "service_constraints": ["Single consignment under one AWB"],
    }
    outputs = iter(["not json", json.dumps(valid)])
    purposes = []

    def complete(purpose, *args, **kwargs):
        purposes.append(purpose)
        return next(outputs)

    monkeypatch.setattr(rfq.llm, "complete", complete)
    mail = Mail(1, 1, CUSTOMER, TEAM, "MUC-LAX", MAIL_1628, datetime.now(UTC).isoformat())
    parsed = rfq.extract_rfq(mail, 1)
    assert parsed is not None
    assert parsed.customer_questions == ["Confirm earliest departure and arrival"]
    assert purposes == ["extract_rfq", "extract_rfq_retry"]


def test_bid_carriers_are_capped_and_observed_lanes_rank_first(monkeypatch) -> None:
    from types import SimpleNamespace

    truck = bids.Carrier("Truck", "trucker", "truck@hackloop.mail", ("51",))
    carriers = {
        f"air-{index}@hackloop.mail": bids.Carrier(
            f"Air {index}", "airline", f"air-{index}@hackloop.mail", ("ORD",)
        )
        for index in range(4)
    }
    monkeypatch.setattr(rfq, "truckers_for_zone", lambda *args: [truck])
    monkeypatch.setattr(
        rfq,
        "airlines_for",
        lambda *args: [SimpleNamespace(channel="email_only", email=email) for email in carriers],
    )
    monkeypatch.setattr(rfq, "carrier_by_email", carriers.get)
    monkeypatch.setattr(
        rfq.served_lanes,
        "serves",
        lambda email, *args: True if email.endswith("2@hackloop.mail") else None,
    )
    monkeypatch.setattr(
        rfq,
        "settings",
        dataclasses.replace(rfq.settings, min_bid_carriers=2, max_bid_carriers=3),
    )
    request = Rfq(pickup_zip="51447", destination="ORD", weight_kg=100)
    trucks, airlines = rfq._bid_carriers(request, "CGN")
    assert trucks == [truck]
    # airlines have their own cap: a trucker no longer takes an airline slot (round 20)
    assert len(airlines) == 3
    assert airlines[0].name == "Air 2"
    monkeypatch.setattr(
        rfq,
        "settings",
        dataclasses.replace(rfq.settings, max_trucker_bids=5, max_bid_carriers=5),
    )
    many = [bids.Carrier(f"T{i}", "trucker", f"t{i}@hackloop.mail", ("51",)) for i in range(5)]
    monkeypatch.setattr(rfq, "truckers_for_zone", lambda *args: many)
    trucks, airlines = rfq._bid_carriers(request, "CGN")
    assert len(trucks) == 5 and len(airlines) == 4


def test_composer_requires_question_coverage_and_one_verified_total(
    monkeypatch,
) -> None:
    request = Rfq(
        origin="MUC",
        destination="LAX",
        weight_kg=998,
        ready_date="2026-11-29",
        customer_questions=["When do we arrive?"],
    )
    option = RateOption("Fjordline", "MUC", "LAX", 3, 320358, "EUR", 320358, "portal")
    body = (
        "Hello,\n\nFjordline from MUC to LAX: 3.203,58 € all-in. "
        "Arrival is 2 December 2026.\n\nYour austrian Power Team\n"
    )
    monkeypatch.setattr(
        rfq.llm,
        "complete",
        lambda *args, **kwargs: json.dumps(
            {
                "body": body,
                "answered_questions": [0],
                "unavailable_questions": [],
                "addressed_constraints": [],
                "unavailable_constraints": [],
            }
        ),
    )
    assert rfq.compose_quote_text(
        request, option, 320358, None, 7, today=datetime(2026, 11, 21).date()
    ) == body.replace("Your austrian Power Team", "Moritz\nYour austrian Power Team")

    monkeypatch.setattr(
        rfq.llm,
        "complete",
        lambda *args, **kwargs: json.dumps(
            {
                "body": body + "EUR 1.00",
                "answered_questions": [],
                "unavailable_questions": [],
                "addressed_constraints": [],
                "unavailable_constraints": [],
            }
        ),
    )
    fallback = rfq.compose_quote_text(request, option, 320358, None, 7)
    assert _amounts(fallback) == ["3.203,58 €"]
    assert "not yet available" not in fallback


@pytest.mark.usefixtures("compose_repair_on")
def test_composer_repairs_mixed_route_phrase_once(monkeypatch) -> None:
    request = Rfq(origin="FRA", destination="GRU", weight_kg=100)
    option = RateOption("Test Air", "FRA", "GRU", 2, 147500, "EUR", 147500, "sheet")
    bodies = [
        (
            "Hello,\n\nTest Air from origin to nach GRU: 1.475 € all-in."
            "\n\nYour austrian Power Team"
        ),
        ("Hello,\n\nTest Air from FRA to GRU: 1.475 € all-in.\n\nYour austrian Power Team"),
    ]
    purposes: list[str] = []

    def complete(purpose, *args, **kwargs):
        purposes.append(purpose)
        return json.dumps(
            {
                "body": bodies.pop(0),
                "answered_questions": [],
                "unavailable_questions": [],
                "addressed_constraints": [],
                "unavailable_constraints": [],
            }
        )

    monkeypatch.setattr(rfq.llm, "complete", complete)

    text = rfq.compose_quote_text(request, option, 147500, None, 1)

    assert "FRA to GRU" in text
    assert purposes == ["compose_quote", "compose_quote_repair"]


def test_composer_without_repair_sends_checked_template_after_one_call(monkeypatch) -> None:
    """Default (COMPOSE_REPAIR off): a failed check costs no second composer call."""
    request = Rfq(origin="FRA", destination="GRU", weight_kg=100)
    option = RateOption("Test Air", "FRA", "GRU", 2, 147500, "EUR", 147500, "sheet")
    purposes: list[str] = []

    def complete(purpose, *args, **kwargs):
        purposes.append(purpose)
        return json.dumps(
            {
                "body": "Hello,\n\nTest Air from origin to nach GRU: 1.475 € all-in.",
                "answered_questions": [],
                "unavailable_questions": [],
                "addressed_constraints": [],
                "unavailable_constraints": [],
            }
        )

    monkeypatch.setattr(rfq.llm, "complete", complete)
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, compose_repair=False))

    text = rfq.compose_quote_text(request, option, 147500, None, 1)

    assert purposes == ["compose_quote"]
    assert "GRU" in text and "nach GRU" not in text  # deterministic copy, not the bad draft


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
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, quote_target_s=5.0))
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
    mail = dataclasses.replace(_rfq_mail(), subject="FW: AMS TLV 104 crates")

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


def test_old_rfq_is_skipped(stub) -> None:
    mail = _rfq_mail()
    mail.received_at = "2026-11-21T15:00:00+00:00"
    rfq.handle_rfq(mail, 265)
    assert stub.sent == []


def test_503_fallback_requests_known_carrier_once(stub, monkeypatch):
    from types import SimpleNamespace

    from app.pricing.reference import carrier_by_name

    carrier = carrier_by_name("Juniper Cargo Lines")
    monkeypatch.setattr(
        rfq,
        "airlines_for",
        lambda *args: [SimpleNamespace(channel="portal", code="JP", email=carrier.email)],
    )
    monkeypatch.setattr(
        rfq,
        "portal_result",
        lambda *args, **kw: PortalResult(
            [opt("Aurelia Air Cargo", 2000, 3, "portal")], ["JP", "JP"], ["503"]
        ),
    )
    monkeypatch.setattr(bids, "wait", lambda *args, **kw: [])
    monkeypatch.setattr(
        rfq,
        "settings",
        dataclasses.replace(
            rfq.settings, allow_incomplete_door_quote=True, rfq_validation_mode="shadow"
        ),
    )
    mail = _rfq_mail()
    rfq.handle_rfq(mail, 265)
    requests = [x for x in stub.sent if x["to"] == carrier.email]
    assert len(requests) == 1
    assert stub.sent[-1]["to"] == CUSTOMER


def test_incomplete_door_quote_is_not_sent(stub, monkeypatch) -> None:
    trucker = bids.Carrier("T", "trucker", "t@hackloop.mail", ("51",))
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *args: ([trucker], []))
    monkeypatch.setattr(bids, "wait", lambda *args, **kwargs: [])
    mail = _rfq_mail()
    rfq.handle_rfq(mail, 265)
    assert all(message["to"] != CUSTOMER for message in stub.sent)
    assert db.get_session(265)["status"] == "error"


def test_existing_bid_request_is_not_resent_after_restart(stub) -> None:
    mail = _rfq_mail()
    carrier = bids.Carrier("T", "trucker", "t@hackloop.mail", ("51",))
    db.record_outgoing(
        mail_id=900,
        recipient=carrier.email,
        subject="old request",
        body="old body",
        category="bid_request",
        session_id=mail.id,
        raw={"id": 900},
    )
    request = Rfq(pickup_zip="51447", destination="ORD", weight_kg=100)
    try:
        assert bids.request(mail.id, request, "CGN", [carrier]) == 0
        assert all(message["to"] != carrier.email for message in stub.sent)
    finally:
        bids.close(mail.id)


# ---------------------------------------------------------------- resume after restart


def test_catch_up_resumes_open_rfq_without_new_llm_call_or_bid_mail(stub, monkeypatch) -> None:
    trucker = bids.Carrier(
        "Elbkante Logistik", "trucker", "elbkante-logistik@hackloop.mail", ("51",)
    )
    monkeypatch.setattr(rfq, "_bid_carriers", lambda r, o: ([trucker], []))
    llm_calls: list[str] = []
    monkeypatch.setattr(
        rfq.llm, "complete", lambda purpose, *a, **k: llm_calls.append(purpose) or "{}"
    )
    mail = _rfq_mail()
    # state left by the run before the restart: extracted, bid asked, trucker answered
    db.set_status(265, "pricing")
    db.set_request(265, Rfq(pickup_zip="51447", destination="ORD", weight_kg=676,
                            volume_cbm=5.79, preference="fastest").model_dump())  # fmt: skip
    db.record_outgoing(mail_id=900, recipient=trucker.email, subject="[AP-265] Pickup",
                       body="...", category="bid_request", session_id=265)  # fmt: skip
    db.ingest_mail(mail_id=901, sender=trucker.email, recipient=TEAM,
                   subject="Re: [AP-265] Pickup", body="All-in EUR 210.00, 1 day.",
                   category="carrier_bid", received_at=mail.received_at, in_reply_to=900,
                   references=[900])  # fmt: skip

    started: list[threading.Thread] = []
    real_thread = threading.Thread

    def capture(*a, **k):
        t = real_thread(*a, **k)
        started.append(t)
        return t

    monkeypatch.setattr(rfq.threading, "Thread", capture)
    assert rfq.catch_up() == 1
    for t in started:
        t.join(10)

    # stored extraction reused; an invalid composition goes out as the checked template
    # copy without a second composer call (COMPOSE_REPAIR off by default)
    assert llm_calls == ["compose_quote"]
    assert [m["to"] for m in stub.sent] == [CUSTOMER]  # no second bid mail, just the quote
    assert "trucking to CGN" in stub.sent[0]["body"]  # the replayed trucker bid is in
    assert db.get_session(265)["status"] == "quoted"
    assert rfq.catch_up() == 0  # quoted: never resumed again


def test_catch_up_skips_old_rfq(stub) -> None:
    mail = _rfq_mail()
    with db.tx() as c:
        c.execute("UPDATE sessions SET created_at = ? WHERE id = 265",
                  ("2026-11-21T15:00:00+00:00",))  # fmt: skip
    assert mail and rfq.catch_up() == 0


def test_extract_ends_without_a_retry_when_the_budget_is_spent(monkeypatch) -> None:
    purposes = []

    def complete(purpose, *args, **kwargs):
        purposes.append(purpose)
        raise rfq.llm.LlmBudgetError("budget spent")

    monkeypatch.setattr(rfq.llm, "complete", complete)
    mail = Mail(1, 1, CUSTOMER, TEAM, "MUC-LAX", MAIL_1628, datetime.now(UTC).isoformat())
    assert rfq.extract_rfq(mail, 1) is None
    assert purposes == ["extract_rfq"]
