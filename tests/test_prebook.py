"""Pre-booking questions (PREBOOK_ANSWER): answer from the stored quote, ask for the go-ahead,
book only on a clear yes. Replies are the real ones of rounds 54 and 56 (tests/test_booking.py)."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.agents import booking, clarification, followup, prebook
from app.mail.outgoing import customer_body, validate_customer_text
from app.models import Mail

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "meyer-industrie@hackloop.mail"
SID = 14659
QUOTED_AT = "2026-11-21T20:23:00+00:00"  # round 54's day: the schedule is as of the quote

ROUND_54 = (
    "Before we book, could you confirm whether this is the fastest option to DEL with pickup "
    "from 22842 on 28 November? What's the earliest arrival with this option?"
)
ROUND_54_DE = (
    "Bevor wir buchen, können Sie bestätigen, ob das die schnellste Verbindung nach DEL mit "
    "Abholung in 22842 am 28. November ist? Wann kommt die Sendung frühestens an?"
)
ROUND_56 = "I'll check whether we can proceed with the booking and get back to you."
CUTOFF = "Thanks. What is the latest drop-off time at HAM if we bring it ourselves?"
CUTOFF_DE = "Danke. Was ist der späteste Anlieferzeitpunkt in HAM?"

FAST = {"airline": "Nova Air", "transit_days": 3, "total_minor": 480000, "total_eur_minor": 480000}
SLOW = {
    "airline": "Kestrel Cargo",
    "transit_days": 6,
    "total_minor": 395000,
    "total_eur_minor": 395000,
}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(prebook, "MIN_GAP_S", 0.0)
    yield


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(booking, "settings", replace(booking.settings, prebook_answer=True))


class Api:
    def __init__(self, expired: set[str] | None = None) -> None:
        self.sent: list[dict] = []
        self.booked: list[tuple[str, str]] = []
        self.expired = expired or set()
        self.next_id = 30000

    def send(self, to, subject, body, in_reply_to=None, references=None):
        self.next_id += 1
        m = {"id": self.next_id, "to": to, "subject": subject, "body": body,
             "in_reply_to": in_reply_to}  # fmt: skip
        self.sent.append(m)
        return m

    def book(self, rate_id, team_reference):
        from app import hackloop

        if rate_id in self.expired:
            raise hackloop.HackloopError("POST", "/v1/portal/bookings", 410, "expired")
        self.booked.append((rate_id, team_reference))
        return {"booking_reference": "BK-PRE00054", "rate_id": rate_id, "price": "4930",
                "currency": "EUR"}  # fmt: skip

    def get_rate(self, origin, destination, airline, weight_kg, volume_cbm=0.0, **_kw):
        return {"rate_id": "R-freshfreshfresh", "total": "4800.00", "currency": "EUR"}


@pytest.fixture
def api(monkeypatch) -> Api:
    a = Api()
    monkeypatch.setattr(booking.hackloop, "send", a.send)
    monkeypatch.setattr(booking.hackloop, "book", a.book)
    monkeypatch.setattr(booking.hackloop, "get_rate", a.get_rate)
    return a


def _quoted(
    *,
    preference: str = "fastest",
    quoted: dict = FAST,
    alternatives: tuple[dict, ...] = (FAST, SLOW),
    door: bool = True,
    lang: str = "en",
    valid_until: str | None = None,
    nudges: int = 0,
    quoted_at: str = QUOTED_AT,
) -> None:
    db.ingest_mail(mail_id=SID, sender=CUSTOMER, recipient=TEAM, subject="Fastest to DEL",
                   body="quote please", category="rfq", received_at="2026-11-21T20:22:47+00:00")  # fmt: skip
    request = {"destination": "DEL", "weight_kg": 640, "customer_name": "Anna", "language": lang,
               "ready_date": "2026-11-28", "preference": preference}  # fmt: skip
    if door:
        request["pickup_zip"] = "22842"
    db.set_request(SID, request)
    db.record_outgoing(mail_id=SID + 1, recipient=CUSTOMER, subject="Re: Fastest to DEL",
                       body="our offer", category="quote", session_id=SID, sender=TEAM,
                       in_reply_to=SID, sent_at=quoted_at)  # fmt: skip
    if door:
        db.emit("pricing_audit", "Pricing inputs and selected option", SID,
                trucking={"carrier": "Hansekogge", "transit_days": 1.0, "total_eur": "140"})  # fmt: skip
    total = quoted["total_minor"] + (14000 if door else 0)
    db.record_quote(
        SID, total_minor=total, currency="EUR", air_total_minor=quoted["total_minor"],
        air_currency="EUR", airline=quoted["airline"], origin="HAM", destination="DEL",
        transit_days=quoted["transit_days"], source="portal", rate_id="R-xLidjNtljIRSGuVR",
        valid_until=valid_until,
        alternatives=[dict(a, origin="HAM", destination="DEL", currency="EUR",
                           source="portal") for a in alternatives]
        + ([{"trucker": "Hansekogge", "total_eur": "140"}] if door else []),
        mail_id=SID + 1,
    )  # fmt: skip
    with db.tx() as c:
        c.execute("UPDATE quotes SET sent_at = ? WHERE session_id = ?", (quoted_at, SID))
    for i in range(nudges):
        db.record_outgoing(mail_id=SID + 2 + i, recipient=CUSTOMER, subject="Re: Fastest to DEL",
                           body="nudge", category=followup.CATEGORY, session_id=SID,
                           sender=TEAM, in_reply_to=SID,
                           sent_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat())  # fmt: skip


_NEXT = [40000]


def _reply(body: str) -> Mail:
    _NEXT[0] += 1
    mid = _NEXT[0]
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(mail_id=mid, sender=CUSTOMER, recipient=TEAM, subject="Re: Fastest to DEL",
                   body=body, category="customer", received_at=now, in_reply_to=SID + 1,
                   references=[SID])  # fmt: skip
    return Mail(mid, SID, CUSTOMER, TEAM, "Re: Fastest to DEL", body, now, references=[SID])


def _deliver(body: str) -> Mail:
    """As the poller does: an open clarification first, else the booking agent."""
    mail = _reply(body)
    if not clarification.handle_reply(mail, SID):
        booking.handle_reply(mail, SID)
    return mail


def _answer(text: str, lang: str = "en") -> prebook.Answer:
    return prebook.answer(text, prebook.facts_for(db.get_session(SID)), lang, "Anna",
                          now=datetime(2026, 11, 21, 20, 25, tzinfo=UTC))  # fmt: skip


def _amounts(body: str) -> list[str]:
    return re.findall(r"\d[\d.,]*\s*(?:€|EUR|USD)", body)


def _wire(body: str, lang: str = "en") -> str:
    rendered = customer_body(body, lang)
    validate_customer_text(rendered)
    return rendered


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (ROUND_54, "question"),
        (ROUND_54_DE, "question"),
        (ROUND_56, "deferral"),
        ("I'll look at it and get back to you.", "deferral"),
        ("Yes, please book.", "yes"),
        ("Leider zu teuer.", "no"),
        ("We will not proceed with this offer.", "no"),
        ("Is pickup not included?", "question"),  # a negation inside a question is no decline
        ("Ist die Abholung nicht inklusive?", "question"),
        ("Which airline is it, and how long is the price valid?", "question"),
        ("Was ist der günstigste Preis? Welche Fluggesellschaft fliegt?", "question"),
        ("Can you do it for 4000 EUR?", "unclear"),  # a price request is no topic we answer
        ("Thanks!", "unclear"),
    ],
)
def test_classify_reply(body, kind):
    assert booking.classify_reply(body) == kind


@pytest.mark.parametrize(
    ("text", "topics"),
    [
        (ROUND_54, ("fastest", "pickup", "schedule")),
        (ROUND_54_DE, ("fastest", "pickup", "schedule")),
        ("How long is the transit time? Which carrier flies it?", ("transit", "airline")),
        ("Is pickup included in the price?", ("pickup",)),
        ("Does the price include fuel surcharges?", ("includes",)),
        ("Is this the cheapest option? When does it depart?", ("cheapest", "schedule")),
        ("Bis wann ist das Angebot gültig? Ist der Treibstoffzuschlag enthalten?",
         ("validity", "includes")),
        ("Is it a direct flight?", ("routing",)),
        (CUTOFF, ("cutoff",)),
    ],
)  # fmt: skip
def test_parse_topics_in_customer_order(text, topics):
    asked = prebook.parse(text)
    assert asked is not None and asked.topics == topics


# ---------------------------------------------------------------- answers


def test_round_54_answer_states_fastest_arrival_pickup_and_asks_go_ahead():
    _quoted(preference="fastest")
    result = _answer(ROUND_54)
    assert result.reason == "answered" and result.body
    body = _wire(result.body)
    assert "Yes, this is the fastest option we found to DEL, with 3 days in transit." in body
    assert "pickup from 22842" in body and "28 November 2026" in body
    assert "about 24 hours" in body  # the trucker's lead time from the pricing audit
    assert "arriving in DEL on 2 December 2026" in body  # departs 29 November + 3 days
    assert "let me know and I will arrange the booking" in body
    assert "reply with" not in body
    assert _amounts(body) == []  # no amount at all: nobody asked about the price
    # the customer's order: fastest, then pickup, then arrival
    assert body.index("fastest") < body.index("22842") < body.index("2 December")


def test_round_54_german_answer():
    _quoted(preference="fastest", lang="de")
    body = _wire(_answer(ROUND_54_DE, "de").body, "de")
    assert "Ja, das ist die schnellste Verbindung" in body and "3 Tagen Laufzeit" in body
    assert "Abholung in 22842" in body and "28. November 2026" in body
    assert "Ankunft in DEL am 2. Dezember 2026" in body
    assert "sag mir einfach Bescheid" in body and _amounts(body) == []
    assert "antworten Sie einfach mit" not in body


def test_truthful_no_when_a_faster_option_was_stored_and_cheapest_was_asked():
    _quoted(preference="cheapest", quoted=SLOW)
    body = _wire(_answer("Is this the fastest option?").body)
    assert "Not quite. You asked for the cheapest option, so I quoted Kestrel Cargo" in body
    assert "6 days in transit" in body and "The fastest connection we saw takes 3 days." in body
    assert "Nova" not in body and _amounts(body) == []  # no other airline, no price, no offer


def test_truthful_no_german():
    _quoted(preference="cheapest", quoted=SLOW, lang="de")
    body = _wire(_answer("Ist das die schnellste Verbindung?", "de").body, "de")
    assert "Nicht ganz. Sie hatten nach der günstigsten Option gefragt" in body
    assert "braucht 3 Tage" in body and "Nova" not in body


def test_better_option_on_the_asked_preference_is_left_to_a_person():
    # asked "fastest", quoted for "fastest", yet a faster one was stored: no automatic answer
    _quoted(preference="fastest", quoted=SLOW)
    result = _answer("Is this the fastest option?")
    assert result.body is None and "faster option was stored" in result.reason


def test_question_without_a_stored_fact_is_promised_at_booking_never_invented():
    _quoted()
    body = _wire(_answer(CUTOFF).body)
    assert "The latest drop off time at HAM is not confirmed yet." in body
    assert "confirm it with the airline when I book" in body
    assert not re.search(r"\b\d{1,2}(?:\.\d{2})?\s*(?:am|pm)\b", body)  # no invented time


def test_question_without_a_stored_fact_german():
    _quoted(lang="de")
    body = _wire(_answer(CUTOFF_DE, "de").body, "de")
    assert "noch nicht bestätigt" in body and "Uhr" not in body


def test_price_questions_state_the_all_in_total_once_and_no_other_amount():
    _quoted(valid_until="2026-11-21T20:37:00+00:00")
    text = "Does the price include fuel surcharges? Is that the final price? How long is it valid?"
    body = _wire(_answer(text).body)
    assert _amounts(body) == ["4.940 €"]
    assert "8.37 pm UTC on 21 November 2026" in body
    assert "transit" not in body  # "how long is it valid?" is no transit question


def test_expired_validity_is_stated_truthfully_with_the_price_change_rule():
    _quoted(valid_until="2026-11-21T20:24:00+00:00")
    body = _wire(_answer("How long is the price valid?").body)
    assert "was held until" in body and "check with you before anything is confirmed" in body


def test_port_quote_says_no_pickup_is_included():
    _quoted(door=False)
    body = _wire(_answer("Is pickup included?").body)
    assert "does not include a pickup" in body


@pytest.mark.parametrize(
    "text",
    [
        "Could you confirm pickup from 80331 instead?",  # another postcode: a change
        "Is the pickup on 30 November possible?",  # another pickup date
        "Is this the fastest option to BOM?",  # another airport
        "Is this the fastest? Can you lower the price?",  # one question we cannot place
    ],
)
def test_questions_about_something_else_keep_todays_path(text):
    _quoted()
    assert _answer(text).body is None


# ---------------------------------------------------------------- flow and consent


def test_round_54_answers_without_booking_and_a_later_yes_books(on, api):
    _quoted(preference="fastest")
    _deliver(ROUND_54)
    assert api.booked == []  # a question is never consent
    assert len(api.sent) == 1 and "fastest option we found" in api.sent[0]["body"]
    s = db.get_session(SID)
    assert s["bookings"] == [] and s["status"] == "quoted"
    assert [m["category"] for m in s["mails"] if m["direction"] == "out"][-1] == prebook.CATEGORY
    assert not any(m["category"] == "clarification" for m in s["mails"])

    _deliver("Yes, please book.")
    assert api.booked == [("R-xLidjNtljIRSGuVR", f"AP-{SID}")]
    assert db.get_session(SID)["status"] == "booked"


@pytest.mark.parametrize("body", [ROUND_54, ROUND_54_DE, "Is pickup included?",
                                  "Which airline and when does it arrive?", CUTOFF])  # fmt: skip
def test_a_question_never_books(on, api, body):
    assert booking.is_acceptance(body) is not True  # the consent rule itself is unchanged
    assert booking.classify_reply(body) != "yes"
    _quoted()
    _deliver(body)
    assert api.booked == [] and db.get_session(SID)["bookings"] == []


def test_round_56_deferral_is_unchanged(on, api, monkeypatch):
    asked = []
    monkeypatch.setattr(clarification, "ask", lambda *a: asked.append(a))
    _quoted()
    _deliver(ROUND_56)
    assert api.sent == [] and api.booked == []
    assert len(asked) == 1 and asked[0][2] == "booking"  # today's path, the nudge follows


def test_flag_off_is_todays_clarification(api, monkeypatch):
    asked = []
    monkeypatch.setattr(clarification, "ask", lambda *a: asked.append(a))
    _quoted()
    _deliver(ROUND_54)
    assert api.sent == [] and api.booked == [] and len(asked) == 1


def test_unplaceable_question_falls_back_to_todays_clarification(on, api, monkeypatch):
    asked = []
    monkeypatch.setattr(clarification, "ask", lambda *a: asked.append(a))
    _quoted(preference="fastest", quoted=SLOW)
    _deliver("Is this the fastest option?")
    assert api.sent == [] and len(asked) == 1
    kinds = [e["kind"] for e in db.get_session(SID)["events"]]
    assert "prebook_not_answered" in kinds


def test_another_option_after_our_answer_never_books_the_quoted_one(on, api, monkeypatch):
    asked = []
    monkeypatch.setattr(clarification, "ask", lambda *a: asked.append(a))
    _quoted(preference="cheapest", quoted=SLOW)
    _deliver("Is this the fastest option?")
    assert len(api.sent) == 1
    _deliver("OK, please book the faster one instead.")
    assert api.booked == [] and len(asked) == 1  # back to today's clarification


def test_expired_rate_on_yes_uses_the_existing_refetch(on, api, monkeypatch):
    api.expired = {"R-xLidjNtljIRSGuVR"}
    monkeypatch.setattr(booking, "_airline_code", lambda o, d, a: "NV")
    _quoted()
    _deliver(ROUND_54)
    _deliver("Thanks, that answers it. Please go ahead and book.")
    assert api.booked == [("R-freshfreshfresh", f"AP-{SID}")]


def test_paused_or_manual_session_gets_no_answer(on, api):
    _quoted()
    db.set_paused(SID, True)
    _deliver(ROUND_54)
    db.set_paused(SID, False)
    db.takeover(SID)
    _deliver(ROUND_54)
    assert api.sent == [] and api.booked == []


def test_quarantined_mail_gets_no_answer(on, api):
    _quoted()
    mail = _reply(ROUND_54)
    db.screening_save(mail_id=mail.id, sender=CUSTOMER, thread_root=SID, verdict="phishing",
                      layer="rules", score=1.0, blocked=True, routed_as="customer",
                      reasons=["test"])  # fmt: skip
    booking.handle_reply(mail, SID)
    assert api.sent == [] and api.booked == []


def test_answers_and_nudges_share_the_follow_up_cap(on, api):
    _quoted(nudges=booking.settings.followup_max)
    _deliver(ROUND_54)
    assert api.sent == []
    assert "prebook_budget" in [e["kind"] for e in db.get_session(SID)["events"]]


def test_nudge_waits_after_our_answer_and_counts_it(on, api):
    # inside the nudge window (FOLLOWUP_WINDOW_S after the quote)
    _quoted(nudges=1, quoted_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat())
    _deliver(ROUND_54)
    s = db.get_session(SID)
    now = datetime.now(UTC)
    assert followup.due(s, now) is None  # not right after our answer
    assert followup.due(s, now + timedelta(seconds=followup.settings.followup_after_s + 1))
    db.record_outgoing(mail_id=99999, recipient=CUSTOMER, subject="Re: Fastest to DEL",
                       body="nudge", category=followup.CATEGORY, session_id=SID, sender=TEAM,
                       in_reply_to=SID)  # fmt: skip
    # 1 nudge + 1 answer + 1 nudge = FOLLOWUP_MAX (3): nothing more after the quote
    later = datetime.now(UTC) + timedelta(seconds=followup.settings.followup_after_s + 1)
    assert followup.due(db.get_session(SID), later) is None


def test_answer_keeps_a_gap_after_our_last_mail(on, api, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(prebook, "MIN_GAP_S", 10.0)
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    _quoted()
    db.record_outgoing(mail_id=SID + 50, recipient=CUSTOMER, subject="Re: Fastest to DEL",
                       body="nudge", category=followup.CATEGORY, session_id=SID, sender=TEAM,
                       in_reply_to=SID)  # fmt: skip
    _deliver(ROUND_54)
    assert len(slept) == 1 and 0 < slept[0] <= 10.0
    assert len(api.sent) == 1


def test_answer_skips_the_llm_reviewer(on, api, monkeypatch):
    from app.agents import email_review

    monkeypatch.setattr(email_review, "review_customer_mail",
                        lambda *a, **k: pytest.fail("facts must not pass the LLM reviewer"))  # fmt: skip
    _quoted()
    _deliver(ROUND_54)
    assert len(api.sent) == 1
