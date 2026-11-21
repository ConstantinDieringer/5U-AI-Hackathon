"""fr/es/it customers get the final reply translated; on any doubt the English original goes out."""

import dataclasses

import pytest

from app import db, hackloop, llm
from app.mail import reply_language
from app.mail.style import SIGNATURE
from app.mail.tracking_notice import NOTICES, has_notice

CUSTOMER = "claire@example.test"
TEAM = "austrian-power@hackloop.mail"
SID = 700
ENGLISH = "Hi Claire,\n\nThe total from FRA to CDG is 1250 €, reference BK-AB12.\n\n" + SIGNATURE
FRENCH = (
    "Votre envoi\n\n"
    "Bonjour Claire,\n\n"
    "Le total pour votre envoi de FRA à CDG est de 1250 €, référence BK-AB12. "
    "Merci et cordialement.\n\n"
    "[[0]]\n\n" + SIGNATURE
)
FRENCH_RFQ = (
    "Bonjour, pouvez vous nous envoyer une offre pour 300 kg de FRA à CDG ? "
    "Merci et cordialement, Claire"
)


@pytest.fixture(autouse=True)
def store(tmp_path):
    db.init_db(str(tmp_path / "reply_language.db"))


def session(body: str) -> None:
    db.ingest_mail(
        mail_id=SID,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Offre",
        body=body,
        category="rfq",
    )


def model(monkeypatch, answer: str) -> list[list[dict]]:
    calls: list[list[dict]] = []

    def complete(purpose, messages, session_id, **kw):
        calls.append(messages)
        return answer

    monkeypatch.setattr(llm, "complete", complete)
    return calls


def send(monkeypatch) -> dict:
    sent: list[dict] = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: sent.append(kw) or {"id": 701})
    hackloop.send_customer(
        CUSTOMER, "Your shipment", ENGLISH, in_reply_to=SID, session_id=SID, review=False
    )
    assert len(sent) == 1
    return sent[0]


def warnings() -> list[dict]:
    return [
        e
        for e in db.get_session(SID)["events"]
        if e["kind"] == "reply_language" and e["level"] == "warn"
    ]


def test_french_customer_gets_translated_reply_with_tracking_notice_kept(monkeypatch):
    session(FRENCH_RFQ)
    calls = model(monkeypatch, FRENCH)
    mail = send(monkeypatch)
    assert len(calls) == 1
    assert "French" in calls[0][0]["content"] and "[[0]]" in calls[0][1]["content"]
    assert mail["subject"] == "Votre envoi"
    assert "Le total pour votre envoi de FRA à CDG est de 1250 €" in mail["body"]
    assert "BK-AB12" in mail["body"] and "[[0]]" not in mail["body"]
    assert NOTICES[("en", False)] in mail["body"] and has_notice(mail["body"])
    assert not warnings()


def test_translation_that_drops_a_number_sends_english_and_warns(monkeypatch):
    session(FRENCH_RFQ)
    model(monkeypatch, FRENCH.replace("1250", "1200"))
    mail = send(monkeypatch)
    assert mail["body"].startswith("Hi Claire,") and "1250 €" in mail["body"]
    assert mail["subject"] == "Your shipment"
    assert "1250" in warnings()[0]["message"]


def test_gateway_error_sends_english_and_warns(monkeypatch):
    session(FRENCH_RFQ)

    def broken(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "complete", broken)
    assert send(monkeypatch)["body"].startswith("Hi Claire,")
    assert warnings()


@pytest.mark.parametrize(
    "customer_text",
    [
        "Hallo, bitte ein Angebot für 300 kg von FRA nach CDG. Vielen Dank und Grüße",
        "Hello, could you please quote 300 kg from FRA to CDG? Thanks and kind regards",
        # quoted French history does not count, only the customer's own latest words
        "Hello, could you please quote this for us? Thanks\n\n> " + FRENCH_RFQ,
    ],
)
def test_german_and_english_customers_are_never_translated(monkeypatch, customer_text):
    session(customer_text)
    calls = model(monkeypatch, FRENCH)
    mail = send(monkeypatch)
    assert not calls
    assert "The total from FRA to CDG is 1250 €" in mail["body"]


def test_setting_off_changes_nothing(monkeypatch):
    session(FRENCH_RFQ)
    monkeypatch.setattr(
        reply_language,
        "settings",
        dataclasses.replace(reply_language.settings, reply_in_customer_language=False),
    )
    calls = model(monkeypatch, FRENCH)
    mail = send(monkeypatch)
    assert not calls
    assert mail["subject"] == "Your shipment" and mail["body"].startswith("Hi Claire,")
