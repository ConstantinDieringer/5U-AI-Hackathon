"""Booking agent: acceptance rules, portal booking with expired-rate retry, confirmation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import db, hackloop
from app.agents import booking
from app.models import Mail

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "weidner-spielwaren@hackloop.mail"
ACCEPT = (
    "Thank you for your offer. That works for us, please go ahead and book it.\n\nKlaus"
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
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

    def get_rate(self, origin, destination, airline, weight_kg, volume_cbm=0.0):
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
    return Mail(
        mid, sid, CUSTOMER, TEAM, "Re: Fastest STR-EZE", ACCEPT, now, references=[sid]
    )


def test_accept_books_portal_rate_and_confirms(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(booking.hackloop, "send", api.send)
    monkeypatch.setattr(booking.hackloop, "book", api.book)
    _quoted_session()
    booking.handle_reply(_accept(), 1223)

    assert api.booked == [("R-xLidjNtljIRSGuVR", "AP-1223")]
    conf = api.sent[-1]
    assert conf["to"] == CUSTOMER and conf["in_reply_to"] == 1429
    assert "BK-ABCD1234" in conf["body"] and "Hi Klaus" in conf["body"]
    s = db.get_session(1223)
    assert (
        s["status"] == "booked"
        and s["bookings"][0]["booking_reference"] == "BK-ABCD1234"
    )

    booking.handle_reply(_accept(mid=1430), 1223)  # a second "yes" books nothing more
    assert len(api.booked) == 1


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
