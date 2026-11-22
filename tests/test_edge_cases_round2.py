"""Predicted 5U escalations (edge-case review 22.11.): inch dimensions, per-kg carrier bids,
unthreaded carrier replies on a shared lane, booking approval as a new mail, forbidden
dangerous goods and ticket auto-replies. No real 5U calls."""

from decimal import Decimal

import pytest

from app import db
from app.agents import bids
from app.mail import lots
from app.mail import rfq_validation as validation

TEAM = "austrian-power@hackloop.mail"
CARRIER = "aurelia-air-cargo@hackloop.mail"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
    yield


# ------------------------------------------------------------- units (case 4)


def test_inch_dimensions_give_the_volume():
    text = "4 wooden crates, dimensions 48 x 40 x 50 inches each, total weight 1,320 lbs."
    facts = validation._unit_facts(text, {"weight_kg": 598.74})
    assert facts["volume_cbm"] == Decimal("6.2926")  # x 167 = 1051 kg chargeable
    assert abs(facts["weight_kg"] - Decimal("598.74")) < Decimal("0.01")


def test_in_as_a_word_is_not_an_inch():
    assert "volume_cbm" not in validation._unit_facts("3 pallets 120x80x100 in the hall, each", {})


def test_lot_lines_in_inches():
    totals = lots.parse_lots("Lot 1: 4 crates, 272 kg, 48 x 40 x 50 inches each")
    assert round(totals.volume_cbm, 4) == Decimal("6.2926")


# ------------------------------------------------------------- per-kg bids (case 4)


BID = (
    "Hi, we can do USD 1.60/kg airfreight + USD 0.55/kg fuel + USD 0.12/kg security, "
    "Min USD 140. Valid 48h."
)


def test_minimum_charge_is_never_the_total():
    assert bids.parse_price(BID) is None  # was (140, USD): the minimum read as the price


def test_per_kg_components_times_weight():
    assert bids.per_kg_total(BID, 1051) == (Decimal("2385.77"), "USD")


def test_minimum_applies_to_freight_only():
    text = "EUR 1.85/kg + EUR 0.45/kg FSC + EUR 0.15/kg SEC (min. EUR 120)"
    assert bids.per_kg_total(text, 40) == (Decimal("144.00"), "EUR")  # 120 + 0.60 x 40


def test_stated_total_still_wins():
    assert bids.parse_price("All-in EUR 2,000 incl. fuel EUR 0.45/kg") == (
        Decimal("2000.00"),
        "EUR",
    )


# ------------------------------------------------------------- unthreaded bids (case 3)


def _bid_request(mail_id: int, session: int, kg: str) -> None:
    db.ingest_mail(
        mail_id=session,
        sender="buyer@shipper.example",
        recipient=TEAM,
        subject=f"Air freight FRA to PVG {kg} kg",
        body="please quote",
        category="rfq",
    )
    db.record_outgoing(
        mail_id=mail_id,
        recipient=CARRIER,
        subject=f"[AP-{session}] Air freight FRA to PVG, {kg} kg / 1.2 cbm",
        body="Please quote",
        category="bid_request",
        session_id=session,
    )


def test_unthreaded_bid_goes_to_the_request_with_its_weight():
    _bid_request(11, 1, "180")
    _bid_request(21, 2, "1250")
    sid = db.ingest_mail(
        mail_id=30,
        sender=f"Rate Desk <{CARRIER}>",
        recipient=TEAM,
        subject="Rate Offer FRA - PVG",
        body="regarding your inquiry for FRA to PVG: EUR 1.85/kg for the 1,250 kg shipment",
        category="carrier_bid",
    )
    assert sid == 2


def test_unthreaded_bid_without_weight_on_a_shared_lane_is_not_guessed():
    _bid_request(11, 1, "180")
    _bid_request(21, 2, "1250")
    assert db.bid_request_for_reply(CARRIER, "Rate Offer FRA - PVG", "EUR 1.85/kg") is None


def test_unthreaded_bid_with_unique_lane_matches():
    _bid_request(11, 1, "180")
    assert db.bid_request_for_reply(CARRIER, "Offer FRA-PVG", "EUR 410 all-in") == (1, 11)


# ------------------------------------------------------------- approval as new mail (case 6)

MEIER = "Thomas Meier <einkauf@metallbau-meier.de>"


def _quoted(session: int, origin: str = "MUC", dest: str = "ICN", kg: float = 480) -> None:
    db.ingest_mail(
        mail_id=session,
        sender=MEIER,
        recipient=TEAM,
        subject=f"Anfrage {origin} {dest}",
        body=f"{kg:g} kg",
        category="rfq",
    )
    db.set_request(session, {"origin": origin, "destination": dest, "weight_kg": kg})
    db.record_quote(
        session,
        total_minor=150000,
        currency="EUR",
        airline="X",
        origin=origin,
        destination=dest,
        transit_days=2,
        source="portal",
        rate_id="R-100",
    )
    db.set_status(session, "quoted")


APPROVAL = (
    "Buchungsfreigabe MUC - ICN (480 kg)",
    (
        "Hallo Team, wir haben das intern freigegeben. Bitte bucht die Sendung ab 80939 "
        "München nach Seoul ICN (480 kg, wie heute Vormittag angeboten) fest ein."
    ),
)


def test_new_mail_approval_finds_the_quote():
    _quoted(5)
    assert db.session_for_unthreaded_acceptance(MEIER, *APPROVAL) == 5


def test_new_mail_with_other_weight_stays_a_request():
    _quoted(5)
    subject, body = APPROVAL
    assert db.session_for_unthreaded_acceptance(MEIER, subject, body.replace("480", "900")) is None


def test_new_request_without_booking_words_stays_a_request():
    _quoted(5)
    assert db.session_for_unthreaded_acceptance(MEIER, "MUC - ICN", "please quote 480 kg") is None


def test_two_open_quotes_on_other_routes_pick_the_named_one():
    _quoted(5)
    _quoted(6, "FRA", "JFK", 300)
    assert db.session_for_unthreaded_acceptance(MEIER, *APPROVAL) == 5


def test_other_sender_never_matches():
    _quoted(5)
    assert db.session_for_unthreaded_acceptance("x@other.example", *APPROVAL) is None


# ------------------------------------------------------------- forbidden cargo (case 7)


def test_damaged_lithium_batteries_are_refused():
    from app.mail import compliance

    refusal = compliance.check(
        "Content: Defective / swollen Lithium-Ion battery packs (UN3480) returned for lab "
        "failure analysis."
    )
    assert refusal is not None and refusal.damaged_lithium
    text = compliance.decline_text(refusal, "Lukas", "en", "Your austrian Power Team")
    assert "A154" in text and "Shipper's Declaration" in text


def test_ordinary_lithium_still_gets_the_dg_question_not_a_refusal():
    from app.mail import compliance

    assert compliance.check("UN3481 batteries packed with equipment, new and not damaged.") is None
    assert compliance.check("UN3481 in equipment. Please add insurance for damaged goods.") is None


def test_sanctioned_transit_is_refused_but_led_is_not_a_place():
    from app.mail import compliance

    refusal = compliance.check("Dubai DXB, or onward transit to Tehran IKA if possible")
    assert refusal is not None and refusal.sanctioned == ("Iran",)
    assert compliance.check("3 pallets of LED lamps to JFK") is None


def test_sea_freight_is_declined_with_an_air_offer():
    """Session 53348: port to port DEHAM to AEJEA, FCL 1 x 40ft. No airport question and no
    rescue loop: one polite answer that we only fly, in the customer's language."""
    from app.mail import compliance

    body = (
        "Could you send me an all-in price in EUR for a port-to-port sea freight shipment from "
        "Hamburg (DEHAM) to Jebel Ali (AEJEA)? It's an FCL shipment in 1 x 40ft container, with "
        "6 pieces, a gross weight of 9617 kg and a total volume of 43.5 cbm."
    )
    assert compliance.sea_freight("Sea freight quote: Hamburg to Jebel Ali", body)
    assert compliance.sea_freight("Angebot", "Seefracht FCL 1 x 40 Fuß ab Hamburg nach Jebel Ali")
    refusal = compliance.Refusal(False, (), sea_freight=True)
    en = compliance.decline_text(refusal, "Klaus", "en", "Your austrian Power Team")
    de = compliance.decline_text(refusal, "Klaus", "de", "Your austrian Power Team")
    assert "air freight" in en and "reply to this email" in en
    assert "Luftfracht" in de and "Seefracht" in de
    for text in (en, de):
        assert not any(ch in text for ch in ":;()") and "—" not in text and " – " not in text


@pytest.mark.parametrize(
    "subject,body",
    [
        ("RFQ", "Air freight FRA to JFK, 300 kg. We usually ship by sea but this one is urgent."),
        ("Anfrage", "Seefracht ist zu langsam, bitte Luftfracht MUC nach PVG, 200 kg."),
        (
            "RFQ",
            "2 pallets, 400 kg, 1.2 cbm, pressure vessels, from 80331 to the port of Singapore.",
        ),
        ("RFQ", "3 crates, 120 kg, container parts, FRA to DXB, fastest option."),
    ],
)
def test_air_requests_mentioning_sea_or_ports_are_still_quoted(subject, body):
    from app.mail import compliance

    assert compliance.sea_freight(subject, body) is None


# ------------------------------------------------------------- ticket auto-reply (case 8)


def test_ticket_auto_reply_is_held_without_a_model_call():
    from app.mail import guard
    from app.models import Category, Mail

    body = (
        "Thank you for your message. Your request has been logged under Ticket #99412. "
        "An agent will review your email shortly. Please do not reply to this automated message."
    )
    mail = Mail(1, 1, "support-desk@customer-logistics.com", TEAM, "Re: Quote [#99412]", body, "")
    assert guard.precheck(mail, Category.CUSTOMER)
    real = Mail(2, 2, "buyer@x.example", TEAM, "Re: Quote", "Please book it, ticket 4 is ours", "")
    assert guard.precheck(real, Category.CUSTOMER) is None


# ------------------------------------------------------------- non-stackable (case 1)


def test_non_stackable_is_told_to_carriers_price_formula_unchanged():
    from app.models import Rfq
    from app.pricing.pricing import chargeable_kg

    rfq = Rfq(
        origin="MUC",
        destination="ORD",
        weight_kg=390,
        volume_cbm=2.16,
        pieces=3,
        service_constraints=["Cargo is strictly NON-STACKABLE (no top-load allowed)"],
    )
    assert bids.non_stackable(rfq)
    assert "non-stackable" in bids._body(rfq, "MUC", "airline")
    assert chargeable_kg(390, 2.16) == Decimal(390)  # every rate sheet: max(kg, cbm x 167)
    plain = rfq.model_copy(update={"service_constraints": []})
    assert "stackable" not in bids._body(plain, "MUC", "airline")


def test_new_quote_request_that_mentions_booking_stays_a_request():
    _quoted(5)
    body = "Please quote MUC - ICN 480 kg, we would book quickly."
    assert db.session_for_unthreaded_acceptance(MEIER, "New RFQ MUC - ICN", body) is None


# ------------------------------------------------------------- requested currency (round 3)


def test_price_also_in_the_currency_the_customer_asked_for(monkeypatch):
    import dataclasses

    from app.agents import rfq
    from app.mail.style import SIGNATURE
    from app.models import Mail
    from app.pricing import fx

    monkeypatch.setattr(
        fx,
        "settings",
        dataclasses.replace(
            fx.settings, fx_eur_rates="USD=0.92", fx_valid_until="2026-12-31T00:00:00+00:00"
        ),
    )
    body = (
        "Hi, please quote FRA-MIA, 410 kg. Our accounting can only process USD quotes, "
        "please state the all-in price in USD. Thanks, Mike"
    )
    mail = Mail(1, 1, "mike@x.example", TEAM, "Quote FRA MIA", body, "")
    quote = f"Hi Mike,\n\nthe all-in price is 1.230 €.\n\n{SIGNATURE}\n"
    out = rfq.with_requested_currency(quote, mail, 123000, "en", 1)
    assert "1.336,96 USD" in out and out.index("USD") < out.index(SIGNATURE)

    assert rfq.requested_currency("cargo value in USD 10,000 for insurance") is None
    assert rfq.requested_currency("please quote in EUR") is None


# ------------------------------------------------------------- dates, weights, height (round 3)


def test_ambiguous_slash_date_follows_the_sender():
    received = "2026-11-22T02:00:00+00:00"
    us = "Hi team, pickup in 60549 Frankfurt, goods ready 11/12, 3 crates.\n– Jennifer, Atlanta"
    de = "Hallo, die Ware ist ab 11/12 abholbereit.\nGruß Jan, Hamburg"
    assert validation.ambiguous_ready_date(us, received) == "2026-11-12"
    assert validation.ambiguous_ready_date(de, received) == "2026-12-11"
    assert validation.ambiguous_ready_date("ready 25/11", received) is None  # not ambiguous
    assert validation.ambiguous_ready_date("1/2 of the cargo is ready", received) is None


def test_weight_per_piece_is_multiplied():
    text = "Bonjour, nous avons 5 palettes de 230 kg, 120x80x120 cm chacune, départ FRA."
    facts = validation._unit_facts(text, {})
    assert facts["weight_kg"] == Decimal(1150) and facts["volume_cbm"] == Decimal("5.7600")
    assert "weight_kg" not in validation._unit_facts("5 palettes, 230 kg au total", {})
    assert "weight_kg" not in validation._unit_facts("3 crates of 120 kg total", {})


def test_piece_taller_than_a_lower_deck_is_found():
    assert validation.tallest_piece_cm("1 Palette, 120 x 100 x 185 cm, 640 kg") == Decimal(185)
    assert validation.tallest_piece_cm("2 pallets 120x80x150 cm") <= validation.LOWER_DECK_HEIGHT_CM
