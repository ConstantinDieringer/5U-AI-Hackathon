"""Edge cases that have not arrived yet (Task 3). Each case pins what the system does today
with every setting at its default; where a setting changes it, the test next to it shows the
switched-on behaviour. Real RFQ flow, stubbed mail, portal and gateway (see build_world)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app import db
from app.agents import booking, tariff
from app.mail.rfq_validation import validate
from app.mail.style import has_customer_presentation_issue
from app.models import Mail, Rfq
from app.pricing import pricing
from app.pricing.tariff_parser import TariffDocument, TariffLane
from tests.test_recent_rounds_e2e import (
    BIDS_42,
    HIMALAYA,
    LAGOON,
    PAMPERO,
    ROUND_42_FACTS,
    TEAM,
    ZEPHYR,
    World,
    _quote,
    build_world,
)

CUSTOMER = "hofmann-technik@hackloop.mail"


def _uln(w: World, mid: int, wish: str, **facts: Any) -> dict[str, Any] | None:
    """A door-to-port RFQ to ULN (email-only airlines, the four real bids of round 42)."""
    w.extraction = {**ROUND_42_FACTS, **facts}
    w.replies = dict(BIDS_42)
    body = (
        "Hello,\n\nplease quote air freight from 60311 Frankfurt am Main to Ulaanbaatar (ULN). "
        f"6 pieces, 640 kg gross, 3.10 cbm, ready on 2026-11-26.\n\n{wish}\n\n"
        "Best regards,\nPetra Hofmann"
    )
    w.rfq(mid, CUSTOMER, "Air freight to Ulaanbaatar", body)
    return _quote(mid)


# ---------------------------------------------------------------- 1. preferred airline


@pytest.mark.parametrize("preference", ["cheapest", "fastest"])
def test_preferred_airline_beats_cheapest_and_fastest_today(
    tmp_path, monkeypatch, preference
) -> None:
    """Today: "please use X" wins over the customer's own cheapest/fastest whenever X has a
    price (rfq._price_and_quote, `liked`), and it is stored for every later RFQ of the
    customer. Balanced choice without it: Lagoon Isles 3,210.00 (3 d)."""
    w = build_world(tmp_path, monkeypatch)
    quote = _uln(
        w,
        20001,
        f"Please use Pampero Air Cargo. We need the {preference} option.",
        preference=preference,
    )
    assert quote is not None and quote["airline"] == "Pampero Air Cargo"
    assert quote["total_minor"] == 378_000 + 18_000  # 17.8 % above Lagoon Isles
    later = _uln(w, 20002, "Thank you.", preference=preference)
    assert later is not None and later["airline"] == "Pampero Air Cargo"  # still preferred


# ---------------------------------------------------------------- 2. extra questions

HOFMANN_43 = (
    "Hello,\n\nplease send your cheapest all-in price in EUR from Hamburg (HAM) to Delhi (DEL). "
    "We deliver to HAM ourselves. 3 pieces, 420 kg gross, 2.10 cbm, ready on 2026-11-27.\n\n"
    "Could you also tell us the latest drop-off time at HAM and the earliest arrival at DEL?"
    "\n\nBest regards,\nPetra Hofmann"
)
HOFMANN_43_FACTS = {
    "origin": "HAM",
    "pickup_zip": None,
    "destination": "DEL",
    "weight_kg": 420.0,
    "volume_cbm": 2.1,
    "pieces": 3,
    "preference": "cheapest",
    "ready_date": "2026-11-27",
    "language": "en",
    "customer_name": "Petra",
    "customer_questions": [
        "What is the latest drop-off time at HAM?",
        "What is the earliest arrival at DEL?",
    ],
}


def _hofmann(w: World) -> str:
    w.extraction = HOFMANN_43_FACTS
    w.rfq(43001, CUSTOMER, "Air freight HAM to DEL", HOFMANN_43)
    quote = _quote(43001)
    assert quote is not None and quote["airline"] == "Vega Skyfreight"
    return next(m["body"] for m in w.to(CUSTOMER) if m["id"] == quote["mail_id"])


def test_extra_questions_with_the_composer(tmp_path, monkeypatch) -> None:
    """Live path: the composer must cover both questions (answered or marked unavailable),
    else its text is rejected and the deterministic fallback goes out."""
    w = build_world(tmp_path, monkeypatch)
    seen: dict[str, Any] = {}

    def composer(purpose: str, messages: list[dict[str, Any]], *_a: Any, **_k: Any) -> str:
        if purpose.startswith("extract"):
            return json.dumps(w.extraction)
        facts = json.loads(messages[-1]["content"])["verified_facts"]
        seen.update(facts)
        body = (
            f"Hello Petra,\n\nthank you for your request. We offer air freight from HAM to DEL "
            f"with Vega Skyfreight at an all-in price of {facts['exact_all_in_price']}, "
            f"transit {facts['transit_days_from_departure']} days.\n\n"
            f"The earliest arrival in DEL is {facts['earliest_arrival']}. "
            f"{facts['cutoff_notice']}\n\nYour austrian Power Team"
        )
        return json.dumps(
            {
                "body": body,
                "answered_questions": [1],
                "unavailable_questions": [0],
                "addressed_constraints": [],
                "unavailable_constraints": [],
                "referenced_mail_ids": [],
            }
        )

    monkeypatch.setattr("app.llm.complete", composer)
    body = _hofmann(w)
    assert seen["earliest_arrival"] and seen["confirmed_cargo_acceptance_cutoff"] is None
    assert seen["earliest_arrival"] in body and seen["cutoff_notice"] in body


def test_extra_questions_fallback_today_skips_the_drop_off_question(tmp_path, monkeypatch) -> None:
    """Composer failure: the deterministic text answers the arrival (schedule line) but says
    nothing on the drop-off time; the cutoff notice needs 'drop-off ... until/before/by'."""
    w = build_world(tmp_path, monkeypatch)
    body = _hofmann(w)
    assert "arriving in DEL on" in body
    assert "cutoff" not in body.lower() and "drop" not in body.lower()


def test_extra_questions_fallback_with_cutoff_question_notice(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch, cutoff_question_notice=True)
    body = _hofmann(w)
    assert "arriving in DEL on" in body
    assert (
        "The exact cargo acceptance cutoff at the departure airport is not yet confirmed." in body
    )


# ---------------------------------------------------------------- 3. ready date after 30.11

DECEMBER = {
    "airline": "Wayzata Air Cargo",
    "valid_from": "2026-12-01",
    "valid_to": "2026-12-31",
    "currency": "EUR",
}


def _ingest_december(tmp_path, monkeypatch) -> None:
    """A December tariff mail for Wayzata FRA to JFK, every rate +10 % (tariff.publish keeps
    the November lanes next to it, each with its own validity)."""
    runtime = tmp_path / "runtime-tariffs.json"
    seed = json.loads(pricing.TARIFFS.read_text(encoding="utf-8"))
    runtime.write_text(json.dumps(seed), encoding="utf-8")
    nov = next(
        lane
        for lane in seed["lanes"]
        if (lane["airline"], lane["origin"], lane["destination"])
        == ("Wayzata Air Cargo", "FRA", "JFK")
    )
    dec = {
        k: v
        for k, v in nov.items()
        if k in TariffLane.model_fields and k not in ("airline", "currency")
    }
    dec["breaks"] = [{**b, "rate": round(b["rate"] * 1.1, 2)} for b in nov["breaks"]]
    document = TariffDocument.model_validate({**DECEMBER, "lanes": [dec]})
    update = tariff.TariffUpdate(document, 1, "dec", "2026-11-21T12:00:00+00:00")
    assert tariff.publish(update, runtime)
    monkeypatch.setattr(pricing, "RUNTIME_TARIFFS", runtime)
    pricing.reload_lanes()


class _Nov21(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 21, 12, 0, tzinfo=tz or UTC)


def _december_quote(w: World, monkeypatch) -> int:
    monkeypatch.setattr(pricing, "datetime", _Nov21)
    w.extraction = {
        "origin": "FRA",
        "pickup_zip": None,
        "destination": "JFK",
        "weight_kg": 349.0,
        "volume_cbm": 2.4,
        "pieces": 11,
        "preference": "cheapest",
        "ready_date": "2026-12-02",
        "language": "en",
        "customer_name": None,
    }
    body = (
        "Please quote the cheapest air freight from FRA to JFK, we deliver to FRA ourselves. "
        "Ready on 2026-12-02: 11 pieces, 349 kg gross, 2.40 cbm."
    )
    w.rfq(16001, "weidner-spielwaren@hackloop.mail", "FRA to JFK in December", body)
    quote = _quote(16001)
    assert quote is not None and quote["airline"] == "Wayzata Air Cargo"
    return int(quote["total_minor"])


def test_ready_date_in_december_uses_todays_november_sheet(tmp_path, monkeypatch) -> None:
    """Today: validity is checked against the day we quote (21.11), so a 2.12 shipment gets the
    November rate even after the December tariff mail is ingested; from 1.12 on the same
    request would get the December rate."""
    w = build_world(tmp_path, monkeypatch)
    _ingest_december(tmp_path, monkeypatch)
    november = _december_quote(w, monkeypatch)
    sheet = pricing.sheet_options(
        Rfq(origin="FRA", destination="JFK", weight_kg=349, volume_cbm=2.4),
        on=date(2026, 11, 21),
    )
    assert november == sheet[0].total_eur_minor
    pricing.reload_lanes()


def test_ready_date_in_december_with_tariff_on_ready_date(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch, tariff_on_ready_date=True)
    _ingest_december(tmp_path, monkeypatch)
    december = _december_quote(w, monkeypatch)
    sheet = pricing.sheet_options(
        Rfq(origin="FRA", destination="JFK", weight_kg=349, volume_cbm=2.4),
        on=date(2026, 12, 2),
    )
    assert december == sheet[0].total_eur_minor
    pricing.reload_lanes()


def test_tariff_on_ready_date_keeps_todays_sheet_without_a_newer_one(tmp_path, monkeypatch) -> None:
    """No December tariff yet: the November sheet still prices a December shipment."""
    w = build_world(tmp_path, monkeypatch, tariff_on_ready_date=True)
    pricing.reload_lanes()
    total = _december_quote(w, monkeypatch)
    sheet = pricing.sheet_options(
        Rfq(origin="FRA", destination="JFK", weight_kg=349, volume_cbm=2.4),
        on=date(2026, 11, 21),
    )
    assert total == sheet[0].total_eur_minor


# ---------------------------------------------------------------- 4. units and separators


def _check(body: str, weight: float, volume: float) -> tuple[Rfq | None, list[str]]:
    mail = Mail(1, 1, "c@hackloop.mail", TEAM, "RFQ", body, datetime.now(UTC).isoformat())
    request, draft = validate(
        mail, Rfq(origin="FRA", destination="JFK", weight_kg=weight, volume_cbm=volume)
    )
    return request, sorted(i["field"] for i in draft.issues)


UNITS = [
    # (mail text, the extraction's kg and cbm, what the mail proves)
    ("FRA to JFK. Gross weight: 2700 lbs, total volume 6.5 cbm.", 1224.7, 6.5, 1224.7),
    ("FRA to JFK. 5 pieces, 2700 lbs gross, 6.5 cbm.", 1224.7, 6.5, 1224.7),
    ("FRA to JFK. Gross weight: 1.2 t, total volume 6.5 cbm.", 1200, 6.5, 1200),
    ("FRA to JFK. 1.2 tonnes gross, 6.5 cbm.", 1200, 6.5, 1200),
    ("FRA to JFK. 1,228 kg gross, 6.5 cbm.", 1228, 6.5, 1228),
    ("FRA nach JFK. Bruttogewicht 1.228 kg, Volumen 6,5 cbm.", 1228, 6.5, 1228),
    ("FRA to JFK. 4 pieces, 300 kg gross, each 120 x 80 x 100 cm.", 300, 3.84, 3.84),
    ("FRA to JFK. 2 pallets, 120x80x150 cm each, gross weight 410 kg.", 410, 2.88, 2.88),
]


@pytest.mark.parametrize(("body", "kg", "cbm", "proved"), UNITS)
def test_units_today_ask_unless_labelled(body: str, kg: float, cbm: float, proved: float) -> None:
    _, issues = _check(body, kg, cbm)
    labelled = body.startswith("FRA to JFK. Gross weight:")
    assert issues == ([] if labelled else [("volume_cbm" if proved < 100 else "weight_kg")])


@pytest.mark.parametrize(("body", "kg", "cbm", "proved"), UNITS)
def test_units_with_unit_evidence(monkeypatch, body: str, kg: float, cbm: float, proved: float):
    from app import config

    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, unit_evidence=True)
    )
    request, issues = _check(body, kg, cbm)
    assert issues == [] and request is not None
    got = request.volume_cbm if proved < 100 else request.weight_kg
    assert got == pytest.approx(proved, abs=0.01)


@pytest.mark.parametrize(
    ("body", "kg"),
    [
        ("FRA to JFK. 1,228 kg gross, 6.5 cbm.", 1.228),  # the extraction read 1.228 kg
        ("FRA to JFK. 1,228 kg gross, 6.5 cbm.", 900),  # neither reading
    ],
)
def test_unit_evidence_never_settles_an_ambiguous_number_alone(monkeypatch, body, kg) -> None:
    from app import config

    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, unit_evidence=True)
    )
    _, issues = _check(body, kg, 6.5)
    assert issues == ["weight_kg"]


# ---------------------------------------------------------------- 5. price in USD


def test_usd_request_is_answered_in_eur_today(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch)
    w.extraction = {**HOFMANN_43_FACTS, "customer_questions": []}
    body = HOFMANN_43.split("\n\nCould you")[0] + "\n\nPlease quote the all-in price in USD."
    w.rfq(5001, CUSTOMER, "HAM to DEL, price in USD", body)
    quote = _quote(5001)
    assert quote is not None and quote["currency"] == "EUR"
    text = next(m["body"] for m in w.to(CUSTOMER) if m["id"] == quote["mail_id"])
    assert "€" in text and "USD" not in text and "$" not in text


# ---------------------------------------------------------------- 6. revision in the thread

REVISIONS = [
    ("Thanks! Could you also quote 800 kg instead of 640 kg?", False),
    ("Sounds good, but can you please quote the fastest option instead?", True),
    ("Ok, please go ahead with the 800 kg version instead.", True),
    ("Bitte buchen Sie, aber mit 800 kg statt 640 kg.", True),
]


def _quoted_thread(w: World) -> None:
    w.extraction = ROUND_42_FACTS
    w.replies = dict(BIDS_42)
    w.rfq(
        6001,
        CUSTOMER,
        "Air freight to Ulaanbaatar",
        "Please quote 60311 Frankfurt to ULN, 640 kg gross, 3.10 cbm, ready 2026-11-26.",
    )
    assert _quote(6001) is not None


def _revise(w: World, text: str) -> list[dict[str, Any]]:
    before = len(w.sent)
    mid = w.next_id()
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Re: Air freight to Ulaanbaatar",
        body=text,
        category="customer",
        received_at=now,
        references=[6001],
    )
    mail = Mail(mid, 6001, CUSTOMER, TEAM, "Re: Air freight to Ulaanbaatar", text, now)
    booking.handle_reply(mail, 6001)
    return w.sent[before:]


@pytest.mark.parametrize(("text", "books_today"), REVISIONS)
def test_revision_never_sends_a_second_quote(tmp_path, monkeypatch, text, books_today) -> None:
    w = build_world(tmp_path, monkeypatch)
    _quoted_thread(w)
    out = _revise(w, text)
    assert len(db.get_session(6001)["quotes"]) == 1
    booked = any(m["to"] == LAGOON for m in out)
    assert booked is books_today  # today: revision wording read as "yes" books the old offer


@pytest.mark.parametrize(("text", "_books_today"), REVISIONS)
def test_revision_with_revision_not_acceptance(tmp_path, monkeypatch, text, _books_today):
    w = build_world(tmp_path, monkeypatch, revision_not_acceptance=True)
    _quoted_thread(w)
    out = _revise(w, text)
    assert len(db.get_session(6001)["quotes"]) == 1
    assert not any(m["to"] == LAGOON for m in out) and not db.get_session(6001)["bookings"]
    assert [m["to"] for m in out] == [CUSTOMER]  # one question back, no quote


# ---------------------------------------------------------------- 7. exclusions


def test_two_exclusions_in_one_mail(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch)
    quote = _uln(
        w,
        7001,
        "Please do not offer Zephyr Steppe Cargo or Lagoon Isles Airways for this shipment.",
    )
    assert w.to(ZEPHYR) == [] and w.to(LAGOON) == []
    assert quote is not None and quote["airline"] == "Pampero Air Cargo"  # 3780/5 d vs 3220/6 d


def test_exclusion_by_code_only(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch)
    quote = _uln(w, 7002, "Please do not offer ZS.")
    assert w.to(ZEPHYR) == []
    assert quote is not None and quote["airline"] == "Lagoon Isles Airways"


ALL_OUT = (
    "Please do not offer Zephyr Steppe Cargo, Lagoon Isles Airways, Himalaya Bridge Air or "
    "Pampero Air Cargo for this shipment."
)


def test_every_carrier_excluded_today(tmp_path, monkeypatch) -> None:
    """Today: no quote, the session ends in error, the customer hears nothing; the attention
    list gets the no_price error."""
    w = build_world(tmp_path, monkeypatch)
    assert _uln(w, 7003, ALL_OUT) is None
    assert not any(m["to"] in (ZEPHYR, LAGOON, HIMALAYA, PAMPERO) for m in w.sent)
    assert w.to(CUSTOMER) == []
    session = db.get_session(7003)
    assert session["status"] == "error"
    assert "no_price" in {e["kind"] for e in session["events"]}


def test_every_carrier_excluded_with_notice(tmp_path, monkeypatch) -> None:
    w = build_world(tmp_path, monkeypatch, all_excluded_notice=True)
    assert _uln(w, 7004, ALL_OUT) is None
    mails = w.to(CUSTOMER)
    assert len(mails) == 1
    text = mails[0]["body"]
    assert "Your austrian Power Team" in text and "€" not in text
    assert not has_customer_presentation_issue(text)
    assert not any(c in text for c in ":;_()")
    assert ":" not in mails[0]["subject"] and ";" not in mails[0]["subject"]
    for name in ("Zephyr", "Lagoon", "Himalaya", "Pampero"):
        assert name not in text  # exclusions acknowledged generically, as in quotes
    events = {e["kind"]: e for e in db.get_session(7004)["events"]}
    assert "needs_human" in events and events["needs_human"]["level"] == "error"
    assert not db.get_session(7004)["quotes"]
