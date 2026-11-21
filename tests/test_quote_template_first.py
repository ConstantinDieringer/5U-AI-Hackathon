"""QUOTE_TEMPLATE_FIRST: code writes the facts, one small call adds opening and answers."""

import dataclasses
import json
from datetime import date

import pytest

from app import db, hackloop, llm
from app.agents import quote_opening, rfq
from app.mail.outgoing import customer_body
from app.mail.style import SIGNATURE
from app.mail.voice import customer_voice
from app.models import RateOption, Rfq

CUSTOMER = "anna@hackloop.mail"
TEAM = "austrian-power@hackloop.mail"
SID = 10
TODAY = date(2026, 11, 21)
EN_MAIL = (
    "Hi team,\n\nwe have 736 kg of machine parts that must reach Delhi before our trade fair. "
    "Is pickup included?\n\nAnna"
)
DE_MAIL = (
    "Hallo zusammen,\n\nwir haben 736 kg Maschinenteile, die dringend vor der Messe nach Delhi "
    "müssen. Wie lange dauert der Flug?\n\nAnna"
)
EN_OPENING = "Getting your machine parts to Delhi before the trade fair sounds doable."
DE_OPENING = (
    "Das klingt machbar, die Maschinenteile bekommen wir rechtzeitig vor der Messe nach Delhi."
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(
        rfq, "settings", dataclasses.replace(rfq.settings, quote_template_first=True)
    )


def incoming(body=EN_MAIL):
    return db.ingest_mail(
        mail_id=SID,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Freight request",
        body=body,
        category="rfq",
        received_at="2026-11-21T12:00:00+00:00",
        references=(),
    )


def option():
    return RateOption("Test Air", "FRA", "DEL", 2, 120000, "EUR", 120000, "sheet")


def request(lang="en", **extra):
    return Rfq(
        origin="FRA",
        destination="DEL",
        weight_kg=736,
        ready_date="2026-11-26",
        customer_name="Anna",
        language=lang,
        **extra,
    )


def stub(monkeypatch, opening, answers=()):
    calls = []

    def complete(purpose, messages, session_id, **kwargs):
        calls.append({"purpose": purpose, "messages": messages, "sid": session_id, **kwargs})
        return json.dumps({"opening": opening, "answers": list(answers)})

    monkeypatch.setattr(llm, "complete", complete)
    return calls


def compose(req):
    return rfq.compose_quote_text(req, option(), 120000, None, SID, today=TODAY)


def template(req):
    return rfq.quote_text(req, option(), 120000, None, SID, today=TODAY)


def test_flag_is_off_by_default():
    assert rfq.settings.quote_template_first is False


def test_flag_off_calls_the_full_composer_exactly_as_before(monkeypatch):
    incoming()
    calls = []

    def complete(purpose, messages, session_id, **kwargs):
        calls.append((purpose, messages, session_id, kwargs))
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(llm, "complete", complete)
    body = compose(request())
    assert len(calls) == 1
    purpose, messages, session_id, kwargs = calls[0]
    assert purpose == "compose_quote" and session_id == SID
    assert messages[0]["content"] == rfq.COMPOSE_PROMPT + "\n\n" + customer_voice()
    assert set(json.loads(messages[1]["content"])) == {"verified_facts", "conversation"}
    assert kwargs == {
        "max_tokens": 750,
        "response_format": rfq.COMPOSE_SCHEMA,
        "model_name": rfq.settings.llm_compose_model,
    }
    assert body == template(request())


def test_one_small_call_and_facts_come_from_the_template_english(monkeypatch, flag_on):
    incoming()
    calls = stub(monkeypatch, EN_OPENING)
    req = request()
    body = compose(req)
    assert [c["purpose"] for c in calls] == ["compose_quote_opening"]
    call = calls[0]
    assert call["sid"] == SID
    assert call["model_name"] == rfq.settings.llm_compose_model
    assert call["max_tokens"] <= 300
    assert call["response_format"]["json_schema"]["strict"] is True
    system = call["messages"][0]["content"]
    assert len(system) < 1500
    assert customer_voice() not in system
    assert customer_voice()[:200] not in system and rfq.COMPOSE_PROMPT not in system
    sent = json.loads(call["messages"][1]["content"])
    assert sent["language"] == "en" and sent["customer_first_name"] == "Anna"
    assert "trade fair" in sent["customer_message"]
    assert sent["facts"]["airline"] == "Test Air"
    assert len(call["messages"][1]["content"]) < 1500

    plain = template(req).split("\n\n")
    parts = body.split("\n\n")
    assert parts[0] == plain[0] == "Hi Anna,"
    assert parts[1] == EN_OPENING
    assert parts[2:] == plain[2:]  # every factual paragraph and the signature are code
    assert body.count("1.200 €") == 1 and body.count("€") == 1
    assert "from FRA to DEL" in body and "Test Air" in body
    assert body.endswith(SIGNATURE + "\n")
    event = [e for e in db.events_after(0) if e["kind"] == "quote_opening"][-1]
    assert event["payload"] == {"opening": True, "answers": 0, "dropped": 0}


def test_german_mail_keeps_template_facts_and_formal_address(monkeypatch, flag_on):
    incoming(DE_MAIL)
    calls = stub(monkeypatch, DE_OPENING, ["Der Flug dauert 2 Tage ab Abflug."])
    req = request("de", customer_questions=["Wie lange dauert der Flug?"])
    body = compose(req)
    sent = json.loads(calls[0]["messages"][1]["content"])
    assert sent["language"] == "de" and sent["address_form"] == "formal Sie"
    plain = template(req).split("\n\n")
    parts = body.split("\n\n")
    assert parts[0] == "Hallo Anna," and parts[1] == DE_OPENING
    assert parts[-3] == "Der Flug dauert 2 Tage ab Abflug."
    assert parts[-2] == plain[-2] and parts[-1] == plain[-1]
    assert parts[2:-3] == plain[2:-2]
    assert body.count("1.200 €") == 1 and "von FRA nach DEL" in body and "Test Air" in body


@pytest.mark.parametrize(
    "opening",
    [
        "We can get this to Delhi in 48 hours for you.",  # invented number
        "Good news, this comes in under 1.000 € for you.",
        "Good news, this comes in at a low euro price.",
        "Quick note: we looked at Delhi for you.",
        "Happy to help (as always) with Delhi.",
        "Das klingt machbar, wir schauen uns Delhi gerne für Sie an.",  # wrong language
        "The cutoff for your cargo is tomorrow evening.",
        "Happy to help with Delhi.\n\nBest regards",
        "x" * 300,
        "",
    ],
)
def test_unsafe_opening_is_dropped_and_the_quote_still_goes_out(monkeypatch, flag_on, opening):
    incoming()
    calls = stub(monkeypatch, opening)
    req = request()
    assert compose(req) == template(req)
    assert len(calls) == 1  # dropped, never repaired with a second call


def test_number_from_customer_mail_or_facts_is_allowed(monkeypatch, flag_on):
    incoming()
    opening = "Your 736 kg for Delhi are in good hands with us."
    stub(monkeypatch, opening)
    assert opening in compose(request())


@pytest.mark.parametrize("failure", ["raise", "not json", "[]", '{"opening": 5}'])
def test_failing_model_yields_the_plain_template(monkeypatch, flag_on, failure):
    incoming()
    calls = []

    def complete(purpose, *args, **kwargs):
        calls.append(purpose)
        if failure == "raise":
            raise RuntimeError("gateway unavailable")
        return failure

    monkeypatch.setattr(llm, "complete", complete)
    for lang in ("en", "de", "fr"):
        req = request(lang)
        assert compose(req) == template(req)
    assert calls == ["compose_quote_opening"] * 3


def test_question_is_answered_only_when_the_answer_is_valid(monkeypatch, flag_on):
    incoming()
    good = "Transit is 2 days from departure."
    stub(
        monkeypatch,
        EN_OPENING,
        [good, "Insurance would add 35 € on top.", "Customs takes about 9 days."],
    )
    req = request(customer_questions=["How long is transit?", "Insurance?", "Customs time?"])
    parts = compose(req).split("\n\n")
    plain = template(req).split("\n\n")
    assert parts[-3] == good and parts[-2] == plain[-2]
    assert "Insurance" not in "".join(parts) and "Customs" not in "".join(parts)
    event = [e for e in db.events_after(0) if e["kind"] == "quote_opening"][-1]
    assert event["payload"] == {"opening": True, "answers": 1, "dropped": 2}


def test_answers_without_a_customer_question_are_ignored(monkeypatch, flag_on):
    incoming()
    stub(monkeypatch, EN_OPENING, ["Transit is 2 days from departure."])
    req = request()
    assert compose(req).split("\n\n")[2:] == template(req).split("\n\n")[2:]


def test_cutoff_question_keeps_the_code_notice_and_drops_a_model_claim(monkeypatch, flag_on):
    incoming()
    stub(monkeypatch, EN_OPENING, ["The cutoff is the evening before departure."])
    req = request(customer_questions=["What is the cargo cutoff at FRA?"])
    body = compose(req)
    assert body.split("\n\n")[1] == EN_OPENING
    assert body.split("\n\n")[2:] == template(req).split("\n\n")[2:]
    assert rfq._CUTOFF_NOTICES["en"] in body


def test_answer_goes_before_next_step_and_cutoff_notice(monkeypatch, flag_on):
    incoming()
    good = "Transit is 2 days from departure."
    stub(monkeypatch, EN_OPENING, [good])
    req = request(customer_questions=["How long is transit?", "What is the cargo cutoff?"])
    parts = compose(req).split("\n\n")
    assert parts[-4] == good
    assert parts[-2] == rfq._CUTOFF_NOTICES["en"] and parts[-1] == SIGNATURE + "\n"


def test_excluded_carrier_in_model_text_is_dropped(monkeypatch, flag_on):
    incoming()
    stub(monkeypatch, "We left Zephyr Cargo out for Delhi as you asked.")
    req = request(excluded_airlines=["zephyr cargo"])
    assert compose(req) == template(req)


def test_french_template_gets_opening_and_other_languages_write_english(monkeypatch, flag_on):
    incoming()
    opening = "Avec plaisir, nous regardons tout de suite ton envoi pour Delhi."
    calls = stub(monkeypatch, opening)
    req = request("fr")
    parts = compose(req).split("\n\n")
    assert parts[0] == "Bonjour Anna," and parts[1] == opening
    assert parts[2:] == template(req).split("\n\n")[2:]
    calls = stub(monkeypatch, EN_OPENING)
    assert EN_OPENING in compose(request("ja"))
    assert json.loads(calls[0]["messages"][1]["content"])["language"] == "en"


@pytest.mark.parametrize(
    "lang,mail,opening", [("en", EN_MAIL, EN_OPENING), ("de", DE_MAIL, DE_OPENING)]
)
def test_assembled_mail_passes_the_send_boundary(monkeypatch, flag_on, lang, mail, opening):
    incoming(mail)
    stub(monkeypatch, opening)
    body = compose(request(lang))
    customer_body(body, lang)
    sent = []

    def send(to, subject, body, in_reply_to=None, references=None, **kwargs):
        sent.append(body)
        return {"id": 999, "to": to, "subject": subject, "body": body}

    monkeypatch.setattr(hackloop, "send", send)
    hackloop.send_customer(
        CUSTOMER, "FRA to DEL", body, session_id=SID, language_code=lang, review=False
    )
    wire = sent[0]
    assert customer_body(opening, lang).strip() in wire
    assert wire.count("1.200 €") == 1 and "Test Air" in wire
    assert wire.rstrip().endswith("Your austrian Power Team")


def test_prompt_is_small_next_to_the_full_composer():
    assert len(quote_opening.OPENING_PROMPT) < 1500
    assert len(rfq.COMPOSE_PROMPT + customer_voice()) > 8 * len(quote_opening.OPENING_PROMPT)
