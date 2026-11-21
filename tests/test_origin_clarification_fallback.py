"""Replay RFQ 5844 and the last missing-origin guard in both runtime modes."""

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from app import config, db
from app.agents import clarification, rfq
from app.mail.rfq_validation import explicit_delivery_origin, validate
from app.models import Mail, Rfq

BODY = (
    "Hi,\n\nCould you quote an all-in price in EUR for the cheapest air freight option to DEL "
    "(Delhi, IN)?\n\nWe’ll deliver the shipment to FRA ourselves. It’ll be ready on 2026-11-24: "
    "11 pieces of printed marketing material, 736 kg gross, 6.88 cbm.\n\nThanks\n\nSara\nLindqvist Medtech"
)


@pytest.fixture(autouse=True)
def no_live_rescue(monkeypatch):
    monkeypatch.setattr(
        clarification.llm,
        "complete",
        lambda *args, **kwargs: json.dumps({"corrections": []}),
    )


def setup_mail(tmp_path, monkeypatch, mode, body=BODY, subject="Air freight quote: FRA–DEL"):
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
        subject,
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
        clarification.hackloop,
        "send",
        lambda *a, **k: pytest.fail("FRA is already stated"),
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
            m("Where can you ship from?\nOn Monday wrote:\nWe deliver to FRA ourselves."),
            "DEL",
        )
        is None
    )
    assert explicit_delivery_origin(m("We deliver to FRA or HAM ourselves."), "DEL") is None
    assert explicit_delivery_origin(m("We deliver to FRA ourselves."), "FRA") is None


def test_session_12867_airport_name_and_code_never_asks(tmp_path, monkeypatch):
    body = (
        "Hello,\n\nCould you please quote the fastest air freight option to BOM (Mumbai, IN)? "
        "The shipment will be ready on 2026-11-23, and we will deliver it to Düsseldorf "
        "Airport (DUS) ourselves.\n\nThe cargo is 2 pieces, with a gross weight of 1676 kg "
        "and a total volume of 11.14 cbm."
    )
    m, request = setup_mail(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        mode="enforce",
        body=body,
        subject="Air freight quote DUS to BOM",
    )
    enabled = dataclasses.replace(rfq.settings, self_delivery_recovery=True)
    monkeypatch.setattr(config, "settings", enabled)
    monkeypatch.setattr(rfq, "settings", enabled)
    monkeypatch.setattr(clarification, "settings", enabled)
    request = request.model_copy(
        update={
            "origin": None,
            "destination": "BOM",
            "weight_kg": 1676,
            "volume_cbm": 11.14,
            "pieces": 2,
            "preference": "fastest",
        }
    )
    checked, draft = validate(m, request)
    assert checked is not None and checked.origin == "DUS" and not draft.issues


@pytest.mark.parametrize(
    "answer_text",
    [
        "The departure airport is DUS.",
        "The departure airport code is DUS.",
        "DUS",
    ],
)
def test_natural_origin_answer_resumes_without_second_question(tmp_path, monkeypatch, answer_text):
    body = "Please quote to BOM. Gross weight: 1676 kg. Volume: 11.14 cbm. Pieces: 2."
    m, request = setup_mail(tmp_path, monkeypatch, "enforce", body, "Quote to BOM")
    request = request.model_copy(
        update={
            "origin": None,
            "destination": "BOM",
            "weight_kg": 1676,
            "volume_cbm": 11.14,
        }
    )
    _, draft = validate(m, request)
    sent = []
    monkeypatch.setattr(
        clarification.hackloop,
        "send",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"id": 2},
    )
    clarification.request(m, m.id, draft)
    answer = Mail(
        3,
        m.id,
        m.sender,
        m.to,
        "Re: Quote to BOM",
        answer_text,
        datetime.now(UTC).isoformat(),
    )
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    assert clarification.handle_reply(answer, m.id)
    assert db.validation_get(m.id)["state"] == "ready"
    assert db.get_session(m.id)["request"]["origin"] == "DUS"
    assert len(sent) == 1 and len(resumed) == 1


def test_stronger_model_rescues_unusual_but_explicit_reply(tmp_path, monkeypatch):
    body = "Please quote to BOM. Gross weight: 1676 kg. Volume: 11.14 cbm. Pieces: 2."
    m, request = setup_mail(tmp_path, monkeypatch, "enforce", body, "Quote to BOM")
    request = request.model_copy(
        update={
            "origin": None,
            "destination": "BOM",
            "weight_kg": 1676,
            "volume_cbm": 11.14,
        }
    )
    _, draft = validate(m, request)
    sent = []
    monkeypatch.setattr(
        clarification.hackloop,
        "send",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"id": 2},
    )
    clarification.request(m, m.id, draft)
    answer_text = "Use the Düsseldorf departure aerodrome mentioned by our shipping team."
    answer = Mail(3, m.id, m.sender, m.to, "Re: Quote", answer_text, datetime.now(UTC).isoformat())
    calls = []

    def rescue(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps(
            {"corrections": [{"field": "origin", "value": "DUS", "evidence": answer_text}]}
        )

    monkeypatch.setattr(clarification, "free_text_answer", lambda *args, **kwargs: {})
    monkeypatch.setattr(clarification, "explicit_delivery_origin", lambda *args, **kwargs: None)
    monkeypatch.setattr(clarification.llm, "complete", rescue)
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    assert clarification.handle_reply(answer, m.id)
    assert db.validation_get(m.id)["state"] == "ready"
    assert db.get_session(m.id)["request"]["origin"] == "DUS"
    assert calls[0]["model_name"] == clarification.settings.llm_rescue_model
    assert len(sent) == 1 and len(resumed) == 1


def test_stronger_model_reads_original_before_first_question(tmp_path, monkeypatch):
    evidence = "We tender the cargo at the Düsseldorf departure aerodrome."
    body = f"Please quote to BOM. {evidence} Gross weight: 1676 kg. Volume: 11.14 cbm. Pieces: 2."
    m, request = setup_mail(tmp_path, monkeypatch, "enforce", body, "Quote to BOM")
    request = request.model_copy(
        update={
            "origin": None,
            "destination": "BOM",
            "weight_kg": 1676,
            "volume_cbm": 11.14,
        }
    )
    _, draft = validate(m, request)
    monkeypatch.setattr(
        clarification.llm,
        "complete",
        lambda *args, **kwargs: json.dumps(
            {"corrections": [{"field": "origin", "value": "DUS", "evidence": evidence}]}
        ),
    )
    monkeypatch.setattr(
        clarification.hackloop,
        "send",
        lambda *args, **kwargs: pytest.fail("rescue should avoid a clarification"),
    )
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    clarification.request(m, m.id, draft)
    assert db.validation_get(m.id)["state"] == "ready"
    assert db.get_session(m.id)["request"]["origin"] == "DUS"
    assert len(resumed) == 1


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_missing_origin_asks_and_reply_resumes_even_in_shadow(tmp_path, monkeypatch, mode):
    body = "Please quote to DEL. Gross weight: 736 kg. Volume: 6.88 cbm. Pieces: 11."
    m, _ = setup_mail(tmp_path, monkeypatch, mode, body, subject="Air freight quote to DEL")
    monkeypatch.setattr(
        rfq,
        "_bid_carriers",
        lambda *args: pytest.fail("Missing origin must ask before pricing"),
    )
    sent = []
    monkeypatch.setattr(
        clarification.hackloop,
        "send",
        lambda *a, **kw: sent.append((a, kw)) or {"id": 2},
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
