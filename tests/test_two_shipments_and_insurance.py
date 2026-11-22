"""Rounds 89/90 (rejected one_of_two) and 91 (rejected too_low): two shipments in one mail and
cargo insurance inside the all-in price. Real flow, stubbed mail/portal/gateway."""

from decimal import Decimal

import pytest

from app import db
from app.mail import shipments
from app.pricing import insurance
from tests.test_recent_rounds_e2e import World, _quote, build_world

CUSTOMER = "rheintal-elektronik@hackloop.mail"

ROUND_90 = (
    "Hello,\n\nCould you please quote the fastest air freight option for each of these two "
    "shipments? Both are ready for pickup on 2026-11-28 at our site in 20674 Hamburg, Germany. "
    "Please arrange the truck from our site to the airport.\n\n"
    "1. Destination airport RUH (Riyadh, SA): sports equipment, 6 pieces, gross weight 591 kg, "
    "volume 1.95 cbm.\n"
    "2. Destination airport GRU (Sao Paulo, BR): machine parts, 6 pieces, gross weight 1335 kg, "
    "volume 10.63 cbm.\n\n"
    "Please send both quotes in the same reply, with a separate all-in price in EUR for each "
    "shipment rather than a combined price.\n\nThank you.\n\n"
    "Viele Gruesse / Many greetings\nDeniz Yilmaz\nRheintal Elektronik GmbH"
)
ROUND_91_INSURANCE = (
    "Please include cargo insurance in the all-in price, on the terms of our frame agreement: "
    "goods value 22000 EUR, insured at 110 % of that value, premium rate 0.40 %."
)


# ---------------------------------------------------------------- parsers


def test_round_90_mail_splits_into_two_shipments() -> None:
    first, second = shipments.split(ROUND_90)
    assert (first.destination, first.weight_kg, first.volume_cbm, first.pieces) == (
        "RUH",
        591.0,
        1.95,
        6,
    )
    assert (second.destination, second.weight_kg, second.volume_cbm) == ("GRU", 1335.0, 10.63)
    assert "GRU" not in first.body and "1335" not in first.body
    assert "RUH" not in second.body and "20674 Hamburg" in second.body


@pytest.mark.parametrize(
    "body",
    [
        "Please quote 591 kg to RUH (Riyadh, SA), ready 2026-11-28.",  # one shipment
        "1. Please quote to RUH (Riyadh, SA).\n2. Confirm the transit time.",  # a to-do list
        "1. RUH (Riyadh, SA), 100 kg gross.\n2. RUH (Riyadh, SA), 200 kg gross.",  # lots
        "1. RUH (Riyadh, SA), 100 kg gross.\n3. GRU (Sao Paulo, BR), 200 kg gross.",
    ],
)
def test_other_mails_are_not_split(body: str) -> None:
    assert shipments.split(body) == []


def test_round_91_premium_is_value_times_coverage_times_rate() -> None:
    cover = insurance.read(ROUND_91_INSURANCE)
    assert cover is not None
    assert cover.premium_minor == 9680  # 22000 x 1.10 x 0.0040 = 96.80 EUR
    assert (cover.coverage_pct, cover.rate_pct) == (Decimal(110), Decimal("0.40"))


@pytest.mark.parametrize(
    ("text", "premium"),
    [
        ("Bitte Versicherung einrechnen: Warenwert 22.000,00 EUR, Prämiensatz 0,40 %.", 8800),
        ("Insurance please, goods value 10,000 EUR, premium rate 0.5 %.", 5000),
        ("Goods value 22000 EUR, premium rate 0.40 %.", None),  # nobody asked for insurance
        ("Please include insurance, premium rate 0.40 %.", None),  # no value
        ("Please include insurance for goods value 22000 EUR.", None),  # no rate
        ("Insurance: goods value 22000 EUR or goods value 30000 EUR, rate 0.4 %.", None),
    ],
)
def test_insurance_reads_only_complete_unambiguous_terms(text: str, premium: int | None) -> None:
    cover = insurance.read(text)
    assert (cover.premium_minor if cover else None) == premium


# ---------------------------------------------------------------- the real flow

SELF_DELIVERY = (
    "Good morning,\n\nwe need an all-in price in EUR for the cheapest air freight from "
    "Frankfurt (FRA) to New York (JFK). We deliver to FRA ourselves. 14 pieces, gross weight "
    "2644 kg, total volume 7.20 cbm, ready on 2026-11-26.{extra}\n\nKind regards,\nJonas Brandt"
)
JFK_FACTS = {
    "origin": "FRA",
    "pickup_zip": None,
    "destination": "JFK",
    "weight_kg": 2644.0,
    "volume_cbm": 7.2,
    "pieces": 14,
    "preference": "cheapest",
    "ready_date": "2026-11-26",
    "language": "en",
    "customer_name": "Jonas",
}
TWO_FROM_FRA = (
    "Good morning,\n\ncould you quote the cheapest air freight option for each of these two "
    "shipments from Frankfurt (FRA)? We deliver to FRA ourselves, both are ready on "
    "2026-11-26.\n\n"
    "1. Destination airport JFK (New York, US): pumps, 14 pieces, gross weight 2644 kg, "
    "volume 7.20 cbm.\n"
    "2. Destination airport RUH (Riyadh, SA): valves, 6 pieces, gross weight 591 kg, "
    "volume 1.95 cbm.\n\n"
    "Please send both quotes in the same reply, with a separate all-in price in EUR for each "
    "shipment rather than a combined price.\n\nKind regards,\nJonas Brandt"
)


@pytest.fixture
def w(tmp_path, monkeypatch) -> World:
    return build_world(tmp_path, monkeypatch)


def _reply(w: World, sid: int) -> str:
    quote = _quote(sid)
    assert quote is not None, [e["message"] for e in db.get_session(sid)["events"][-4:]]
    return next(m["body"] for m in w.to(CUSTOMER) if m["id"] == quote["mail_id"])


def test_requested_insurance_is_inside_the_one_all_in_total(w: World) -> None:
    w.extraction = JFK_FACTS
    w.rfq(70001, CUSTOMER, "FRA to JFK", SELF_DELIVERY.format(extra=""))
    plain = _quote(70001)["total_minor"]
    assert "insurance" not in _reply(w, 70001).lower()

    w.rfq(70002, CUSTOMER, "FRA to JFK", SELF_DELIVERY.format(extra=" " + ROUND_91_INSURANCE))
    assert _quote(70002)["total_minor"] == plain + 9680
    body = _reply(w, 70002)
    assert "The total includes the cargo insurance you asked for." in body
    assert body.count("€") == 1  # still one all-in total, the premium is not a second amount


def test_insurance_setting_off_keeps_the_old_total(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch, cargo_insurance=False)
    w.extraction = JFK_FACTS
    w.rfq(70003, CUSTOMER, "FRA to JFK", SELF_DELIVERY.format(extra=" " + ROUND_91_INSURANCE))
    w.rfq(70004, CUSTOMER, "FRA to JFK", SELF_DELIVERY.format(extra=""))
    assert _quote(70003)["total_minor"] == _quote(70004)["total_minor"]


def test_two_shipments_get_two_separate_totals_in_one_reply(w: World) -> None:
    from app.agents import rfq
    from app.models import Rfq

    w.extraction = JFK_FACTS
    w.rfq(70010, CUSTOMER, "Air freight quote request: JFK and RUH", TWO_FROM_FRA)

    first = _quote(70010)
    assert first is not None and first["destination"] == "JFK"
    assert len(w.to(CUSTOMER)) == 1  # one reply, never a second quote mail
    body = _reply(w, 70010)
    assert body.count("€") == 2
    assert "FRA to JFK" in body and "from FRA to RUH" in body
    assert "For your second shipment" in body and "separate all in total" in body

    # the second total is what a request for that shipment alone is priced at
    alone = Rfq.model_validate(
        {**JFK_FACTS, "destination": "RUH", "weight_kg": 591.0, "volume_cbm": 1.95, "pieces": 6}
    )
    expected = rfq.best_air(rfq._sheets(alone), [], "cheapest")
    extra = next(e for e in db.get_session(70010)["events"] if e["kind"] == "extra_shipment_quoted")
    assert extra["payload"]["total_minor"] == expected.total_eur_minor
    assert extra["payload"]["selected"]["destination"] == "RUH"
    assert rfq._amount(expected.total_eur_minor, "en") in body
    # the reply keeps the customer's subject instead of naming one of the two routes
    sent = next(m for m in w.to(CUSTOMER) if m["id"] == first["mail_id"])
    assert "JFK and RUH" in sent["subject"]


def test_two_shipments_setting_off_quotes_as_before(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch, multi_shipment_quote=False)
    w.extraction = JFK_FACTS
    w.rfq(70011, CUSTOMER, "Air freight quote request: JFK and RUH", TWO_FROM_FRA)
    sent = w.to(CUSTOMER)
    assert all("second shipment" not in m["body"] for m in sent)


def test_second_shipment_with_pickup_uses_this_mails_trucker_bid(w: World) -> None:
    from decimal import Decimal as D

    from app.agents import rfq
    from app.models import Rfq
    from tests.test_recent_rounds_e2e import ISARWELLE, TAUNUS

    door = TWO_FROM_FRA.replace(
        "from Frankfurt (FRA)? We deliver to FRA ourselves, both are",
        "? Please arrange the truck from our plant in 60311 Frankfurt am Main, both are",
    )
    facts = {**JFK_FACTS, "origin": None, "pickup_zip": "60311"}
    w.extraction = facts
    w.replies = {
        TAUNUS: "Our all-in price is EUR 180.00, pickup within 1 day.",
        ISARWELLE: "Our all-in price is EUR 160.00, pickup within 2 days.",
    }
    w.rfq(70020, CUSTOMER, "Air freight quote request: JFK and RUH", door)

    first = _quote(70020)
    assert first is not None and first["destination"] == "JFK"
    truck = D(next(a["total_eur"] for a in first["alternatives"] if "trucker" in a))
    alone = Rfq.model_validate(
        {**facts, "destination": "RUH", "weight_kg": 591.0, "volume_cbm": 1.95, "pieces": 6}
    )
    air = rfq.best_air(rfq._sheets(alone), [], "cheapest").total_eur_minor
    extra = next(e for e in db.get_session(70020)["events"] if e["kind"] == "extra_shipment_quoted")
    # 591 kg is more than four times lighter than the 2644 kg the trucker priced: too far to
    # scale, so the real bid is kept
    assert extra["payload"]["total_minor"] == air + int(truck * 100)
    assert extra["payload"]["pickup_included"] is True
    body = _reply(w, 70020)
    assert body.count("€") == 2 and "pickup from your site is included" in body


def test_pickup_for_a_comparable_weight_is_scaled_by_the_square_root() -> None:
    from decimal import Decimal as D

    from app.agents import bids, rfq
    from app.models import Rfq
    from app.pricing.reference import carriers

    trucker = next(c for c in carriers() if c.kind == "trucker")
    first = Rfq.model_validate({**JFK_FACTS, "weight_kg": 591.0})
    offer = bids.Offer(trucker, D("150.00"), 1.0, 1)
    # round 90: 591 kg priced by the trucker, the second shipment weighs 1335 kg
    expected = (D("150.00") * (D(1335) / D(591)).sqrt()).quantize(D("0.01"))
    assert rfq._extra_truck_minor(first, offer, 1335.0, 1) == int(expected * 100)
