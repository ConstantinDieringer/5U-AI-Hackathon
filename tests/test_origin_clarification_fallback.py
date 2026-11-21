"""Replay RFQ 5844 and the last missing-origin guard in both runtime modes."""

import dataclasses
from datetime import UTC, datetime

import pytest

from app import db
from app.agents import clarification, rfq
from app.mail.rfq_validation import explicit_delivery_origin, validate
from app.models import Mail, Rfq

BODY = (
    "Hi,\n\nCould you quote an all-in price in EUR for the cheapest air freight option to DEL "
    "(Delhi, IN)?\n\nWe’ll deliver the shipment to FRA ourselves. It’ll be ready on 2026-11-24: "
    "11 pieces of printed marketing material, 736 kg gross, 6.88 cbm.\n\nThanks\n\nSara\nLindqvist Medtech"
)


def setup_mail(tmp_path, monkeypatch, mode, body=BODY):
    db.init_db(str(tmp_path / "origin.db"))
    config = dataclasses.replace(rfq.settings, rfq_validation_mode=mode)
    monkeypatch.setattr(rfq, "settings", config)
    monkeypatch.setattr(clarification, "settings", config)
    ts = datetime.now(UTC).isoformat()
    m = Mail(
        1,
        1,
        "lindqvist-medtech@hackloop.mail",
        "austrian-power@hackloop.mail",
        "Air freight quote: FRA–DEL",
        body,
        ts,
    )
    db.ingest_mail(
        mail_id=1,
        sender=m.sender,
        recipient=m.to,
        subject=m.subject,
        body=m.body,
        category="rfq",
        received_at=ts,
    )
    request = Rfq(origin=None, destination="DEL", weight_kg=736, volume_cbm=6.88, pieces=11)
    db.set_request(1, request.model_dump())
    return m, request


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_mail_5844_reaches_pricing_with_fra(tmp_path, monkeypatch, mode):
    m, request = setup_mail(tmp_path, monkeypatch, mode)
    checked, draft = validate(m, request)
    assert checked.origin == "FRA" and not draft.issues
    reached = []

    def stop_at_pricing(request, origin):
        reached.append(origin)
        raise RuntimeError("pricing reached")

    monkeypatch.setattr(rfq, "_bid_carriers", stop_at_pricing)
    monkeypatch.setattr(
        clarification.hackloop, "send", lambda *a, **k: pytest.fail("FRA is already stated")
    )
    with pytest.raises(RuntimeError, match="pricing reached"):
        rfq._handle(m, 1)
    assert reached == ["FRA"]
    assert db.get_session(1)["request"]["origin"] == "FRA"


def test_no_historical_or_ambiguous_airport_is_invented():
    def m(body):
        return Mail(1, 1, "x", "y", "FRA–DEL old subject", body, "")

    assert (
        explicit_delivery_origin(
            m("Where can you ship from?\nOn Monday wrote:\nWe deliver to FRA ourselves."), "DEL"
        )
        is None
    )
    assert explicit_delivery_origin(m("We deliver to FRA or HAM ourselves."), "DEL") is None
    assert explicit_delivery_origin(m("We deliver to FRA ourselves."), "FRA") is None


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_missing_origin_asks_and_reply_resumes_even_in_shadow(tmp_path, monkeypatch, mode):
    body = "Please quote to DEL. Gross weight: 736 kg. Volume: 6.88 cbm. Pieces: 11."
    m, _ = setup_mail(tmp_path, monkeypatch, mode, body)
    sent = []
    monkeypatch.setattr(
        clarification.hackloop, "send", lambda *a, **kw: sent.append((a, kw)) or {"id": 2}
    )
    rfq._handle(m, 1)
    assert len(sent) == 1 and "departure airport" in sent[0][0][2]
    assert db.get_session(1)["status"] != "error"
    assert db.get_session(1)["first_reply_at"] is None
    answer = Mail(3, 1, m.sender, m.to, "Re: RFQ", "Origin: FRA", datetime.now(UTC).isoformat())
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    clarification.handle_reply(answer, 1)
    assert len(resumed) == 1 and db.get_session(1)["request"]["origin"] == "FRA"
