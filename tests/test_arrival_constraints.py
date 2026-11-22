"""Round 83/23465: cheapest that arrives in time, not the shortest transit at any price."""

import dataclasses
import time
from datetime import UTC, date, datetime

import pytest

from app import db
from app.agents import bids, clarification, rfq
from app.models import Mail, RateOption, Rfq
from app.pricing.portal_sync import PortalResult
from app.ui import drafts

TODAY = date(2026, 11, 22)
BODY = (
    "Could you please provide an all-in air freight quote in EUR from STR to MEL? "
    "The shipment will be ready on 2026-11-26, and we will deliver it to STR ourselves.\n\n"
    "6 pieces, gross weight 1841 kg, total volume 4.63 cbm.\n\n"
    "The goods must arrive at MEL no later than 2026-12-02. "
    "Among the options that meet this deadline, price will determine our choice."
)


def request(**changes):
    return Rfq(
        origin="STR",
        destination="MEL",
        weight_kg=1841,
        volume_cbm=4.63,
        pieces=6,
        ready_date="2026-11-26",
        preference="fastest",
    ).model_copy(update=changes)


def mail(body=BODY):
    return Mail(
        23465,
        23465,
        "sonnberg-roesterei@hackloop.mail",
        "team@hackloop.mail",
        "Air freight STR to MEL",
        body,
        datetime.now(UTC).isoformat(),
    )


def options():
    return [
        RateOption(name, "STR", "MEL", days, price, "EUR", price, source)
        for name, days, price, source in (
            ("Yellowtail Air Freight", 5, 756000, "sheet"),
            ("Aurelia Air Cargo", 3, 804000, "portal"),
            ("Solace Air Cargo", 4, 606000, "portal"),
        )
    ]


@pytest.fixture
def flow(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "arrival.db"))
    root = mail()
    db.ingest_mail(
        mail_id=root.id,
        sender=root.sender,
        recipient=root.to,
        subject=root.subject,
        body=root.body,
        category="rfq",
        received_at=root.received_at,
    )
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *a: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *a: {})
    monkeypatch.setattr(rfq, "portal_result", lambda *a, **k: PortalResult([], [], {}))
    monkeypatch.setattr(rfq, "_sheets", lambda *a: options())
    monkeypatch.setattr(bids, "wait", lambda *a, **k: None)
    monkeypatch.setattr(rfq, "_quote_pdf", lambda *a, **k: None)
    monkeypatch.setattr("app.pricing.selection.pricing_day", lambda: TODAY)
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, price_adjust_pct=0))
    monkeypatch.setattr(
        rfq, "compose_quote_text", lambda r, o, *a: f"Checked quote for {o.airline}"
    )
    sent = []
    monkeypatch.setattr(
        rfq.hackloop,
        "send_customer",
        lambda to, subject, body, **k: (
            sent.append((body, k)) or {"id": 23592, "body": body, "subject": subject}
        ),
    )
    return root, sent


@pytest.mark.core
def test_no_feasible_option_asks_before_claim_and_never_quotes_late(flow, monkeypatch):
    root, sent = flow
    only_late = dataclasses.replace(options()[2], transit_days=30)
    monkeypatch.setattr(rfq, "_sheets", lambda *a: [only_late])
    monkeypatch.setattr(
        clarification, "settings", dataclasses.replace(clarification.settings, clarify_once=False)
    )
    rfq._price_and_quote(root, root.id, request(), "STR", time.time(), "enforce")
    session = db.get_session(root.id)
    assert not session["quotes"] and session["first_reply_at"] is None
    assert len(sent) == 1 and "latest arrival date" in sent[0][0]
    assert session["validation"]["state"] == "awaiting_clarification"
    assert session["validation"]["send_state"] == "sent"


@pytest.mark.core
def test_emergency_draft_uses_same_intent_and_deadline_gate(flow, monkeypatch):
    root, _ = flow
    db.set_request(root.id, request().model_dump())
    monkeypatch.setattr(drafts, "sheet_options", lambda *a: options())
    monkeypatch.setattr(drafts, "portal_result", lambda *a, **k: PortalResult([], [], {}))
    result = drafts.build(root.id)
    rfq._price_and_quote(root, root.id, request(), "STR", time.time(), "enforce")
    quote = db.get_session(root.id)["quotes"][0]
    assert quote["airline"] == result["choice"]["airline"] == "Solace Air Cargo"
    assert quote["total_minor"] == result["total_minor"] == 606000


@pytest.mark.core
def test_late_preferred_airline_cannot_bypass_arrival_filter(flow, monkeypatch):
    from app.agents import carrier_prefs

    root, sent = flow
    monkeypatch.setattr(carrier_prefs, "preferred", lambda *a: {"aurelia air cargo"})
    opts = [
        dataclasses.replace(o, transit_days=30) if o.airline == "Aurelia Air Cargo" else o
        for o in options()
    ]
    monkeypatch.setattr(rfq, "_sheets", lambda *a: opts)
    rfq._price_and_quote(root, root.id, request(), "STR", time.time(), "enforce")
    assert len(sent) == 1
    assert db.get_session(root.id)["quotes"][0]["airline"] == "Solace Air Cargo"


# ---------------------------------------------------------------- carrier preferences


@pytest.mark.parametrize(
    "text",
    [
        "We do not need a QA certificate.",
        "No CS required.",
        "Yes please book it. No need for HA paperwork.",
        "No CS, no QA certificate.",
    ],
)
def test_ordinary_abbreviations_never_exclude_a_carrier(text) -> None:
    from app.agents import carrier_prefs

    assert carrier_prefs.parse(text) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Please avoid Juniper.", [("Juniper Cargo Lines", "exclude")]),
        ("Please do not offer Zephyr Steppe Cargo (ZS).", [("Zephyr Steppe Cargo", "exclude")]),
        ("No ZS flights please.", [("Zephyr Steppe Cargo", "exclude")]),
        (
            "Please avoid ZS, MD or QA.",
            [
                ("Meridian Freightways", "exclude"),
                ("Quetzal Air Freight", "exclude"),
                ("Zephyr Steppe Cargo", "exclude"),
            ],
        ),
        ("We prefer airline FQ.", [("Fjordline Air Cargo", "prefer")]),
    ],
)
def test_named_or_coded_carriers_are_still_recognised(text, expected) -> None:
    from app.agents import carrier_prefs

    assert carrier_prefs.parse(text) == expected
