"""Exclusions stay consistent across selection, customer copy and booking."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app import db
from app.agents import bids, carrier_prefs, rfq
from app.models import Mail, RateOption, Rfq
from app.pricing.lanes import LaneAirline
from app.pricing.portal_sync import PortalResult
from app.pricing.reference import Carrier


@pytest.fixture(autouse=True)
def database(tmp_path):
    db.init_db(str(tmp_path / "exclusions.db"))


def test_selected_carrier_remains_clear_in_customer_copy():
    request = Rfq(
        origin="FRA", destination="DEL", weight_kg=100, excluded_airlines=["cobalt skylift"]
    )
    option = RateOption("Allowed Air", "FRA", "DEL", 2, 10000, "EUR", 10000, "sheet")
    body = "Allowed Air FRA to DEL: 100 € all-in. We will not offer Cobalt Skylift.\n\nYour austrian Power Team"
    data = {
        "body": body,
        "answered_questions": [],
        "unavailable_questions": [],
        "addressed_constraints": [],
        "unavailable_constraints": [],
    }
    with pytest.raises(ValueError, match="without repeating excluded names"):
        rfq._validate_composed(json.dumps(data), request, option, "100 €")
    data["body"] = body.replace(
        "We will not offer Cobalt Skylift.", "Your carrier exclusions have been applied."
    )
    assert rfq._validate_composed(json.dumps(data), request, option, "100 €") == data["body"]
    blocked = RateOption("Cobalt Skylift", "FRA", "DEL", 2, 10000, "EUR", 10000, "sheet")
    with pytest.raises(ValueError, match="excluded"):
        rfq.quote_text(request, blocked, 10000, None, 1)


def test_bid_fanout_skips_excluded_carriers_before_cap(monkeypatch):
    request = Rfq(origin="FRA", destination="DEL", weight_kg=100, excluded_airlines=["blocked air"])
    monkeypatch.setattr(
        rfq,
        "airlines_for",
        lambda *a: [
            LaneAirline("Blocked Air", "email_only", email="b@example.com"),
            LaneAirline("Allowed Air", "email_only", email="a@example.com"),
        ],
    )
    monkeypatch.setattr(rfq.served_lanes, "serves", lambda *a: None)
    monkeypatch.setattr(
        rfq,
        "carrier_by_email",
        lambda email: Carrier(
            "Blocked Air" if email.startswith("b") else "Allowed Air", "airline", email, ("DEL",)
        ),
    )
    _, airlines = rfq._bid_carriers(request, "FRA")
    assert [c.name for c in airlines] == ["Allowed Air"]


def test_all_price_sources_are_filtered_before_selection(monkeypatch):
    body = "Please do not offer Cobalt Skylift for any future inquiries. Quote FRA to DEL."
    stamp = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=100,
        sender="one@example.com",
        recipient="team@example.com",
        subject="Quote",
        body=body,
        category="rfq",
        received_at=stamp,
    )
    mail = Mail(100, 100, "one@example.com", "team@example.com", "Quote", body, stamp)
    request = Rfq(origin="FRA", destination="DEL", weight_kg=100)

    def option(name, cents, source):
        return RateOption(name, "FRA", "DEL", 2, cents, "EUR", cents, source)

    monkeypatch.setattr(rfq, "_bid_carriers", lambda *a: ([], []))
    monkeypatch.setattr(rfq, "airlines_for", lambda *a: [])
    monkeypatch.setattr(
        rfq,
        "sheet_options",
        lambda *a: [option("Cobalt Skylift", 100, "sheet"), option("Sheet Air", 22000, "sheet")],
    )
    monkeypatch.setattr(
        rfq,
        "portal_result",
        lambda *a, **kw: PortalResult(
            [option("Cobalt Skylift", 100, "portal"), option("Portal Air", 20000, "portal")]
        ),
    )
    offers = [
        bids.Offer(Carrier(n, "airline", n + "@example.com", ("DEL",)), Decimal(p), 2, i)
        for i, (n, p) in enumerate([("Cobalt Skylift", 1), ("Bid Air", 300)], 1)
    ]
    monkeypatch.setattr(bids, "offers", lambda *a: offers)
    monkeypatch.setattr(bids, "request", lambda *a: None)
    monkeypatch.setattr(bids, "wait", lambda *a, **kw: None)
    monkeypatch.setattr(
        rfq, "compose_quote_text", lambda r, o, total, t, sid: rfq.quote_text(r, o, total, t, sid)
    )
    sent = []
    monkeypatch.setattr(rfq.hackloop, "send", lambda *a, **kw: sent.append(kw) or {"id": 101})
    rfq._price_and_quote(mail, 100, request, "FRA", time.time(), "enforce")
    quote = db.get_session(100)["quotes"][0]
    assert quote["airline"] == "Portal Air" and quote["total_minor"] == 20000
    assert len(sent) == 1 and "Cobalt" not in sent[0]["body"]
    db.init_db()
    assert carrier_prefs.excluded("one@example.com") == {"cobalt skylift"}
    assert not carrier_prefs.excluded("other@example.com")
