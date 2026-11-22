"""Booking agent: acceptance rules, portal booking with expired-rate retry, confirmation."""

from __future__ import annotations

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
        return {"rate_id": "R-freshfreshfresh"}


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


@pytest.mark.core
def test_explicit_decline_never_calls_booking(monkeypatch):
    body = "Thanks, but we won’t go ahead."
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    booking.handle_reply(replace(_accept(), body=body), 1223)
    assert api.booked == [] and api.sent == []
    assert db.get_session(1223)["bookings"] == []


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
        airline="Xenon Cargo",
        origin="MUC",
        destination="ORD",
        transit_days=3,
        source="portal",
        rate_id="R-SBoXEj_UyTbn6fZr",
        mail_id=13380,
        air_total_minor=369920,
        air_currency="EUR",
        alternatives=[
            {"airline": "Xenon Cargo", "total_eur_minor": 369920, "transit_days": 3},
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


# ---------------------------------------------------------------- consent that is not current yet


@pytest.mark.core
@pytest.mark.parametrize(
    "body",
    [
        "Sounds good, but our CFO has to sign first.",
        "Approved by purchasing? We will let you know.",
        "Go ahead with option 2 if it still arrives before Friday.",
        "Passt, bitte buchen sobald der Zoll bestätigt.",
    ],
)
def test_conditional_or_pending_yes_never_books(body, monkeypatch):
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    from app.agents import consent_check

    # even a model that says "book" cannot turn a held yes into a booking
    monkeypatch.setattr(consent_check, "classify", lambda *_a, **_k: "book")
    _quoted_session()
    booking.handle_reply(replace(_accept(), body=body), 1223)
    assert api.booked == []
    assert booking.is_acceptance(body) is None


@pytest.mark.parametrize(
    "body",
    [
        "Yes please book. Let me know if you need anything else.",
        "Ja, bitte buchen. Meldet euch, falls ihr noch etwas braucht.",
        "OK book it, it is approved by our CFO.",
    ],
)
def test_plain_yes_with_polite_extras_still_accepts(body):
    assert booking.is_acceptance(body) is True


def test_yes_next_to_doubt_word_is_held_when_the_model_cannot_answer(monkeypatch):
    # conftest's consent_check answers "unclear", as it does on timeout or gateway error
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    booking.handle_reply(
        replace(_accept(), body="Please hold off for now, ok book next week."), 1223
    )
    assert api.booked == []


# ---------------------------------------------------------------- cancellation (5U, 22.11.)


def _booked(monkeypatch) -> Api:
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    booking.handle_reply(_accept(), 1223)
    assert db.get_session(1223)["bookings"][0]["booking_reference"] == "BK-ABCD1234"
    return api


def _reply(mid: int, body: str) -> Mail:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(mail_id=mid, sender=CUSTOMER, recipient=TEAM, subject="Re: Fastest STR-EZE",
                   body=body, category="customer", received_at=now, in_reply_to=1223,
                   references=[1223])  # fmt: skip
    return Mail(mid, 1223, CUSTOMER, TEAM, "Re: Fastest STR-EZE", body, now, references=[1223])


def test_customer_cancellation_cancels_through_the_portal(monkeypatch):
    api = _booked(monkeypatch)
    cancelled = []
    monkeypatch.setattr(
        booking.hackloop,
        "cancel_booking",
        lambda ref: (
            cancelled.append(ref)
            or {"booking_reference": ref, "team_reference": "AP-1223", "status": "cancelled"}
        ),
    )
    body = "Hi, sorry, our customer pulled the order. Please cancel booking BK-ABCD1234.\n\nKlaus"
    booking.handle_reply(_reply(1500, body), 1223)
    booking.handle_reply(_reply(1501, body), 1223)  # a repeat cancels nothing twice

    assert cancelled == ["BK-ABCD1234"]
    reply = api.sent[-1]
    assert "BK-ABCD1234" in reply["body"] and "cancelled" in reply["body"]
    s = db.get_session(1223)
    assert s["status"] == "lost" and s["bookings"][0]["status"] == "failed"


def test_question_about_cancellation_terms_cancels_nothing(monkeypatch):
    _booked(monkeypatch)
    monkeypatch.setattr(booking.hackloop, "cancel_booking", lambda ref: pytest.fail("cancelled"))
    booking.handle_reply(_reply(1500, "Quick question: what is your cancellation fee?"), 1223)
    assert db.get_session(1223)["bookings"][0]["status"] in ("booked", "confirmed")


# ---------------------------------------------------------------- rounds 144 and 147

FASTEST_IF_SO = (
    "Hi Moritz,\n\nCould you please confirm that this is the fastest option from STR to EZE? "
    "If so, please go ahead with the booking.\n\nKlaus"
)


def _quoted_with_options(other_days: int, sid: int = 1223) -> None:
    """The quoted Fjordline option (4 days) next to one stored alternative."""
    _quoted_session(sid)
    with db.tx() as c:
        c.execute(
            "UPDATE quotes SET alternatives_json = ? WHERE session_id = ?",
            (
                db._json(
                    [
                        {"airline": "Fjordline Air Cargo", "transit_days": 4,
                         "total_eur_minor": 440852},
                        {"airline": "Gannet Airways", "transit_days": other_days,
                         "total_eur_minor": 390000},
                    ]
                ),
                sid,
            ),
        )  # fmt: skip


def _model_says_question(monkeypatch) -> Api:
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    from app.agents import consent_check

    monkeypatch.setattr(consent_check, "classify", lambda *_a, **_k: "question")
    return api


@pytest.mark.core
def test_yes_on_a_fastest_condition_the_quote_proves_books(monkeypatch):
    """Round 147: the conditional yes was answered with questions until the customer left."""
    api = _model_says_question(monkeypatch)
    _quoted_with_options(other_days=6)
    booking.handle_reply(replace(_accept(), body=FASTEST_IF_SO), 1223)
    assert api.booked == [("R-xLidjNtljIRSGuVR", "AP-1223")]


@pytest.mark.parametrize(
    ("other_days", "body"),
    [
        (3, FASTEST_IF_SO),  # a faster option is stored: the condition fails
        (6, FASTEST_IF_SO.replace("If so, please go ahead", "Please wait with the booking")),
        (6, FASTEST_IF_SO.replace("from STR to EZE", "from MUC to EZE")),  # another airport
        (6, FASTEST_IF_SO.replace("the fastest option", "the latest drop off time")),
    ],
)
def test_conditional_yes_without_proof_never_books(other_days, body, monkeypatch):
    api = _model_says_question(monkeypatch)
    _quoted_with_options(other_days=other_days)
    booking.handle_reply(replace(_accept(), body=body), 1223)
    assert api.booked == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "Thank you for your offer. It suits us well, and we would like to place the shipment "
            "with you. However, another forwarder has offered us a rate 10 percent lower for the "
            "same shipment.\n\nCould you match their rate?"
        ),
        (
            "Thank you. To decide I need a clear answer: does your price stay as offered, or "
            "what is your new all-in total?"
        ),
    ],
)
def test_price_match_request_gets_our_unchanged_total(body):
    """Round 144: both questions got "shall we book?" and the customer booked elsewhere."""
    from app.agents import prebook

    _quoted_with_options(other_days=6)
    assert booking.classify_reply(body) == "question"
    result = prebook.answer(body, prebook.facts_for(db.get_session(1223)), "en", "Deniz")
    assert result.body is not None, result.reason
    assert "stays as offered" in result.body
    assert result.body.count("4.408,52") == 1  # the total once, no invented discount


def test_price_match_is_answered_on_the_live_path_without_booking(monkeypatch):
    api = _model_says_question(monkeypatch)
    assert booking.settings.prebook_answer is False  # production default
    _quoted_with_options(other_days=6)
    body = "Does your price stay as offered, or what is your new all-in total?\n\nKlaus"
    booking.handle_reply(replace(_accept(), body=body), 1223)
    assert api.booked == []
    assert len(api.sent) == 1 and "stays as offered" in api.sent[0]["body"]


def test_price_match_is_answered_after_both_nudges(monkeypatch):
    """FOLLOWUP_MAX=2 nudges 5 s apart are out before most replies: still answer the price."""
    from app.agents import followup, prebook

    api = _model_says_question(monkeypatch)
    monkeypatch.setattr(prebook, "MIN_GAP_S", 0.0)
    _quoted_with_options(other_days=6)
    now = datetime.now(UTC).isoformat()
    for mid in (1300, 1301):
        db.record_outgoing(
            mail_id=mid, recipient=CUSTOMER, subject="Re: Fastest STR-EZE",
            body="just checking in", category=followup.CATEGORY, session_id=1223,
            sender=TEAM, in_reply_to=1223, references=[1223], sent_at=now,
        )  # fmt: skip
    body = "Could you match their rate?\n\nKlaus"
    booking.handle_reply(replace(_accept(), body=body), 1223)
    assert api.booked == []
    assert [m for m in api.sent if "stays as offered" in m["body"]]
