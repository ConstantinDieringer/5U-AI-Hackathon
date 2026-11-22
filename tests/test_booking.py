"""Booking agent: acceptance rules, portal booking with expired-rate retry, confirmation."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app import db, hackloop
from app.agents import booking
from app.models import Mail

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "weidner-spielwaren@hackloop.mail"
ACCEPT = "Thank you for your offer. That works for us, please go ahead and book it.\n\nKlaus"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    # confirm right after booking unless a test waits for the airline's price itself
    monkeypatch.setattr(booking, "settings", replace(booking.settings, airline_confirm_wait_s=0))
    yield


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (ACCEPT, True),  # real acceptance, mails 618 / 799 / 1429
        ("Passt, bitte buchen.", True),
        ("Sorry, that is too expensive, we will not proceed.", False),
        ("Leider zu teuer.", False),
        ("What is the transit time?", None),
        ("Thanks!\n\n> please go ahead and book it", None),  # quoted text is ignored
    ],
)
def test_is_acceptance(body: str, expected: bool | None) -> None:
    assert booking.is_acceptance(body) is expected


def test_language() -> None:
    assert booking.language(ACCEPT) == "en"
    assert booking.language("Passt, bitte buchen. Viele Grüße") == "de"


PREBOOK_QUESTIONS = [
    (
        "Before we book, could you confirm whether this is the fastest option to DEL "
        "with pickup from 22842 on 28 November? What's the earliest arrival with this option?"
    ),
    "Before booking, please confirm the pickup time.",
    "Can we book this rate tomorrow?",
    "Please book if the cargo can arrive by Friday.",
    "We accept once you confirm the arrival date.",
    "If you can confirm arrival by Friday, please book.",
    "Bevor wir buchen, bestätigen Sie bitte die Abholung.",
    "Bitte buchen, falls der Abflug am Freitag möglich ist.",
    "I'll check whether we can proceed with the booking and get back to you.",
    "I'll check whether we can go ahead and get back to you.",
    "We are checking whether we can accept the offer.",
    "Ich prüfe, ob wir die Buchung freigeben können.",
]

EXPLICIT_DECLINES = [
    (
        "Thank you for your reply. It wasn't in the language I wrote to you in, and I couldn't "
        "follow it, so we won't go ahead."
    ),
    "We won’t go ahead.",
    "We won't proceed.",
    "I cannot accept the offer.",
    "We can't book this.",
    "We would not approve the booking.",
    "Please don't reserve it.",
    "We will not go ahead.",
]


@pytest.mark.parametrize("body", EXPLICIT_DECLINES)
def test_explicit_negative_booking_intent_overrides_approval_words(body):
    assert booking.is_acceptance(body) is False


@pytest.mark.parametrize("body", PREBOOK_QUESTIONS)
def test_prebooking_question_or_condition_is_not_consent(body):
    assert booking.is_acceptance(body) is None


@pytest.mark.parametrize(
    "body",
    [
        "Please book it. Can you send the reference afterwards?",
        "Could you book the shipment for us?",
        "Yes, go ahead. Please confirm the pickup time.",
        "Bitte buchen. Wann erhalten wir die Referenz?",
        "We checked internally and accept your offer. Please book.",
        "Wir haben die Buchung geprüft. Bitte buchen.",
        "Please book it. I'll check whether our invoice details are current.",
    ],
)
def test_explicit_booking_request_can_include_a_followup_question(body):
    assert booking.is_acceptance(body) is True


@pytest.mark.parametrize(
    "approval",
    [
        "book",
        "go ahead",
        "proceed",
        "accept",
        "approve",
        "reserve",
        "buchen",
        "passt",
    ],
)
def test_pending_check_scopes_all_existing_approval_words(approval):
    assert booking.is_acceptance(f"We are checking whether we can {approval}.") is None


class Api:
    def __init__(self, expired: set[str] | None = None) -> None:
        self.sent: list[dict] = []
        self.booked: list[tuple[str, str]] = []
        self.expired = expired or set()
        self.next_id = 5000

    def send(self, to, subject, body, in_reply_to=None, references=None):
        self.next_id += 1
        m = {
            "id": self.next_id,
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
            "references": [],
        }
        self.sent.append(m)
        return m

    def book(self, rate_id, team_reference):
        if rate_id in self.expired:
            raise hackloop.HackloopError("POST", "/v1/portal/bookings", 410, "expired")
        self.booked.append((rate_id, team_reference))
        return {
            "booking_reference": "BK-ABCD1234",
            "rate_id": rate_id,
            "price": "4408.52",
            "currency": "EUR",
            "status": "booked",
        }

    def get_rate(self, origin, destination, airline, weight_kg, volume_cbm=0.0, **_kw):
        # a full portal rate response: fresh rates are validated like quoted ones
        return {
            "rate_id": "R-freshfreshfresh",
            "airline": "Fjordline Air Cargo",
            "airline_code": airline,
            "origin": origin,
            "destination": destination,
            "weight_kg": str(weight_kg),
            "chargeable_weight_kg": str(weight_kg),
            "total": "4800.00",
            "currency": "EUR",
            "transit_days": 3,
            "valid_until": "2099-01-01T00:00:00+00:00",
        }


def _quoted_session(sid: int = 1223) -> None:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=sid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Fastest STR-EZE",
        body="quote please",
        category="rfq",
        received_at=now,
    )
    db.set_request(
        sid,
        {
            "destination": "EZE",
            "weight_kg": 1228,
            "volume_cbm": 5.37,
            "customer_name": "Klaus",
            "language": "en",
        },
    )
    db.record_quote(
        sid,
        total_minor=440852,
        currency="EUR",
        airline="Fjordline Air Cargo",
        origin="STR",
        destination="EZE",
        transit_days=4,
        source="portal",
        rate_id="R-xLidjNtljIRSGuVR",
        mail_id=sid + 1,
    )


def _accept(sid: int = 1223, mid: int = 1429) -> Mail:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Re: Fastest STR-EZE",
        body=ACCEPT,
        category="customer",
        received_at=now,
        in_reply_to=sid,
        references=[sid],
    )
    return Mail(mid, sid, CUSTOMER, TEAM, "Re: Fastest STR-EZE", ACCEPT, now, references=[sid])


def test_accept_books_portal_rate_and_confirms(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    booking.handle_reply(replace(_accept(), subject="FW: AMS TLV 104 crates"), 1223)

    assert api.booked == [("R-xLidjNtljIRSGuVR", "AP-1223")]
    conf = api.sent[-1]
    assert conf["to"] == CUSTOMER and conf["in_reply_to"] == 1429
    assert conf["subject"] == "Air freight STR to EZE"
    assert "BK-ABCD1234" in conf["body"] and "Hi Klaus" in conf["body"]
    s = db.get_session(1223)
    assert s["status"] == "booked" and s["bookings"][0]["booking_reference"] == "BK-ABCD1234"

    booking.handle_reply(_accept(mid=1430), 1223)  # a second "yes" books nothing more
    assert len(api.booked) == 1


@pytest.mark.parametrize("body", PREBOOK_QUESTIONS)
def test_prebooking_question_does_not_call_booking_or_send_confirmation(monkeypatch, body):
    from app.agents import clarification

    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    questions = []
    monkeypatch.setattr(clarification, "ask", lambda *args: questions.append(args))
    _quoted_session()
    booking.handle_reply(replace(_accept(), body=body), 1223)
    assert api.booked == [] and api.sent == []
    assert db.get_session(1223)["bookings"] == []
    assert len(questions) == 1 and questions[0][2] == "booking"


@pytest.mark.parametrize("body", EXPLICIT_DECLINES)
def test_explicit_decline_never_calls_booking(monkeypatch, body):
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    booking.handle_reply(replace(_accept(), body=body), 1223)
    assert api.booked == [] and api.sent == []
    assert db.get_session(1223)["bookings"] == []


def test_expired_rate_is_refetched(monkeypatch) -> None:
    api = Api(expired={"R-xLidjNtljIRSGuVR"})
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    monkeypatch.setattr(booking.hackloop, "get_rate", api.get_rate)
    monkeypatch.setattr(booking, "_airline_code", lambda o, d, a: "FJ")
    _quoted_session()
    booking.handle_reply(_accept(), 1223)
    assert api.booked == [("R-freshfreshfresh", "AP-1223")]


def test_airline_confirmation_sets_confirmed(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    booking.handle_reply(_accept(), 1223)
    m = Mail(
        1500,
        1500,
        "fjordline-air-cargo@hackloop.mail",
        TEAM,
        "Booking BK-ABCD1234 confirmed",
        "Booking BK-ABCD1234 confirmed.",
        "",
    )
    assert booking.on_airline_mail(m)
    assert db.get_session(1223)["bookings"][0]["status"] == "confirmed"


def test_catch_up_books_waiting_acceptance(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    started: list[int] = []
    monkeypatch.setattr(booking, "handle_reply", lambda mail, sid: started.append(sid))
    _quoted_session()
    _accept()
    booking.catch_up()
    import time

    time.sleep(0.2)
    assert started == [1223]


@pytest.mark.parametrize(
    ("air_minor", "air_currency", "expected"),
    [
        (287765, "EUR", "2.877,65 €"),
        (320000, "USD", "3.200 USD"),
        (0, "EUR", "0 €"),
    ],
)
def test_email_booking_uses_frozen_air_price_after_restart(
    monkeypatch, air_minor, air_currency, expected
) -> None:
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    _quoted_session()
    db.record_quote(
        1223,
        total_minor=301837,
        currency="EUR",
        airline="Meridian Freightways",
        origin="HAM",
        destination="EZE",
        transit_days=3,
        source="bid",
        air_total_minor=air_minor,
        air_currency=air_currency,
    )
    # Reopen the database: the chosen component must survive process state loss.
    db.init_db()
    session = db.get_session(1223)
    quote = session["quotes"][-1]
    booking._book_by_email(1223, quote, session["request"], "airline@hackloop.mail")
    assert expected in api.sent[0]["body"]
    assert "3.018,37 €" not in api.sent[0]["body"]
    customer = booking._confirmation("en", None, quote, "AP-1223")
    assert "3.018,37 €" in customer


def test_legacy_email_booking_does_not_assert_customer_total_as_air_price(monkeypatch):
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    _quoted_session()
    session = db.get_session(1223)
    booking._book_by_email(1223, session["quotes"][0], session["request"], "airline@hackloop.mail")
    assert "as quoted by you" in api.sent[0]["body"]
    assert "4.408,52 €" not in api.sent[0]["body"]


def test_customer_exclusion_blocks_booking_an_existing_quote(monkeypatch):
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    db.add_carrier_pref(CUSTOMER, "Fjordline Air Cargo", "exclude", 1223)
    booking.handle_reply(_accept(), 1223)
    assert api.booked == [] and api.sent == []
    assert not db.get_session(1223)["bookings"]


# ---------------------------------------------------------------- price changes (rounds 49/50)


def _round49(monkeypatch, wait_s: float = 0, booked_price: str = "3699.20") -> Api:
    """MUC-ORD door quote: 3,699.20 air (XC, portal) + 429.30 trucking = 4,128.50 all-in."""
    api = Api()

    def book(rate_id, team_reference):
        api.booked.append((rate_id, team_reference))
        return {
            "booking_reference": "BK-WQW57T0G",
            "rate_id": rate_id,
            "price": booked_price,
            "currency": "EUR",
            "status": "booked",
        }

    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", book)
    monkeypatch.setattr(
        booking, "settings", replace(booking.settings, airline_confirm_wait_s=wait_s)
    )
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=13096,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Cheapest MUC-ORD",
        body="quote please",
        category="rfq",
        received_at=now,
    )
    db.set_request(
        13096,
        {
            "destination": "ORD",
            "weight_kg": 1445,
            "volume_cbm": 4.07,
            "customer_name": "Klaus",
            "language": "en",
        },
    )
    db.record_quote(
        13096,
        total_minor=412850,
        currency="EUR",
        airline="Xenon Cargo Airlines",
        origin="MUC",
        destination="ORD",
        transit_days=3,
        source="portal",
        rate_id="R-SBoXEj_UyTbn6fZr",
        mail_id=13380,
        air_total_minor=369920,
        air_currency="EUR",
        alternatives=[
            {"airline": "Xenon Cargo Airlines", "total_eur_minor": 369920, "transit_days": 3},
            {"airline": "Nova Air", "total_eur_minor": 371000, "transit_days": 4},
            {"airline": "Solace", "total_eur_minor": 450000, "transit_days": 2},
            {"trucker": "Ruhrpott", "total_eur": "429.30"},
        ],
    )
    return api


def _yes(mid: int, body: str = ACCEPT) -> Mail:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Re: Cheapest MUC-ORD",
        body=body,
        category="customer",
        received_at=now,
        in_reply_to=13096,
        references=[13096],
    )
    return Mail(mid, 13096, CUSTOMER, TEAM, "Re: Cheapest MUC-ORD", body, now, references=[13096])


def _airline_confirms(price: str) -> None:
    m = Mail(
        13436,
        13436,
        "xenon-cargo-airlines@hackloop.mail",
        TEAM,
        "Booking BK-WQW57T0G confirmed",
        f"Booking BK-WQW57T0G confirmed.\nPrice: {price} EUR\n",
        "",
    )
    assert booking.on_airline_mail(m)


def _wait_for(pred, timeout: float = 5.0) -> None:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        time.sleep(0.05)
    assert pred()


def test_portal_response_price_change_asks_customer_with_new_total_and_alternative(
    monkeypatch,
) -> None:
    api = _round49(monkeypatch, booked_price="4069.12")
    booking.handle_reply(_yes(13403), 13096)
    body = api.sent[-1]["body"]
    # new all-in = 4,128.50 + (4,069.12 - 3,699.20) = 4,498.42; Nova all-in 3,710 + 429.30
    assert "4.498,42 €" in body and "4.128,50 €" in body
    assert "your shipment is booked." not in body.lower()
    assert "Once your shipment is booked," in body
    assert "Nova Air" in body and "4.139,30 €" in body and "Solace" not in body
    assert "(" not in body and " - " not in body and "—" not in body
    b = db.get_session(13096)["bookings"][0]
    assert b["customer_state"] == "price_change_asked" and b["customer_total_minor"] == 449842


def test_unchanged_booking_waits_for_airline_mail_then_asks_on_change(monkeypatch) -> None:
    api = _round49(monkeypatch, wait_s=30)
    booking.handle_reply(_yes(13403), 13096)
    assert api.sent == []  # nothing confirmed before the airline's price is known
    _airline_confirms("4069.12")
    _wait_for(lambda: len(api.sent) == 1)
    assert "4.498,42 €" in api.sent[0]["body"]
    assert db.get_session(13096)["bookings"][0]["customer_state"] == "price_change_asked"


def test_same_price_in_airline_mail_confirms_quoted_total(monkeypatch) -> None:
    api = _round49(monkeypatch, wait_s=30)
    booking.handle_reply(_yes(13403), 13096)
    _airline_confirms("3,699.20")
    _wait_for(lambda: db.get_session(13096)["bookings"][0]["customer_state"] == "confirmed_sent")
    assert len(api.sent) == 1
    assert "4.128,50 €" in api.sent[0]["body"] and "BK-WQW57T0G" in api.sent[0]["body"]
    assert db.get_session(13096)["bookings"][0]["customer_state"] == "confirmed_sent"


def test_no_airline_mail_confirms_after_wait(monkeypatch) -> None:
    api = _round49(monkeypatch, wait_s=0.3)
    booking.handle_reply(_yes(13403), 13096)
    _wait_for(lambda: len(api.sent) == 1)
    assert "4.128,50 €" in api.sent[0]["body"]


def test_late_airline_price_after_confirmation_asks_customer(monkeypatch) -> None:
    api = _round49(monkeypatch, wait_s=0)
    booking.handle_reply(_yes(13403), 13096)
    assert "4.128,50 €" in api.sent[-1]["body"]
    _airline_confirms("4069.12")
    _wait_for(lambda: len(api.sent) == 2)
    assert "4.498,42 €" in api.sent[-1]["body"] and "confirmation" in api.sent[-1]["body"]


def test_yes_to_new_price_confirms_at_new_total(monkeypatch) -> None:
    api = _round49(monkeypatch, booked_price="4069.12")
    booking.handle_reply(_yes(13403), 13096)
    booking.handle_reply(_yes(13450, "Yes, that's fine, please go ahead."), 13096)
    assert "4.498,42 €" in api.sent[-1]["body"] and "BK-WQW57T0G" in api.sent[-1]["body"]
    assert db.get_session(13096)["bookings"][0]["customer_state"] == "price_accepted"
    assert len(api.booked) == 1


@pytest.mark.parametrize(
    "answer",
    ["Please go ahead with the alternative instead.", "Too expensive, we decline.", "Hmm?"],
)
def test_other_answers_go_to_a_human_and_send_nothing(monkeypatch, answer) -> None:
    api = _round49(monkeypatch, booked_price="4069.12")
    booking.handle_reply(_yes(13403), 13096)
    sent = len(api.sent)
    booking.handle_reply(_yes(13450, answer), 13096)
    assert len(api.sent) == sent and len(api.booked) == 1
    assert db.get_session(13096)["bookings"][0]["customer_state"] == "needs_human"


def test_small_difference_within_tolerance_is_no_change(monkeypatch) -> None:
    api = _round49(monkeypatch, booked_price="3700.20")
    booking.handle_reply(_yes(13403), 13096)
    assert "4.128,50 €" in api.sent[-1]["body"]


def test_german_price_change_mail(monkeypatch) -> None:
    api = _round49(monkeypatch, booked_price="4069.12")
    booking.handle_reply(_yes(13403, "Passt, bitte buchen. Viele Grüße"), 13096)
    body = api.sent[-1]["body"]
    assert "Gesamtpreis" in body and "4.498,42 €" in body and "Nova Air" in body


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Oui, c'est parfait, vous pouvez réserver.", True),
        ("Perfecto, de acuerdo, adelante con la reserva.", True),
        ("Akkoord, graag boeken.", True),
        ("Dziękuję, proszę zarezerwować.", True),
        ("好的，请订舱，谢谢！", True),
        ("Хорошо, бронируйте.", True),
        ("这个价格太贵了，我们不订了。", False),
        ("Merci mais c'est trop cher.", False),
        ("No, gracias, no reserven.", None),
    ],
)
def test_acceptance_in_other_languages(text: str, verdict: bool | None) -> None:
    assert booking.is_acceptance(text) is verdict


REVISION_24648 = (
    "Hi there,\n\nSorry for the change, the final packing list came in, and the shipment details "
    "have changed to:\n\n5 pieces, 1317 kg gross weight, 8.18 cbm\n\nEverything else stays the "
    "same. Could you please update your offer?\n\nKlaus"
)
YES_24714 = (
    "Hi Moritz,\n\nYes, please go ahead and book the fastest option to SGN, with pickup from our "
    "site in 64344 on 24 November.\n\nKlaus"
)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (REVISION_24648, ["Gewicht 1228 → 1317 kg", "Volumen 5.37 → 8.18 cbm"]),
        ("Neu sind es 1.317 kg.", ["Gewicht 1228 → 1317 kg"]),
        ("now 1,317 kgs", ["Gewicht 1228 → 1317 kg"]),
        ("Yes, 1228 kg and 5.37 cbm as quoted, please book.", []),  # same figures
        (YES_24714, []),  # no cargo figures at all
        ("Thanks!\n\n> 5 pieces, 1317 kg", []),  # quoted history is not the customer's change
    ],
)
def test_cargo_revision_compares_own_words_with_the_quoted_request(body, expected):
    request = {"weight_kg": 1228, "volume_cbm": 5.37}
    assert booking.cargo_revision(body, request) == expected


def test_changed_cargo_hands_over_and_a_later_yes_books_nothing(monkeypatch):
    """Round 86 / session 24377: the 753 kg rate was booked for 1317 kg."""
    from app.agents import clarification

    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    questions = []
    monkeypatch.setattr(clarification, "ask", lambda *args: questions.append(args))
    _quoted_session()

    booking.handle_reply(replace(_accept(mid=1429), body=REVISION_24648), 1223)
    s = db.get_session(1223)
    assert s["mode"] == "manual" and s["bookings"] == []
    assert questions == [] and api.sent == []  # no generic "book or clarify?" question
    human = [e for e in db.events_after(0) if e["kind"] == "needs_human"]
    assert human and "1317 kg" in human[0]["message"] and human[0]["level"] == "error"

    booking.handle_reply(replace(_accept(mid=1430), body=YES_24714), 1223)
    assert api.booked == [] and db.get_session(1223)["bookings"] == []


# ---------------------------------------------------------------- booking safety (P3)


@pytest.mark.parametrize(
    "body",
    [
        "Sounds good, but our CFO has to sign first.",
        "Approved by purchasing? We will let you know.",
        "Passt, aber wir müssen noch intern freigeben.",
    ],
)
def test_yes_with_a_condition_or_open_question_is_not_consent(body):
    assert booking.is_acceptance(body) is None


def test_model_outage_never_books_a_held_yes(monkeypatch):
    from app.agents import consent_check

    monkeypatch.setattr(consent_check, "classify", lambda text, sid: "unclear")
    body = "Please hold off for now, we will book next week."
    assert booking.is_acceptance(body) is True  # the rules alone would book
    assert booking._consent(body, None, 1223) is None


def _age_quote(sid: int, minutes: int) -> None:
    from datetime import timedelta

    old = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    db.conn().execute("UPDATE quotes SET sent_at = ? WHERE session_id = ?", (old, sid))
    db.conn().commit()


@pytest.fixture
def no_rate_cache(monkeypatch):
    from app.pricing import portal_sync

    monkeypatch.setattr(portal_sync, "_cache", {})


def test_rate_older_than_15_minutes_is_refetched_before_booking(monkeypatch, no_rate_cache):
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    monkeypatch.setattr(booking.hackloop, "get_rate", api.get_rate)
    monkeypatch.setattr(booking, "_airline_code", lambda o, d, a: "FJ")
    _quoted_session()
    _age_quote(1223, 16)
    booking.handle_reply(_accept(), 1223)
    assert api.booked == [("R-freshfreshfresh", "AP-1223")]  # no attempt on the old rate


def test_fresh_quote_books_its_own_rate_without_refetch(monkeypatch):
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    monkeypatch.setattr(
        booking.hackloop, "get_rate", lambda *a, **k: pytest.fail("fresh quote re-read")
    )
    _quoted_session()
    booking.handle_reply(_accept(), 1223)
    assert api.booked == [("R-xLidjNtljIRSGuVR", "AP-1223")]


def test_fresh_rate_of_another_airline_is_never_booked(monkeypatch, no_rate_cache):
    api = Api()

    def other_airline(*args, **kwargs):
        return {**api.get_rate(*args, **kwargs), "airline": "Solace Air Cargo"}

    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    monkeypatch.setattr(booking.hackloop, "get_rate", other_airline)
    monkeypatch.setattr(booking, "_airline_code", lambda o, d, a: "FJ")
    _quoted_session()
    _age_quote(1223, 16)
    booking.handle_reply(_accept(), 1223)
    assert api.booked == [("R-xLidjNtljIRSGuVR", "AP-1223")]  # stored rate, not Solace's


def _confirmation_from(sender: str, body: str) -> bool:
    return booking.on_airline_mail(
        Mail(13436, 13436, sender, TEAM, "Booking BK-WQW57T0G confirmed", body, "")
    )


def test_confirmation_from_another_carrier_changes_nothing(monkeypatch):
    api = _round49(monkeypatch)
    booking.handle_reply(_yes(13403), 13096)
    sent = len(api.sent)
    assert _confirmation_from(
        "solace-air-cargo@hackloop.mail", "Booking BK-WQW57T0G confirmed.\nPrice: 9999.00 EUR\n"
    )
    b = db.get_session(13096)["bookings"][0]
    assert b["status"] == "booked" and b.get("airline_price_minor") is None
    assert len(api.sent) == sent


def test_confirmation_takes_the_total_not_the_insured_value(monkeypatch):
    _round49(monkeypatch)
    booking.handle_reply(_yes(13403), 13096)
    _confirmation_from(
        "xenon-cargo-airlines@hackloop.mail",
        "Booking BK-WQW57T0G confirmed. Rate 2.56 EUR/kg, total 3,699.20 EUR, "
        "cargo value 30,000 EUR insured.",
    )
    for thread in threading.enumerate():  # same price: the late-change check ends at once
        if thread.name == "late-13096":
            thread.join(5)
    assert db.get_session(13096)["bookings"][0]["airline_price_minor"] == 369920


def test_booking_retry_keeps_the_new_price():
    _quoted_session()
    common = {"booking_reference": None, "rate_id": "R-1", "airline": "Fjordline Air Cargo"}
    db.record_booking(1223, price_minor=100000, currency="EUR", status="failed", **common)
    db.record_booking(1223, price_minor=120000, currency="USD", **common)
    b = db.get_session(1223)["bookings"][0]
    assert (b["price_minor"], b["currency"], b["status"]) == (120000, "USD", "booked")
