"""Round 28 (mail 7237): we asked four times and quoted by hand at 350 s. The customer's four
answers all said "we'll drop it off at CGN ourselves ... quote it from CGN to DFW", but the
reply parser read only "origin: XXX".

CLARIFY_ONCE=true: at most one question per thread; free-text answers count (a bare code, an
airport city, a ZIP, "gross weight is N kg"); an unusable or missing answer leads to the best
quote real prices allow before 120 s, flagged for a person; no real price, no quote. Default
false keeps today's behaviour (SELF_DELIVERY_RECOVERY stays off here on purpose).
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import config, db
from app.agents import bids, clarification, rfq
from app.mail.rfq_validation import free_text_answer
from app.models import Mail, RateOption
from app.pricing.portal_sync import PortalResult
from tests.test_self_delivery_origin import ANSWERS, CUSTOMER, FACTS, ROUND_28

TEAM = "austrian-power@hackloop.mail"


# ---------------------------------------------------------------- reading answers


@pytest.mark.parametrize("answer", ANSWERS)
def test_each_real_answer_names_cgn(answer) -> None:
    assert free_text_answer(answer, {"origin"}, "DFW") == {"origin": "CGN"}
    # production asked for the pickup ZIP; the same answers say there is no pickup
    assert free_text_answer(answer, {"pickup_zip"}, "DFW") == {"origin": "CGN", "no_pickup": True}


@pytest.mark.parametrize(
    ("text", "asked", "found"),
    [
        ("CGN", {"origin"}, {"origin": "CGN"}),
        ("From Cologne, please.", {"origin"}, {"origin": "CGN"}),
        ("We are in 51147 Köln-Porz.", {"origin"}, {"pickup_zip": "51147"}),
        ("Pickup is at 72379 Hechingen.", {"pickup_zip"}, {"pickup_zip": "72379"}),
        ("Our warehouse is in Bad Homburg.", {"pickup_zip"}, {"pickup_city": "Bad Homburg"}),
        ("The gross weight is 85 kg.", {"weight_kg"}, {"weight_kg": 85.0}),
        ("It's 85 kg in total.", {"weight_kg"}, {"weight_kg": 85.0}),
        ("We do not ship from FRA. We deliver to CGN.", {"origin"}, {"origin": "CGN"}),
        ("FRA or MUC, whatever is cheaper.", {"origin"}, {}),  # two airports: no answer
        ("Thanks, I'll check with the warehouse.", {"origin", "pickup_zip"}, {}),
        ("the str is closed", {"origin"}, {}),  # capitals only
        ("CGN\n\nCheers,\nKlaus\nWeidner GmbH, 90402 Nürnberg", {"origin"}, {"origin": "CGN"}),
    ],
)
def test_free_text_answers(text, asked, found) -> None:
    assert free_text_answer(text, asked, "DFW") == found


# ---------------------------------------------------------------- whole thread


@pytest.fixture()
def thread(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "t.db"))
    sent: list[dict] = []

    def send(to, subject, body, in_reply_to=None, **_k):
        sent.append({"id": 9_000 + len(sent), "to": to, "subject": subject, "body": body})
        return sent[-1]

    def complete(purpose, *_a, **_k):
        if purpose.startswith("extract"):
            return json.dumps(FACTS)
        raise ValueError("no composer in this test")

    def portal(request, **_k):
        def rate(airline: str, days: int, minor: int) -> RateOption:
            return RateOption(airline, "CGN", "DFW", days, minor, "EUR", minor, "portal", "R")

        return PortalResult(
            [rate("Ultraviolet Air Freight", 4, 39045), rate("Lumen Air Cargo", 1, 45708)]
        )

    for module in (rfq.hackloop, bids.hackloop, clarification.hackloop):
        monkeypatch.setattr(module, "send", send)
    monkeypatch.setattr(rfq.llm, "complete", complete)
    monkeypatch.setattr(rfq, "portal_result", portal)
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *_a: ([], []))
    monkeypatch.setattr(bids, "wait", lambda *_a, **_k: [])

    def configure(once: bool) -> None:
        new = dataclasses.replace(config.settings, clarify_once=once, self_delivery_recovery=False)
        for module in (config, rfq, clarification):
            monkeypatch.setattr(module, "settings", new)

    def start(body: str = ROUND_28) -> None:
        now = datetime.now(UTC).isoformat()
        db.ingest_mail(
            mail_id=7237,
            sender=CUSTOMER,
            recipient=TEAM,
            subject="Air freight CGN to DFW",
            body=body,
            category="rfq",
            received_at=now,
        )
        rfq.handle_rfq(Mail(7237, 7237, CUSTOMER, TEAM, "Air freight CGN to DFW", body, now), 7237)

    def answer(body: str) -> None:
        mid = 10_000 + len(sent) + len(db.get_session(7237)["mails"])
        now = datetime.now(UTC).isoformat()
        db.ingest_mail(
            mail_id=mid,
            sender=CUSTOMER,
            recipient=TEAM,
            subject="Re: Air freight CGN to DFW",
            body=body,
            category="customer",
            received_at=now,
            references=[7237],
        )
        mail = Mail(mid, 7237, CUSTOMER, TEAM, "Re: Air freight CGN to DFW", body, now)
        clarification.handle_reply(mail, 7237, resume=False)

    def questions() -> list[dict]:
        return [m for m in sent if m["to"] == CUSTOMER and "Ultraviolet" not in m["body"]]

    return configure, start, answer, questions, sent


def _quote(sid: int = 7237) -> dict | None:
    quotes = db.get_session(sid)["quotes"]
    return quotes[0] if quotes else None


def test_origin_answer_resolves_even_without_one_question_mode(thread) -> None:
    configure, start, answer, questions, _ = thread
    configure(False)
    start()
    answer(ANSWERS[0])
    assert len(questions()) == 1
    assert _quote()["total_minor"] == 39045


@pytest.mark.parametrize("reply", ANSWERS)
def test_one_question_and_each_real_answer_resolves_it(thread, reply) -> None:
    configure, start, answer, questions, _ = thread
    configure(True)
    start()
    assert len(questions()) == 1 and "departure airport" in questions()[0]["body"]
    answer(reply)
    quote = _quote()
    assert (quote["airline"], quote["total_minor"]) == ("Ultraviolet Air Freight", 39045)
    assert len(questions()) == 1
    assert db.validation_get(7237)["draft"]["evidence"]["origin"]["source"] == (
        "customer_correction"
    )


def test_unusable_answer_quotes_from_real_prices_and_flags_a_person(thread) -> None:
    configure, start, answer, questions, _ = thread
    configure(True)
    start()
    answer("Thanks, I'll check and come back to you.\n\nCheers,\nKlaus")
    answer("Still checking, sorry!")  # a second unusable answer: still no second question
    assert len(questions()) == 1
    # the request names exactly one network airport (CGN, "Cologne airport"): stated, not guessed
    assert (_quote()["airline"], _quote()["total_minor"]) == ("Ultraviolet Air Freight", 39045)
    kinds = [e["kind"] for e in db.get_session(7237)["events"]]
    assert "needs_human" in kinds and "clarification_capped" in kinds
    flagged = [i for i in db.attention_items() if i["kind"] == "needs_human"]
    assert [i["session_id"] for i in flagged] == [7237]


def test_no_real_price_means_no_quote(thread) -> None:
    """Round 20 class: a pickup without any site. The fallback cannot price the truck, so it
    sends nothing, asks nothing more and leaves the session to a person."""
    configure, start, answer, questions, sent = thread
    configure(True)
    body = (
        "Hi,\nplease quote the cheapest option to DFW, gross weight 85 kg, total volume 0.70 cbm."
        " Please arrange the truck from our warehouse.\n\nCheers,\nKlaus"
    )
    start(body)
    assert len(questions()) == 1 and "postal code" in questions()[0]["body"]
    answer("Thanks, I'll ask the warehouse.")
    assert len(sent) == 1 and _quote() is None
    assert db.validation_get(7237)["state"] == "manual_review"
    assert "needs_human" in [e["kind"] for e in db.get_session(7237)["events"]]


def test_no_answer_by_the_deadline_quotes_once(thread) -> None:
    configure, start, _, questions, _ = thread
    configure(True)
    start()
    created = datetime.fromisoformat(db.get_session(7237)["created_at"])
    assert clarification.fallback_due(created + timedelta(seconds=30)) == []  # still waiting
    started = clarification.fallback_due(created + timedelta(seconds=50))
    assert started == [7237]
    for thread_ in __import__("threading").enumerate():
        if thread_.name == "rfq-fallback-7237":
            thread_.join(10)
    assert _quote()["total_minor"] == 39045 and len(questions()) == 1
    assert clarification.fallback_due(created + timedelta(seconds=60)) == []  # once only


def test_deadline_waits_only_when_the_setting_is_on(thread, monkeypatch) -> None:
    configure, start, _, _, _ = thread
    configure(False)
    start()
    calls = []
    monkeypatch.setattr(clarification, "fallback_due", lambda *a: calls.append(a))
    stop = __import__("threading").Event()
    monkeypatch.setattr(stop, "wait", lambda _s: bool(calls) or stop.is_set() or stop.set())
    clarification.run(stop)
    assert calls == []
