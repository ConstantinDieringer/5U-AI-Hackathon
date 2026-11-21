"""Customers in any language get the final reply translated, checked, original on any doubt."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from app import db, llm
from app.mail import reply_language, store
from app.mail.language import detect
from app.mail.tracking_notice import NOTICES

SUBJECT = "Your air freight quote FRA to JFK"
BODY = (
    "Dear Ms Dupont,\n\nThank you for your request. We can fly your 500 kg shipment from FRA "
    "to JFK on 25 November 2026. The total is 1240 € and your reference is R-4821.\n\n"
    "Kind regards\nAustrian Power\n"
)
ANSWERS = {
    "fr": (
        "Votre offre de fret aérien FRA à JFK",
        "Madame Dupont,\n\nMerci pour votre demande. Nous pouvons transporter votre envoi de "
        "500 kg de FRA à JFK le 25 novembre 2026. Le total est de 1240 € et votre référence "
        "est R-4821.\n\nCordialement\nAustrian Power",
    ),
    "pt": (
        "A sua cotação de frete aéreo FRA para JFK",
        "Cara Sra. Dupont,\n\nObrigado pelo seu pedido. Podemos transportar a sua remessa de "
        "500 kg de FRA para JFK em 25 de novembro de 2026. O total é 1240 € e a sua referência "
        "é R-4821.\n\nCom os melhores cumprimentos\nAustrian Power",
    ),
    "nl": (
        "Uw luchtvrachtofferte FRA naar JFK",
        "Beste mevrouw Dupont,\n\nBedankt voor uw aanvraag. Wij kunnen uw zending van 500 kg "
        "op 25 november 2026 van FRA naar JFK vliegen. Het totaal bedraagt 1240 € en uw "
        "referentie is R-4821.\n\nMet vriendelijke groet\nAustrian Power",
    ),
}
CUSTOMER = {
    "fr": "Bonjour,\n\npouvez-vous nous envoyer une offre pour le fret aérien ? Merci.\n\n"
    "Cordialement\nMarie Dupont",
    "pt": "Olá,\n\nvocês podem enviar uma cotação para o frete aéreo? Obrigado pela ajuda.\n\n"
    "Atenciosamente\nMaria",
    "nl": "Goedemiddag,\n\nkunnen jullie een offerte sturen voor luchtvracht naar JFK?\n\n"
    "Met vriendelijke groet\nMarijke",
    "de": "Sehr geehrte Damen und Herren,\n\nbitte senden Sie uns ein Angebot für die "
    "Luftfracht. Vielen Dank.\n\nViele Grüße\nMax",
    "en": "Dear team,\n\ncould you please quote the air freight for us? Thank you.\n\nKind regards",
}


def _answer(lang: str, subject: str | None = None, body: str | None = None) -> str:
    sub, text = ANSWERS.get(lang, (SUBJECT, BODY))
    return json.dumps({"language": lang, "subject": subject or sub, "body": body or text})


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "customer": [CUSTOMER["fr"]],
        "answer": _answer("fr"),
        "calls": [],
        "events": [],
    }
    monkeypatch.setattr(
        db,
        "customer_mail_history",
        lambda sid, sender, before: [
            {"sender": sender, "subject": "RFQ", "body": text} for text in state["customer"]
        ],
    )

    def complete(purpose: str, messages: list[dict[str, Any]], sid: int | None, **kw: Any) -> str:
        state["calls"].append(messages)
        return state["answer"]

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(
        store, "emit", lambda kind, msg, sid=None, level="info": state["events"].append((level, msg))
    )
    return state


def _localize(body: str = BODY) -> tuple[str, str]:
    return reply_language.localize(
        SUBJECT, body, session_id=7, in_reply_to=42, recipient="marie@client.example"
    )


def _levels(state: dict[str, Any]) -> list[str]:
    return [level for level, _ in state["events"]]


@pytest.mark.parametrize("lang", ["fr", "pt", "nl"])
def test_customer_gets_reply_in_own_language(mailbox: dict[str, Any], lang: str) -> None:
    mailbox["customer"], mailbox["answer"] = [CUSTOMER[lang]], _answer(lang)
    subject, body = _localize()
    assert len(mailbox["calls"]) == 1
    assert json.loads(mailbox["calls"][0][1]["content"])["customer_message"].startswith(
        CUSTOMER[lang].split(",")[0]
    )
    assert subject.split()[0] == ANSWERS[lang][0].split()[0]
    assert ANSWERS[lang][1].split("\n")[2].split()[0] in body
    assert "R-4821" in body and "500 kg" in body and "FRA" in body
    assert _levels(mailbox) == ["info"]


def test_portuguese_is_no_longer_read_as_spanish() -> None:
    assert detect(CUSTOMER["pt"]) == "pt"


def test_dropped_number_sends_original(mailbox: dict[str, Any]) -> None:
    mailbox["answer"] = _answer("fr", body=ANSWERS["fr"][1].replace("500 kg", "kg"))
    assert _localize() == (SUBJECT, BODY)
    assert _levels(mailbox) == ["warn"]


def test_translation_still_in_english_sends_original(mailbox: dict[str, Any]) -> None:
    mailbox["customer"], mailbox["answer"] = [CUSTOMER["nl"]], _answer("nl", body=BODY)
    assert _localize() == (SUBJECT, BODY)
    assert _levels(mailbox) == ["warn"]


@pytest.mark.parametrize("raw", ["not json", json.dumps({"language": "French"})])
def test_unusable_answer_sends_original(mailbox: dict[str, Any], raw: str) -> None:
    mailbox["answer"] = raw
    assert _localize() == (SUBJECT, BODY)
    assert _levels(mailbox) == ["warn"]


def test_gateway_error_sends_original(
    mailbox: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **kw: Any) -> str:
        raise llm.LlmBudgetError("budget")

    monkeypatch.setattr(llm, "complete", boom)
    assert _localize() == (SUBJECT, BODY)
    assert _levels(mailbox) == ["warn"]


def test_model_says_english_changes_nothing(mailbox: dict[str, Any]) -> None:
    mailbox["customer"], mailbox["answer"] = ["Ok, merci"], _answer("en")
    assert _localize() == (SUBJECT, BODY)
    assert len(mailbox["calls"]) == 1 and mailbox["events"] == []


@pytest.mark.parametrize("lang", ["de", "en"])
def test_german_and_english_customers_never_translated(mailbox: dict[str, Any], lang: str) -> None:
    mailbox["customer"] = [CUSTOMER[lang]]
    assert _localize() == (SUBJECT, BODY)
    assert mailbox["calls"] == []


def test_short_reply_uses_earlier_mails(mailbox: dict[str, Any]) -> None:
    mailbox["customer"] = ["ok go ahead", CUSTOMER["en"]]
    assert _localize() == (SUBJECT, BODY)
    assert mailbox["calls"] == []


@pytest.mark.parametrize("text", ["ok go ahead", "<script>alert(1)</script>", "RFQ FRA JFK 500kg"])
def test_no_call_without_evidence_of_another_language(mailbox: dict[str, Any], text: str) -> None:
    mailbox["customer"] = [text]
    assert _localize() == (SUBJECT, BODY)
    assert mailbox["calls"] == []


def test_non_latin_customer_gets_a_call(mailbox: dict[str, Any]) -> None:
    mailbox["customer"], mailbox["answer"] = ["您好，请报价300公斤空运到JFK，谢谢"], _answer("en")
    _localize()
    assert len(mailbox["calls"]) == 1


def test_tracking_invitation_kept_in_customer_language(mailbox: dict[str, Any]) -> None:
    notice = NOTICES[("en", False)]
    fr = ANSWERS["fr"][1].replace("Cordialement", "[[0]]\n\nCordialement")
    mailbox["answer"] = _answer("fr", body=fr)
    _, body = _localize(BODY.replace("Kind regards", notice + "\n\nKind regards"))
    assert NOTICES[("fr", False)] in body


def test_setting_off_changes_nothing(
    mailbox: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    off = dataclasses.replace(reply_language.settings, reply_in_customer_language=False)
    monkeypatch.setattr(reply_language, "settings", off)
    assert _localize() == (SUBJECT, BODY)
    assert mailbox["calls"] == [] and mailbox["events"] == []
