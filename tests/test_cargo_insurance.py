"""Real failures 26566/26764: required cargo insurance vanished from all-in totals."""

import base64
import dataclasses
import io
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal

import pdfplumber
import pytest
from pydantic import ValidationError

from app import db
from app.agents import bids, booking, clarification, rfq
from app.mail.outgoing import customer_body, validate_customer_text
from app.mail.rfq_validation import validate
from app.mail.style import SIGNATURE, amount
from app.models import CargoInsurance, Mail, RateOption, Rfq, RfqDraft
from app.pricing import insurance
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

POLICY = CargoInsurance(goods_value="22000", currency="EUR", coverage_pct="110", premium_pct="0.40")


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


@pytest.mark.parametrize(("body", "expected"), [(BODY_26566, 9680), (BODY_26764, 26730)])
def test_real_agreed_premiums(body, expected):
    policy, problems = insurance.read([body])
    assert not problems and policy is not None
    assert insurance.premium(policy).eur_minor == expected


@pytest.mark.parametrize(
    "terms",
    [
        "goods value EUR 22,000.00, coverage 110%, premium rate 0.40%",
        "Warenwert 22.000,00 EUR, versichert zu 110 %, Prämiensatz 0,40 %",
        "goods value 22 000 EUR, insured for 110%, premium .40%",
    ],
)
def test_currency_and_locale_numbers(terms):
    policy, problems = insurance.read(["Cargo insurance " + terms])
    assert not problems
    assert insurance.premium(policy).eur_minor == 9680


def test_minimum_and_explicit_fx(monkeypatch):
    policy, problems = insurance.read(
        [
            "Cargo insurance goods value USD 1000, cover 110%, premium rate 0.300%, minimum premium USD 20"
        ]
    )
    assert not problems
    monkeypatch.setattr(insurance, "eur_rate", lambda c: (Decimal("0.9"), "test-rate", None))
    premium = insurance.premium(policy)
    assert (premium.eur_minor, premium.original_amount, premium.currency) == (1800, "20.00", "USD")
    assert premium.fx_rate == "0.9" and premium.fx_source == "test-rate"


@pytest.mark.parametrize("terms", ["premium rate -0.4%", "minimum premium applies", "cover 0%"])
def test_unusable_terms_never_get_a_default(terms):
    policy, problems = insurance.read(["Cargo insurance goods value EUR 22000, " + terms])
    assert problems
    with pytest.raises(ValueError):
        insurance.premium(policy)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_policy_rejects_invalid_money(value):
    with pytest.raises(ValidationError):
        POLICY.model_copy().model_validate({**POLICY.model_dump(), "goods_value": value})


def test_zero_rate_is_supported_only_when_explicit():
    assert insurance.premium(POLICY.model_copy(update={"premium_pct": "0"})).eur_minor == 0
    with pytest.raises(ValueError):
        insurance.premium(POLICY.model_copy(update={"premium_pct": None}))


def test_conflicting_document_terms_remain_unresolved():
    policy, problems = insurance.read([BODY_26566, "Insurance premium rate 0.30%"])
    assert "conflicting premium_pct" in problems
    assert policy.premium_pct is None
    _, draft = validate(mail(), request(), {"agreement.txt": "Insurance premium rate 0.30%"})
    assert "insurance" in {i["field"] for i in draft.issues}


def test_model_cannot_invent_missing_terms():
    m = mail("Please include cargo insurance. DUS to MEX, 176 kg, 1.26 cbm, self delivery.")
    _, draft = validate(m, request().model_copy(update={"insurance": POLICY}))
    assert draft.facts["insurance"]["premium_pct"] is None
    assert any(i["field"] == "insurance" for i in draft.issues)


def test_no_request_and_customer_decline_do_not_add_insurance():
    assert insurance.read(["DUS to MEX, 176 kg, 1.26 cbm"])[0] is None
    assert insurance.read(["No cargo insurance needed.", BODY_26566])[0] is None
    assert insurance.requested("Do not quote without cargo insurance")


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


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize(
    ("mid", "body", "air", "truck", "premium", "total"),
    [
        (26566, BODY_26566, 92795, 0, 9680, 102475),
        (26764, BODY_26764, 304950, 25238, 26730, 356918),
    ],
)
def test_real_threads_send_and_persist_complete_total(
    monkeypatch, mid, body, air, truck, premium, total, forced
):
    m, req = mail(body + "\nPlease send the offer as PDF.", mid), request(mid)
    install_prices(monkeypatch, req, air, truck)
    monkeypatch.setattr(rfq, "FORCED_FULL_COMPOSE_S", 1000)
    sent = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"id": 50000}

    monkeypatch.setattr(rfq.hackloop, "send", send)
    rfq._price_and_quote(m, mid, req, req.origin, time.time(), "shadow", forced)
    [wire] = sent
    assert amount(total) in wire["body"] and wire["body"].count("€") == 1
    assert "Cargo insurance on your agreed terms is included in this total." in wire["body"]
    assert "unavailable" not in wire["body"] and wire["in_reply_to"] == mid
    [quote] = db.get_session(mid)["quotes"]
    assert (quote["total_minor"], quote["insurance_minor"], quote["air_total_minor"]) == (
        total,
        premium,
        air,
    )
    assert booking._quoted_air(quote) == (air, "EUR")
    [attachment] = wire["attachments"]
    with pdfplumber.open(io.BytesIO(base64.b64decode(attachment["content_base64"]))) as pdf:
        text = pdf.pages[0].extract_text()
    assert amount(total) in text and "insurance" in text
    assert not db.claim_quote_send(mid)


@pytest.mark.parametrize("forced", [False, True])
def test_missing_terms_stop_before_portal_or_carriers_even_in_shadow(monkeypatch, forced):
    m = mail("DUS to MEX, self delivery, 176 kg, 1.26 cbm. Please include cargo insurance.")
    asked = []
    monkeypatch.setattr(clarification, "request", lambda m, sid, draft: asked.append(draft))
    monkeypatch.setattr(
        rfq, "portal_result", lambda *a, **kw: pytest.fail("unpriced service reached portal")
    )
    monkeypatch.setattr(
        bids, "request", lambda *a, **kw: pytest.fail("unpriced service reached carriers")
    )
    assert rfq._price(m, m.id, request(), "DUS", time.time(), "shadow", forced) is None
    assert len(asked) == 1 and asked[0].issues[0]["field"] == "insurance"


def test_commercial_discount_does_not_reduce_agreed_premium(monkeypatch):
    m, req = mail(), request()
    install_prices(monkeypatch, req, 92795)
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, price_adjust_pct=10))
    priced = rfq._price(m, m.id, req, "DUS", time.time(), "enforce", True)
    assert priced.total == round(92795 * 0.9) + 9680


def test_partial_insurance_clarification_accumulates_and_can_be_declined(monkeypatch):
    root = mail(
        "DUS to MEX, self delivery, 176 kg gross, 1.26 cbm. Please include cargo insurance."
    )
    _, draft = validate(root, request())
    draft.evidence["attachment_texts"] = {}
    reply = dataclasses.replace(root, id=30001, body="Goods value EUR 22000, cover 110%.")
    checked, draft = clarification.answer_draft(reply, root.id, root, draft)
    assert draft.issues and draft.facts["insurance"]["goods_value"] == "22000"
    assert draft.facts["insurance"]["premium_pct"] is None
    reply = dataclasses.replace(reply, id=30002, body="The agreed premium rate is 0.40%.")
    checked, ready = clarification.answer_draft(reply, root.id, root, draft)
    assert checked is not None and insurance.premium(checked.insurance).eur_minor == 9680
    reply = dataclasses.replace(reply, id=30003, body="No cargo insurance needed.")
    checked, _ = clarification.answer_draft(reply, root.id, root, draft)
    assert checked is not None and checked.insurance is None
    db.validation_save(root.id, "ready", ready.model_dump(mode="json"), dataclasses.asdict(root))
    req, problems = rfq._insurance_terms(root, root.id, request())
    assert not problems and insurance.premium(req.insurance).eur_minor == 9680


@pytest.mark.parametrize("language", ["en", "de", "fr", "es", "it"])
def test_native_templates_confirm_insurance_with_one_total(language):
    req = request().model_copy(update={"insurance": POLICY, "language": language})
    option = RateOption("Test Air", "DUS", "MEX", 2, 92795, "EUR", 92795, "portal")
    body = rfq.quote_text(req, option, 102475, None, 0)
    assert insurance.notice(POLICY, language) in body and body.count("€") == 1
    validate_customer_text(customer_body(body, language))


def test_contradictory_composer_is_rejected_even_with_notice_present():
    bad = f"Hello,\n\nInsurance is not included. {insurance.notice(POLICY, 'en')}\n\n{SIGNATURE}"
    with pytest.raises(ValueError, match="unavailable or extra"):
        insurance.with_notice(bad, POLICY, "en")


def test_bad_composer_cannot_send_unavailable_insurance(monkeypatch):
    m, req = mail(), request()
    install_prices(monkeypatch, req, 92795)
    monkeypatch.setattr(
        rfq, "compose_quote_text", lambda *a: f"Insurance is not available.\n\n{SIGNATURE}"
    )
    sent = []
    monkeypatch.setattr(rfq.hackloop, "send", lambda **kw: sent.append(kw) or {"id": 50001})
    rfq._price_and_quote(m, m.id, req, "DUS", time.time(), "enforce")
    assert len(sent) == 1 and "not available" not in sent[0]["body"]
    assert amount(102475) in sent[0]["body"]


def test_alternative_keeps_insurance_out_of_airline_price():
    quote = {
        "currency": "EUR",
        "airline": "A",
        "total_minor": 120000,
        "insurance_minor": 9680,
        "alternatives": [
            {"airline": "B", "total_eur_minor": 80000, "transit_days": 3},
            {"trucker": "T", "total_eur": "100"},
        ],
    }
    assert booking._alternative(quote, 120000)["total_minor"] == 99680
    assert booking._quoted_air(quote) is None


def test_shared_value_is_not_duplicated_for_multiple_shipments(monkeypatch):
    m, req = mail(), request()
    extra = req.model_copy(update={"destination": "GRU"})
    asked = []
    monkeypatch.setattr(clarification, "request", lambda m, sid, draft: asked.append(draft))
    monkeypatch.setattr(rfq, "_price", lambda *a, **kw: pytest.fail("ambiguous insurance priced"))
    rfq._price_and_quote_multi(m, m.id, req, "DUS", [extra], time.time(), "enforce")
    assert asked[0].issues[0]["field"] == "insurance_allocation"


def test_unpriced_insurance_cannot_use_clarification_fallback(monkeypatch):
    m = mail()
    draft = RfqDraft(
        facts=request().model_dump(), issues=[{"field": "insurance", "code": "missing"}]
    )
    db.validation_save(m.id, "awaiting_clarification", draft.model_dump(), dataclasses.asdict(m))
    monkeypatch.setattr(
        rfq, "_price_and_quote", lambda *a, **kw: pytest.fail("unpriced insurance fallback")
    )
    assert not rfq.quote_without_answer(m.id, "no answer")


def test_legacy_database_migrates_without_changing_quotes(tmp_path):
    path = str(tmp_path / "legacy.db")
    schema = db.SCHEMA.replace(
        "  insurance_minor   INTEGER NOT NULL DEFAULT 0 CHECK (insurance_minor >= 0),\n", ""
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        connection.execute(
            "INSERT INTO sessions(id, customer, created_at, updated_at) VALUES (1, 'test', ?, ?)",
            (db.now(), db.now()),
        )
        connection.execute(
            "INSERT INTO quotes(session_id,total_minor,currency,sent_at) VALUES (1,92795,'EUR',?)",
            (db.now(),),
        )
    db.init_db(path)
    [quote] = db.get_session(1)["quotes"]
    assert quote["total_minor"] == 92795 and quote["insurance_minor"] == 0


def test_invalid_correction_does_not_reuse_previous_premium():
    policy, problems = insurance.read(["Premium rate -0.40%"], POLICY, requested=True)
    assert problems and policy.premium_pct is None


def test_full_rfq_flow_reads_insurance_after_polite_closing(monkeypatch):
    m, req = mail(BODY_26764, 26764), request(26764)
    install_prices(monkeypatch, req, 304950, 25238)
    monkeypatch.setattr(rfq, "extract_rfq", lambda *a: req)
    sent = []
    monkeypatch.setattr(rfq.hackloop, "send", lambda **kw: sent.append(kw) or {"id": 50002})
    rfq.handle_rfq(m, m.id)
    assert len(sent) == 1 and amount(356918) in sent[0]["body"]


def test_expired_insurance_fx_holds_quote_without_looping_clarification(monkeypatch):
    m = mail(BODY_26566.replace("22000 EUR", "22000 USD"))
    monkeypatch.setattr(
        insurance, "eur_rate", lambda *a: (_ for _ in ()).throw(ValueError("expired FX"))
    )
    monkeypatch.setattr(
        clarification, "request", lambda *a: pytest.fail("FX is not a customer question")
    )
    monkeypatch.setattr(
        rfq, "portal_result", lambda *a, **kw: pytest.fail("invalid FX reached portal")
    )
    assert rfq._price(m, m.id, request(), "DUS", time.time(), "enforce", True) is None
    assert db.get_session(m.id)["status"] == "error"
