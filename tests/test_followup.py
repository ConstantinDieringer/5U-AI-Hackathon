"""Follow-up agent: nudge a silent customer after our quote, never after a yes or a no."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import db, hackloop
from app.agents import followup
from app.mail.outgoing import customer_body

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "meyer-industrie@hackloop.mail"
SID = 14659


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
    yield


class Api:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.next_id = 20000

    def send(self, to, subject, body, in_reply_to=None, references=None):
        self.next_id += 1
        m = {
            "id": self.next_id,
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
        }
        self.sent.append(m)
        return m


def _iso(age_s: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()


def _quoted(quote_age_s: float = 30, lang: str = "en", subject="Fastest to DEL") -> None:
    db.ingest_mail(
        mail_id=SID,
        sender=CUSTOMER,
        recipient=TEAM,
        subject=subject,
        body="quote please",
        category="rfq",
        received_at=_iso(quote_age_s + 60),
    )
    db.set_request(SID, {"destination": "DEL", "customer_name": "Anna", "language": lang})
    db.record_outgoing(
        mail_id=SID + 1,
        recipient=CUSTOMER,
        subject="Re: Fastest to DEL",
        body="our offer",
        category="quote",
        session_id=SID,
        sender=TEAM,
        in_reply_to=SID,
        sent_at=_iso(quote_age_s),
    )
    db.record_quote(
        SID,
        total_minor=500000,
        currency="EUR",
        airline="Nova",
        origin="FRA",
        destination="DEL",
        transit_days=2,
        source="sheet",
        mail_id=SID + 1,
    )
    with db.tx() as c:  # quote sent_at follows the scenario's clock
        c.execute("UPDATE quotes SET sent_at = ? WHERE session_id = ?", (_iso(quote_age_s), SID))


def _reply(mid: int, body: str, age_s: float) -> None:
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Re: Fastest to DEL",
        body=body,
        category="customer",
        received_at=_iso(age_s),
        in_reply_to=SID,
        references=[SID],
    )


@pytest.mark.parametrize("subject", ["Fastest to DEL", "FW: AMS TLV 104 crates"])
def test_silent_customer_gets_nudge_after_30s(monkeypatch, subject) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=31, subject=subject)
    followup.tick()
    assert len(api.sent) == 1
    m = api.sent[0]
    assert m["to"] == CUSTOMER and m["in_reply_to"] == SID
    assert m["subject"] == "Air freight FRA to DEL"
    stored = [mail for mail in db.get_session(SID)["mails"] if mail["direction"] == "out"][-1]
    assert stored["subject"] == m["subject"] and stored["body"] == m["body"]
    assert "Hi Anna" in m["body"] and "book" in m["body"]
    assert "5.000" not in m["body"] and "5,000" not in m["body"]  # price never restated
    followup.tick()  # our nudge is the last mail now and younger than 30 s
    assert len(api.sent) == 1


def test_no_nudge_before_30s(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=25)
    followup.tick()
    assert api.sent == []


def test_stalling_reply_round_54_gets_nudge(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=60)
    _reply(
        SID + 5,
        "Thanks, I will look through it and get back to you. Fastest is still "
        "our priority.\n\nAnna",
        age_s=32,
    )
    followup.tick()
    assert len(api.sent) == 1 and api.sent[0]["in_reply_to"] == SID + 5


def test_acceptance_and_decline_stop_nudges(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=60)
    _reply(SID + 5, "Please go ahead and book it.", age_s=20)
    followup.tick()
    assert api.sent == []  # booking agent's job
    _reply(SID + 6, "Sorry, too expensive, we will not proceed.", age_s=15)
    followup.tick()
    assert api.sent == []


def test_cap_window_and_language(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=2000)  # older than the rate validity window
    followup.tick()
    assert api.sent == []

    s = {
        "status": "quoted",
        "quotes": [{"sent_at": _iso(60), "mail_id": 1}],
        "bookings": [],
        "customer": CUSTOMER,
        "mails": [
            {
                "id": i,
                "direction": "out",
                "recipient": CUSTOMER,
                "sender": TEAM,
                "category": "followup",
                "body": "",
                "received_at": _iso(60),
            }
            for i in range(1, 4)
        ],
    }
    assert followup.due(s) is None  # three nudges sent: stop


def test_booked_paused_manual_get_nothing(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=40)
    monkeypatch.setattr(db, "is_manual", lambda sid: True)
    followup.tick()
    assert api.sent == []


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("n", [0, 1, 2])
def test_templates_pass_the_strict_renderer_unchanged(lang, n) -> None:
    for name in ("Anna", None):
        body = followup.text(lang, name, n)
        assert customer_body(body, lang)  # never raises on our own templates
    assert followup.text("de", "Anna", 0).startswith("Hallo Anna,")


def test_not_sure_yet_is_no_decline(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=60)
    _reply(SID + 5, "Not sure yet, I need to check with my manager.", age_s=32)
    followup.tick()
    assert len(api.sent) == 1


def _asked(question_age_s: float, rfq_age_s: float = 90) -> None:
    db.ingest_mail(
        mail_id=SID,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Fastest to DEL",
        body="quote please",
        category="rfq",
        received_at=_iso(rfq_age_s),
    )
    db.set_request(SID, {"destination": "DEL", "customer_name": "Anna", "language": "de"})
    db.validation_save(SID, "awaiting_clarification", {}, {})
    db.record_outgoing(
        mail_id=SID + 1,
        recipient=CUSTOMER,
        subject="Re: Fastest to DEL",
        body="Von wo holen wir ab?",
        category="clarification",
        session_id=SID,
        sender=TEAM,
        in_reply_to=SID,
        sent_at=_iso(question_age_s),
    )


def test_unanswered_question_gets_nudge(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _asked(question_age_s=31)
    followup.tick()
    assert len(api.sent) == 1
    assert "my question below" in api.sent[0]["body"] and api.sent[0]["in_reply_to"] == SID
    s = db.get_session(SID)
    assert s["mails"][-1]["category"] == followup.CATEGORY_QUESTION


def test_question_not_nudged_when_answered_or_too_early(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _asked(question_age_s=20)
    followup.tick()
    assert api.sent == []
    _reply(SID + 5, "Abholung in 22842.", age_s=1)
    s = db.get_session(SID)
    assert followup.due(s, datetime.now(UTC) + timedelta(seconds=60)) is None


def test_question_not_nudged_after_rfq_deadline(monkeypatch) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _asked(question_age_s=60, rfq_age_s=400)  # past the 6 min deadline
    followup.tick()
    assert api.sent == []


def _rescue_candidates() -> list[int]:
    now = datetime.now(UTC)
    rows = db.rescue_candidates(
        (now - timedelta(seconds=45)).isoformat(), (now - timedelta(seconds=600)).isoformat()
    )
    return [int(r["id"]) for r in rows]


@pytest.mark.parametrize(
    "body",
    [
        "Is pickup not included in the total?",  # a "no" word: booking sends nothing at all
        "Could you tell me the cut-off time? No rush.",
    ],
)
def test_unanswered_question_is_not_nudged_and_stays_a_rescue_candidate(monkeypatch, body) -> None:
    from app.agents import booking, clarification, rescue

    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=80)
    _reply(SID + 5, body, age_s=50)
    row = next(m for m in db.get_session(SID)["mails"] if m["id"] == SID + 5)
    mail = clarification._stored_mail({**row, "session_id": SID})
    booking.handle_reply(mail, SID)  # the poller's path: rules say "not a yes", no mail goes out
    assert api.sent == []
    followup.tick()
    assert api.sent == []  # a nudge in reply to the question would hide it from the rescue
    assert _rescue_candidates() == [SID + 5]
    assert rescue._eligible(SID, mail)


@pytest.mark.parametrize(
    "body",
    ["Is pickup included in the total?", "Thanks, received."],  # question, unclear
)
def test_open_reply_nobody_answered_is_not_nudged(monkeypatch, body) -> None:
    # e.g. a rescue-owned thread whose model call is still running, or the question cap
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=80)
    _reply(SID + 5, body, age_s=50)
    followup.tick()
    assert api.sent == []
    assert _rescue_candidates() == [SID + 5]


def test_answered_question_is_nudged_after_our_answer_goes_quiet(monkeypatch) -> None:
    from app.agents import booking, clarification

    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=80)
    _reply(SID + 5, "Is pickup included in the total?", age_s=50)
    row = next(m for m in db.get_session(SID)["mails"] if m["id"] == SID + 5)
    booking.handle_reply(clarification._stored_mail({**row, "session_id": SID}), SID)
    assert len(api.sent) == 1 and api.sent[0]["in_reply_to"] == SID + 5  # booking's question
    followup.tick()
    assert len(api.sent) == 1  # our mail is younger than 30 s
    later = datetime.now(UTC) + timedelta(seconds=31)
    assert followup.due(db.get_session(SID), later) == ("offer", 0)  # silence after OUR mail


@pytest.mark.parametrize(
    ("body", "nudged"),
    [
        (None, True),  # silence
        ("Yes, please book it.", False),
        ("No thanks, we will not proceed.", False),
        ("I will get back to you tomorrow.", True),  # stalling: nothing to answer
    ],
)
def test_silence_and_stalling_are_nudged_yes_and_no_are_not(monkeypatch, body, nudged) -> None:
    api = Api()
    monkeypatch.setattr(hackloop, "send", api.send)
    _quoted(quote_age_s=80)
    if body:
        _reply(SID + 5, body, age_s=50)
    followup.tick()
    assert len(api.sent) == (1 if nudged else 0)
