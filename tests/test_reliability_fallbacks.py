"""Never-silent fallbacks (rounds 60, 63-65, 70): unreadable bids get one checked model
reading, a missing trucker bid gets an estimate from earlier zone bids, booking replies the
rules cannot decide get a checked second opinion, and short replies keep the customer's
language. No live calls: every model answer here is a stub."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app import config, db
from app.agents import bid_reader, bids, booking, consent_check
from app.mail import language, reply_language
from app.models import Mail
from app.pricing import truck_estimate
from app.pricing.truck_estimate import PastBid

SID = 321


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(bids, "_spawn", lambda fn, *a: fn(*a))
    bids.close(SID)
    yield
    bids.close(SID)


# ---------------------------------------------------------------- bid reading


EUR = {"EUR", "USD"}


@pytest.mark.parametrize(
    "raw,text,expected",
    [
        (
            '{"total":"1234.50","currency":"EUR","transit_days":3}',
            "Summe | | 1.234,50 EUR",
            (Decimal("1234.50"), "EUR", 3.0),
        ),
        (
            '{"total":"980","currency":"usd","transit_days":null}',
            "Rate all in: USD 980",
            (Decimal(980), "USD", None),
        ),
        # the model may not compute or invent a total that is not written in the bid
        (
            '{"total":"1100","currency":"EUR","transit_days":2}',
            "Base 600 EUR, weight 500 EUR",
            None,
        ),
        ('{"total":null,"currency":null,"transit_days":null}', "see attachment", None),
        ('{"total":"500","currency":"CHF","transit_days":2}', "Total CHF 500", None),
        ("not json", "Total 500 EUR", None),
        ('{"total":"-5","currency":"EUR","transit_days":2}', "Total -5 EUR", None),
    ],
)
def test_model_reading_is_checked_against_the_text(raw, text, expected):
    got = bid_reader.check(raw, text, EUR)
    assert (None if got is None else (got.amount, got.currency, got.transit_days)) == expected


def _bid_mail(body: str) -> Mail:
    return Mail(
        901,
        SID,
        "elbkante-logistik@hackloop.mail",
        "austrian-power@hackloop.mail",
        f"Re: [AP-{SID}] Pickup",
        body,
        "2026-11-21T22:00:00+00:00",
    )


TABLE_EMPTY_CELLS = "Pos | Leistung | Betrag\n1 | Vorlauf | \n | Gesamt | 412,80\nWährung EUR"


def test_unreadable_bid_becomes_offer_through_checked_model_reading(monkeypatch):
    monkeypatch.setattr(bids, "_bid_evidence", lambda *a: (a[0], None, "kein Preis erkannt"))
    monkeypatch.setattr(
        bid_reader,
        "read",
        lambda text, sid, cur: bid_reader.check(
            '{"total":"412.80","currency":"EUR","transit_days":null}', text, cur
        ),
    )
    bids.on_bid_mail(_bid_mail(TABLE_EMPTY_CELLS), SID)
    (offer,) = bids.offers(SID)
    assert offer.total_eur == Decimal("412.80") and offer.currency == "EUR"


def test_model_doubt_keeps_bid_unreadable_and_stops_waiting(monkeypatch):
    monkeypatch.setattr(bids, "_bid_evidence", lambda *a: (a[0], None, "kein Preis erkannt"))
    bids.on_bid_mail(_bid_mail("Rate on request"), SID)  # conftest stub: model finds nothing
    assert bids.offers(SID) == []
    assert "elbkante-logistik@hackloop.mail" in bids._open[SID].unreadable


def test_conflicting_sources_never_go_to_the_model(monkeypatch):
    called = []
    monkeypatch.setattr(
        bids, "_bid_evidence", lambda *a: (a[0], None, "conflicting body or attachment totals")
    )
    monkeypatch.setattr(bid_reader, "read", lambda *a: called.append(a))
    bids.on_bid_mail(_bid_mail("Total 200 EUR"), SID)
    assert called == [] and bids.offers(SID) == []


def test_flag_off_restores_old_behaviour(monkeypatch):
    called = []
    monkeypatch.setattr(config, "settings", replace(config.settings, bid_llm_fallback=False))
    monkeypatch.setattr(bids, "_bid_evidence", lambda *a: (a[0], None, "kein Preis erkannt"))
    monkeypatch.setattr(bid_reader, "read", lambda *a: called.append(a))
    bids.on_bid_mail(_bid_mail("Rate on request"), SID)
    assert called == []


# ---------------------------------------------------------------- truck estimate


def test_estimate_takes_nearest_weight_and_scales_by_square_root():
    history = [
        PastBid("a@x", Decimal("200.00"), 400.0, 1),
        PastBid("b@x", Decimal("300.00"), 1600.0, 2),
    ]
    est = truck_estimate.estimate(history, 1600.0)
    assert est is not None and est.total_eur == Decimal("300.00") and est.basis.session_id == 2
    est = truck_estimate.estimate(history, 900.0)  # nearest: 400 kg -> 200 * sqrt(2.25)
    assert est is not None and est.total_eur == Decimal("300.00") and est.basis.session_id == 1


def test_no_estimate_without_comparable_history():
    assert truck_estimate.estimate([], 500.0) is None
    assert truck_estimate.estimate([PastBid("a@x", Decimal(100), 100.0, 1)], 1000.0) is None
    assert truck_estimate.for_zone(None, 500.0, SID) is None


# ---------------------------------------------------------------- consent


def _with_label(monkeypatch, label: str) -> list[str]:
    asked: list[str] = []

    def classify(text: str, sid: int) -> str:
        asked.append(text)
        return label

    monkeypatch.setattr(consent_check, "classify", classify)
    return asked


@pytest.mark.parametrize(
    "body",
    [
        "Thanks, but we won't go ahead with this one.",
        "Sorry, that is too expensive, we will not proceed.",
        "Leider zu teuer.",
    ],
)
def test_rules_decline_is_never_overruled(body, monkeypatch):
    asked = _with_label(monkeypatch, "book")
    assert booking._consent(body, None, SID) is False and asked == []


def test_plain_yes_books_without_a_model_call(monkeypatch):
    asked = _with_label(monkeypatch, "decline")
    assert booking._consent("Please go ahead and book it.", None, SID) is True and asked == []


@pytest.mark.parametrize("body", ["Yes", "Oui, merci", "Ja.", "Sí, perfecto"])
def test_undecided_reply_gets_second_opinion(body, monkeypatch):
    asked = _with_label(monkeypatch, "book")
    assert booking._consent(body, None, SID) is True and len(asked) == 1


@pytest.mark.parametrize(
    "body,label,expected",
    [
        ("Sounds good, but please hold off until Monday.", "later", None),
        ("Approved, no problem at all.", "book", True),
        ("Book it? No, we'll pass for now.", "decline", False),
    ],
)
def test_yes_next_to_a_doubt_word_is_checked(body, label, expected, monkeypatch):
    asked = _with_label(monkeypatch, label)
    assert booking._consent(body, None, SID) is expected and len(asked) == 1


@pytest.mark.parametrize(
    "body",
    [
        "Before we book, is insurance included?",
        "Please book if the cargo can arrive by Friday.",
        "Sounds good, but quote the fastest instead.",
    ],
)
def test_questions_conditions_and_changes_never_reach_the_model(body, monkeypatch):
    asked = _with_label(monkeypatch, "book")
    assert booking._consent(body, None, SID) is None and asked == []


def test_model_decline_only_settles_an_undecided_reply(monkeypatch):
    _with_label(monkeypatch, "decline")
    assert booking._consent("Hmm, we'll pass this time.", None, SID) is False


def test_model_outage_keeps_the_rules_yes(monkeypatch):
    _with_label(monkeypatch, "unclear")
    assert booking._consent(
        "Yes, please book it. Unfortunately a bit late on our side.", None, SID
    ) is True


def test_consent_flag_off_is_rules_only(monkeypatch):
    asked = _with_label(monkeypatch, "book")
    monkeypatch.setattr(booking, "settings", replace(booking.settings, consent_llm_check=False))
    assert booking._consent("Yes", None, SID) is None and asked == []


@pytest.mark.parametrize(
    "raw,label",
    [('{"label":"book"}', "book"), ('{"label":"maybe"}', "unclear"), ("{", "unclear")],
)
def test_consent_answer_is_schema_checked(raw, label):
    assert consent_check.parse(raw) == label


# ---------------------------------------------------------------- language


def _ingest(mail_id: int, body: str, sender: str = "client@hackloop.mail") -> int:
    sid = db.ingest_mail(
        mail_id=mail_id,
        sender=sender,
        recipient="austrian-power@hackloop.mail",
        subject="Demande de cotation" if mail_id == 1 else "Re: Demande de cotation",
        body=body,
        category="rfq" if mail_id == 1 else "customer",
        in_reply_to=None if mail_id == 1 else 1,
        references=() if mail_id == 1 else (1,),
    )
    assert isinstance(sid, int)
    return sid


def test_short_reply_keeps_the_language_of_the_request():
    sid = _ingest(
        1, "Bonjour, pouvez-vous nous faire une offre pour 300 kg vers JFK ? Merci et cordialement"
    )
    assert _ingest(2, "OK") == sid
    assert reply_language._customer_text(sid, 2, "client@hackloop.mail")[0] == "fr"


@pytest.mark.parametrize(
    "text,lang",
    [("Oui, allez-y", "fr"), ("Sí, adelante", "es"), ("Sì, procedete pure", "it")],
)
def test_short_affirmations_have_a_language(text, lang):
    assert language.detect(text, default="") == lang


def test_booking_text_follows_german_request_on_bare_ok():
    sid = _ingest(1, "Hallo, bitte ein Angebot für 300 kg nach JFK. Vielen Dank und Grüße")
    db.set_request(sid, {"language": "de"})
    assert booking.language("OK", sid) == "de"
    assert booking.language("OK") == "en"
    assert booking.language("Thanks, please book it.", sid) == "en"


def test_rows_from_history_feed_the_estimate(monkeypatch):
    sid = _ingest(1, "Bonjour, offre pour 400 kg merci")
    request = {"pickup_zip": "80331", "destination": "JFK", "weight_kg": 400, "language": "fr"}
    db.set_request(sid, request)
    from app.models import Rfq
    from app.pricing.reference import carriers

    zone = Rfq.model_validate(request).pickup_zone
    trucker = next((c for c in carriers() if c.kind == "trucker"), None)
    if zone is None or trucker is None:
        pytest.skip("kit has no zone for 80331 or no trucker")
    db.emit(
        "bid_received",
        f"Gebot {trucker.name}: 250.00 EUR",
        sid,
        original_amount="250.00",
        fx_rate="1",
    )
    est = truck_estimate.for_zone(zone, 400.0, SID)
    assert est is not None and est.total_eur == Decimal("250.00") and est.basis.session_id == sid
