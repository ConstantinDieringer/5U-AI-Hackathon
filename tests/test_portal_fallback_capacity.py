"""Case 12460: a portal outage must reach the lane's actual airlines within the bid cap."""

import dataclasses
import time
from datetime import UTC, datetime

import pytest

from app import db
from app.agents import bids, rfq
from app.models import Mail, Rfq
from app.pricing.lanes import LaneAirline
from app.pricing.portal_sync import PortalResult
from app.pricing.reference import carrier_by_name

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "auer-maschinenbau@hackloop.mail"
PORTAL = [
    ("AU", "Aurelia Air Cargo"),
    ("FQ", "Fjordline Air Cargo"),
    ("NV", "Nova Atlas Cargo"),
]
SPECULATIVE = [
    "Danube Wings Cargo",
    "Juniper Cargo Lines",
    "Quetzal Air Freight",
    "Tamarind Airways",
    "Ultraviolet Air Freight",
]


@pytest.fixture()
def case(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "outage.db"))
    stamp = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=12460,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Air freight quote to EZE",
        body="Pickup 71445 to EZE, 485 kg / 3.66 cbm",
        category="rfq",
        received_at=stamp,
    )
    mail = Mail(12460, 12460, CUSTOMER, TEAM, "Air freight quote to EZE", "", stamp)
    request = Rfq(pickup_zip="71445", destination="EZE", weight_kg=485, volume_cbm=3.66)
    lanes = [
        LaneAirline(name, "portal", code, carrier_by_name(name).email) for code, name in PORTAL
    ] + [LaneAirline(name, "email_only", email=carrier_by_name(name).email) for name in SPECULATIVE]
    monkeypatch.setattr(rfq, "airlines_for", lambda *_: lanes)
    monkeypatch.setattr(rfq.served_lanes, "serves", lambda *_: None)
    monkeypatch.setattr(rfq, "sheet_options", lambda *_: [])
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, max_bid_carriers=5))
    monkeypatch.setattr(bids, "wait", lambda *a, **kw: [])
    monkeypatch.setattr(
        rfq,
        "compose_quote_text",
        lambda *args: rfq.quote_text(*args),
    )
    sent = []

    def send(to, subject, body, **kwargs):
        mid = 50000 + len(sent)
        sent.append({"id": mid, "to": to, "body": body, **kwargs})
        carrier = rfq.carrier_by_email(to)
        if carrier:
            if carrier.kind == "trucker":
                reply = "All-in price 194.80 EUR, 1 day"
            elif carrier.name in {name for _, name in PORTAL}:
                reply = "All-in price 1200.00 EUR, 2 days"
            else:
                reply = "We do not serve this lane/zone: STR-EZE. We fly FRA-EZE."
            bids.on_bid_mail(
                Mail(mid + 10000, 12460, to, TEAM, "Re: " + subject, reply, stamp),
                12460,
            )
        return {"id": mid}

    monkeypatch.setattr(rfq.hackloop, "send", send)
    yield mail, request, sent
    bids.close(12460)


def test_503_reserves_slots_and_sends_a_complete_quote(case, monkeypatch):
    mail, request, sent = case

    def outage(*args, **kwargs):
        early_airlines = [row for row in sent if rfq.carrier_by_email(row["to"]).kind == "airline"]
        assert len(early_airlines) == 2  # three slots remain for the real lane carriers
        return PortalResult([], ["AU", "AU", "FQ", "NV"], ["503: contact airline"])

    monkeypatch.setattr(rfq, "portal_result", outage)
    rfq._price_and_quote(mail, 12460, request, "STR", time.time(), "enforce")
    recipients = db.bid_request_recipients(12460)
    air = [email for email in recipients if rfq.carrier_by_email(email).kind == "airline"]
    assert len(air) == 5
    assert {carrier_by_name(name).email for _, name in PORTAL} <= recipients
    for _, name in PORTAL:
        assert sum(row["to"] == carrier_by_name(name).email for row in sent) == 1
    quotes = db.get_session(12460)["quotes"]
    assert len(quotes) == 1 and quotes[0]["total_minor"] == 139480
    assert sum(row["to"] == CUSTOMER for row in sent) == 1


def test_healthy_portal_releases_reserved_slots(case, monkeypatch):
    mail, request, _ = case
    monkeypatch.setattr(rfq, "portal_result", lambda *a, **kw: PortalResult())
    rfq._price_and_quote(mail, 12460, request, "STR", time.time(), "enforce")
    recipients = db.bid_request_recipients(12460)
    air = [email for email in recipients if rfq.carrier_by_email(email).kind == "airline"]
    assert set(air) == {carrier_by_name(name).email for name in SPECULATIVE}
    assert not db.get_session(12460)["quotes"]  # trucking alone is not an air quote


def test_restart_counts_persisted_airline_requests_separately_from_trucks(case):
    _, _, _ = case
    names = SPECULATIVE[:2] + ["Flinkwagen Logistik", "Kranich Kurier"]
    for mid, name in enumerate(names, 60000):
        db.record_outgoing(
            mail_id=mid,
            recipient=carrier_by_name(name).email,
            subject="[AP-12460] Bid request",
            body="request",
            category="bid_request",
            session_id=12460,
        )
    bids.close(12460)
    candidates = [carrier_by_name(name) for _, name in PORTAL]
    candidates += [carrier_by_name(name) for name in SPECULATIVE]
    selected = rfq._limit_airline_requests(12460, candidates)
    assert {carrier.name for carrier in selected} == set(SPECULATIVE[:2]) | {
        name for _, name in PORTAL
    }


def test_excluded_or_declining_portal_airline_reserves_no_slot(case, monkeypatch):
    _, request, _ = case
    request = request.model_copy(update={"excluded_airlines": ["aurelia air cargo"]})
    monkeypatch.setattr(
        rfq.served_lanes,
        "serves",
        lambda email, *args: (
            False if email == carrier_by_name("Fjordline Air Cargo").email else None
        ),
    )
    assert set(rfq._portal_bid_carriers(request, "STR")) == {"NV"}


def test_legacy_full_budget_is_reported_without_exceeding_cap(case, monkeypatch):
    mail, request, sent = case
    for mid, name in enumerate(SPECULATIVE, 60000):
        db.record_outgoing(
            mail_id=mid,
            recipient=carrier_by_name(name).email,
            subject="[AP-12460] Air freight",
            body="request",
            category="bid_request",
            session_id=12460,
        )
    monkeypatch.setattr(
        rfq, "portal_result", lambda *a, **kw: PortalResult([], ["AU", "FQ", "NV"], ["503"])
    )
    rfq._price_and_quote(mail, 12460, request, "STR", time.time(), "enforce")
    assert all(row["to"] not in {carrier_by_name(name).email for _, name in PORTAL} for row in sent)
    case_data = db.get_session(12460)
    warnings = [
        event for event in case_data["events"] if event["kind"] == "portal_fallback_blocked"
    ]
    assert len(warnings) == 1 and len(warnings[0]["payload"]["carriers"]) == 3
    assert not case_data["quotes"]


def test_pause_during_portal_read_blocks_fallback_and_customer_quote(case, monkeypatch):
    mail, request, sent = case

    def pause_during_read(*args, **kwargs):
        db.set_paused(12460, True)
        return PortalResult([], ["AU", "FQ", "NV"], ["503"])

    monkeypatch.setattr(rfq, "portal_result", pause_during_read)
    rfq._price_and_quote(mail, 12460, request, "STR", time.time(), "enforce")
    blocked = {carrier_by_name(name).email for _, name in PORTAL} | {CUSTOMER}
    assert all(row["to"] not in blocked for row in sent)
    assert not db.get_session(12460)["quotes"]
