"""Round 28 (mail 7237) was lost: the customer delivers to CGN himself and wrote the route,
but the LLM left origin empty and we asked for the departure four times. Every answer
("drop the shipment off at CGN ourselves ... from CGN to DFW") went unread.

SELF_DELIVERY_RECOVERY=true reads the departure airport from self-delivery wording and from
an explicit "from <German airport> to <destination>" route in the current message, and from
the customer's answer to the origin question. Default false keeps today's behaviour.
"""

import dataclasses

import pytest

from app import config, db
from app.agents import clarification
from app.mail import rfq_validation
from app.mail.rfq_validation import explicit_delivery_origin, validate
from app.models import Mail, RfqDraft

CUSTOMER = "weidner-spielwaren@hackloop.mail"
ROUND_28 = (
    "Hi there,\n\nHope your week’s going well. Could you send me an all-in price in EUR for "
    "the cheapest air freight option from CGN to DFW (Dallas, US)?\n\nWe’ll deliver the "
    "textile samples to Cologne airport (CGN) ourselves, so there’s no need to arrange "
    "pickup. The shipment will be ready on 2026-11-27 and consists of 4 pieces, with a gross "
    "weight of 85 kg and a total volume of 0.70 cbm.\n\nThanks for your help!\n\nCheers,\n"
    "Klaus Weidner\nWeidner Spielwaren GmbH, Nuremberg"
)
ANSWERS = [  # the customer's four real answers to our origin question
    (
        "Hi Moritz,\n\nThanks for getting back to me! We’ll drop the shipment off at CGN "
        "ourselves, so there’s no pickup ZIP. Can you quote it from CGN to DFW on that basis?\n\n"
        "Cheers,\nKlaus Weidner"
    ),
    (
        "Hi Moritz,\n\nThanks for getting back to me! We’ll deliver the shipment directly to CGN "
        "ourselves, so there’s no pickup ZIP. Can you quote it from CGN to DFW on that basis?\n\n"
        "Cheers,\nKlaus Weidner"
    ),
    (
        "Hi Moritz,\n\nThanks for getting back to me! There’s no pickup ZIP, as we’ll drop the "
        "shipment off at CGN ourselves. Could you quote it from CGN to DFW on that basis?\n\n"
        "Cheers,\nKlaus Weidner"
    ),
    (
        "Hi Moritz,\n\nThanks for getting back to me! There’s no pickup ZIP—we’ll drop the "
        "shipment off at CGN ourselves. Could you quote it from CGN to DFW on that basis?\n\n"
        "Cheers,\nKlaus Weidner"
    ),
]
FACTS = {  # what the extraction returned in round 28: no origin, no pickup
    "origin": None,
    "pickup_zip": None,
    "destination": "DFW",
    "weight_kg": 85.0,
    "volume_cbm": 0.7,
    "pieces": 4,
    "preference": "cheapest",
    "ready_date": "2026-11-27",
    "language": "en",
    "customer_name": "Klaus",
}


def _mail(body: str, mid: int = 7237) -> Mail:
    return Mail(
        mid,
        7237,
        CUSTOMER,
        "austrian-power@hackloop.mail",
        "Air freight CGN to DFW",
        body,
        "2026-11-21T17:44:57+00:00",
    )


@pytest.fixture()
def recovery(monkeypatch):
    def set_(on: bool) -> None:
        new = dataclasses.replace(config.settings, self_delivery_recovery=on)
        monkeypatch.setattr(config, "settings", new)
        monkeypatch.setattr(clarification, "settings", new)

    return set_


def test_default_keeps_todays_behaviour(recovery) -> None:
    recovery(False)
    assert explicit_delivery_origin(_mail(ROUND_28), "DFW") is None
    _, draft = validate(_mail(ROUND_28), RfqDraft(facts=FACTS))
    assert {i["field"] for i in draft.issues} == {"origin"}


def test_round_28_request_is_quotable_without_a_question(recovery) -> None:
    recovery(True)
    assert explicit_delivery_origin(_mail(ROUND_28), "DFW") == "CGN"
    rfq, draft = validate(_mail(ROUND_28), RfqDraft(facts=FACTS))
    assert draft.issues == [] and rfq is not None and rfq.origin == "CGN"
    assert draft.evidence["origin"]["source"] == "explicit_self_delivery"


@pytest.mark.parametrize("answer", ANSWERS)
def test_every_real_answer_names_the_departure(recovery, answer) -> None:
    recovery(True)
    assert explicit_delivery_origin(_mail(answer, 7307), "DFW") == "CGN"


@pytest.mark.parametrize(
    ("body", "destination"),
    [
        # pickup requested: the zone airport decides, not a guess from the text
        ("Please arrange the truck from our site in 51896 to the airport.", "DFW"),
        # the route must end at the requested destination
        (
            "We usually ship from HAM to EZE. This time please quote 300 kg to DFW.",
            "DFW",
        ),
        # two different airports: ambiguous, ask
        (
            "We can deliver to FRA or MUC ourselves, whatever is cheaper. 300 kg to DFW.",
            "DFW",
        ),
        # not a German export airport we serve
        ("Please quote from AMS to DFW, we deliver to AMS ourselves.", "DFW"),
        # an old forwarded request is history, not the current shipment
        (
            (
                "Please quote 300 kg to DFW, pickup details follow.\n\n"
                "-----Original Message-----\nFrom: x\nPlease quote from FRA to DFW, "
                "we deliver to FRA ourselves."
            ),
            "DFW",
        ),
    ],
)
def test_no_departure_is_guessed(recovery, body, destination) -> None:
    recovery(True)
    assert explicit_delivery_origin(_mail(body), destination) is None


@pytest.mark.parametrize(
    "enabled,answer_text,expected_origin",
    [
        (True, ANSWERS[0], "CGN"),
        (False, "The departure airport is DUS. We will deliver it there ourselves.", "DUS"),
        (False, "The departure airport code is DUS. We deliver to the airport ourselves.", "DUS"),
        (False, "Der Abflughafen ist Frankfurt. Wir liefern selbst an.", "FRA"),
    ],
)
def test_answer_resolves_the_open_origin_question(
    recovery, tmp_path, monkeypatch, enabled, answer_text, expected_origin
) -> None:
    """The thread state after our first question: the answer now completes the request."""
    recovery(False)  # the request was blocked under today's rules
    db.init_db(str(tmp_path / "t.db"))
    root = _mail(ROUND_28)
    db.ingest_mail(
        mail_id=7237,
        sender=CUSTOMER,
        recipient=root.to,
        subject=root.subject,
        body=ROUND_28,
        category="rfq",
        received_at="2099-01-01T00:00:00+00:00",
    )
    _, draft = validate(root, RfqDraft(facts=FACTS))
    db.validation_save(
        7237,
        "awaiting_clarification",
        draft.model_dump(mode="json"),
        dataclasses.asdict(root),
    )
    assert db.clarification_claim(7237)
    db.clarification_sent(7237, "sent", 7289)  # our question, as in round 28
    resumed = []
    monkeypatch.setattr("app.agents.rfq.handle_rfq", lambda mail, sid: resumed.append(sid))
    recovery(enabled)
    answer = _mail(answer_text, 7307)
    db.ingest_mail(
        mail_id=7307,
        sender=CUSTOMER,
        recipient=answer.to,
        subject=answer.subject,
        body=answer_text,
        category="customer",
        references=[7237],
        received_at="2099-01-01T00:00:10+00:00",
    )
    assert clarification.handle_reply(answer, 7237)
    assert db.validation_get(7237)["state"] == "ready" and resumed == [7237]
    assert db.get_session(7237)["request"]["origin"] == expected_origin


@pytest.mark.parametrize(
    "text",
    [
        "The departure airport is not DUS.",
        "The departure airport is DUS or FRA.",
        "Destination is DFW.",
        "We used FRA last time; not this time.",
    ],
)
def test_origin_answer_parser_does_not_guess(text):
    assert not rfq_validation.free_text_answer(text, {"origin"}, "DFW")


def test_setting_parses_true_false(monkeypatch) -> None:
    """Default on since the Task 1 replay; an explicit "false" (or any non-"true") is the off
    switch, unset or empty keeps the default."""
    for raw, value in (
        ("true", True),
        ("TRUE", True),
        ("false", False),
        ("", True),
        ("yes", False),
    ):
        monkeypatch.setenv("SELF_DELIVERY_RECOVERY", raw)
        assert config._flag("SELF_DELIVERY_RECOVERY", default=True) is value
    monkeypatch.delenv("SELF_DELIVERY_RECOVERY")
    assert config._flag("SELF_DELIVERY_RECOVERY", default=True) is True
    assert config._flag("SELF_DELIVERY_RECOVERY") is False  # switches without a default


def test_validation_module_reads_the_live_setting() -> None:
    assert rfq_validation.config is config  # monkeypatched settings reach validate()


# ---------------------------------------------------------------- PR #18 follow-ups

NEGATED = "We do not deliver to FRA ourselves, please arrange pickup."


@pytest.mark.parametrize(
    "body",
    [
        NEGATED,
        "We don’t deliver to FRA ourselves, please arrange pickup.",
        "We can't bring it to FRA ourselves, so please arrange pickup.",
        "Wir liefern die Ware nicht selbst nach Frankfurt an, bitte Abholung organisieren.",
    ],
)
def test_negated_self_delivery_is_not_a_departure(recovery, body) -> None:
    recovery(True)
    assert explicit_delivery_origin(_mail(body), "DFW") is None
    assert not rfq_validation.no_pickup(body)  # the customer wants a pickup


def test_negated_sentence_asks_for_the_pickup_zip(recovery) -> None:
    recovery(True)
    body = f"Please quote to DFW, gross weight 85 kg, volume 0.70 cbm. {NEGATED}"
    _, draft = validate(_mail(body), RfqDraft(facts=FACTS))
    assert {i["field"] for i in draft.issues} == {"pickup_zip"}


def test_negation_bug_is_todays_default(recovery) -> None:
    """Documents the default: today's rule reads the negated sentence as FRA."""
    recovery(False)
    assert explicit_delivery_origin(_mail(NEGATED), "DFW") == "FRA"


@pytest.mark.parametrize(
    ("body", "airport"),
    [
        ("Wir liefern die Ware selbst nach Frankfurt an.", "FRA"),
        ("Wir bringen die Kisten selbst zum Flughafen München.", "MUC"),
        ("Wir liefern selbst an den Flughafen Köln.", "CGN"),
    ],
)
def test_german_word_order(recovery, body, airport) -> None:
    recovery(False)
    assert explicit_delivery_origin(_mail(body), "DFW") is None
    recovery(True)
    assert explicit_delivery_origin(_mail(body), "DFW") == airport


def test_airport_codes_in_capitals_only(recovery) -> None:
    body = "We will bring the goods to the str ourselves."
    recovery(False)
    assert explicit_delivery_origin(_mail(body), "DFW") == "STR"  # today's rule
    recovery(True)
    assert explicit_delivery_origin(_mail(body), "DFW") is None
    assert explicit_delivery_origin(_mail("We will bring it to STR ourselves."), "DFW") == "STR"


def test_round_28_quotes_the_cheapest_real_price(recovery, tmp_path, monkeypatch) -> None:
    """Round 28 live portal read: Ultraviolet 390.45 EUR (4 d), Lumen 457.08 EUR (1 d); the
    sheet gives Quetzal 416.16 EUR (2 d), which the board rejected as too_high (6.6 % above)."""
    import json
    from datetime import UTC, datetime

    from app.agents import bids, rfq
    from app.models import RateOption
    from app.pricing.portal_sync import PortalResult

    recovery(True)
    db.init_db(str(tmp_path / "t.db"))
    now = datetime.now(UTC).isoformat()
    mail = dataclasses.replace(_mail(ROUND_28), received_at=now)
    db.ingest_mail(
        mail_id=7237,
        sender=CUSTOMER,
        recipient=mail.to,
        subject=mail.subject,
        body=ROUND_28,
        category="rfq",
        received_at=now,
    )

    def complete(purpose, *_a, **_k):
        if purpose.startswith("extract"):
            return json.dumps(FACTS)
        raise ValueError("no composer in this test")  # deterministic quote text

    def portal(request, **_k):
        assert (request.origin, request.destination) == ("CGN", "DFW")

        def rate(airline: str, days: int, minor: int) -> RateOption:
            return RateOption(airline, "CGN", "DFW", days, minor, "EUR", minor, "portal", "R")

        return PortalResult(
            [rate("Ultraviolet Air Freight", 4, 39045), rate("Lumen Air Cargo", 1, 45708)]
        )

    sent = []

    def send(to, subject, body, in_reply_to=None, **_k):
        sent.append({"id": 9000 + len(sent), "to": to, "subject": subject, "body": body})
        return sent[-1]

    monkeypatch.setattr(rfq.llm, "complete", complete)
    monkeypatch.setattr(rfq, "portal_result", portal)
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *_a: ([], []))
    monkeypatch.setattr(bids, "wait", lambda *_a, **_k: [])
    monkeypatch.setattr(rfq.hackloop, "send", send)

    rfq.handle_rfq(mail, 7237)

    quote = db.get_session(7237)["quotes"][0]
    assert (quote["airline"], quote["total_minor"]) == ("Ultraviolet Air Freight", 39045)
    assert "390,45 €" in sent[-1]["body"]
    audit = [e for e in db.get_session(7237)["events"] if e["kind"] == "pricing_audit"][-1]
    sheet = [o for o in audit["payload"]["options"] if o["source"] == "sheet"]
    assert [(o["airline"], o["total_eur_minor"]) for o in sheet] == [("Quetzal Air Freight", 41616)]
