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


def test_answer_resolves_the_open_origin_question(
    recovery, tmp_path, monkeypatch
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
    monkeypatch.setattr(
        "app.agents.rfq.handle_rfq", lambda mail, sid: resumed.append(sid)
    )
    recovery(True)
    answer = _mail(ANSWERS[0], 7307)
    db.ingest_mail(
        mail_id=7307,
        sender=CUSTOMER,
        recipient=answer.to,
        subject=answer.subject,
        body=ANSWERS[0],
        category="customer",
        references=[7237],
        received_at="2099-01-01T00:00:10+00:00",
    )
    assert clarification.handle_reply(answer, 7237)
    assert db.validation_get(7237)["state"] == "ready" and resumed == [7237]
    assert db.get_session(7237)["request"]["origin"] == "CGN"


def test_setting_parses_true_false(monkeypatch) -> None:
    for raw, value in (
        ("true", True),
        ("TRUE", True),
        ("false", False),
        ("", False),
        ("yes", False),
    ):
        monkeypatch.setenv("SELF_DELIVERY_RECOVERY", raw)
        assert config._flag("SELF_DELIVERY_RECOVERY") is value


def test_validation_module_reads_the_live_setting() -> None:
    assert rfq_validation.config is config  # monkeypatched settings reach validate()
