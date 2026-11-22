"""Round 91 to 93 (rejected too_low): cargo insurance inside the all-in price. Real flow,
stubbed mail, portal and gateway."""

from decimal import Decimal

import pytest

from app import db
from app.pricing import insurance
from tests.test_recent_rounds_e2e import World, _quote, build_world

CUSTOMER = "rheintal-elektronik@hackloop.mail"
ROUND_91_INSURANCE = (
    "Please include cargo insurance in the all-in price, on the terms of our frame agreement: "
    "goods value 22000 EUR, insured at 110 % of that value, premium rate 0.40 %."
)


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
