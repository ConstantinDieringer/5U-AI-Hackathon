"""fr/es/it customers get the final reply translated, checked, English on any doubt."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app import db, llm
from app.mail import reply_language, store

SUBJECT = "Your air freight quote FRA to JFK"
BODY = (
    "Dear Ms Dupont,\n\nThank you for your request. We can fly your 500 kg shipment from FRA "
    "to JFK on 25 November 2026. The total is 1240 € and your reference is R-4821.\n\n"
    "Kind regards\nAustrian Power\n"
)
FRENCH = (
    "Votre offre de fret aérien FRA à JFK\n"
    "Madame Dupont,\n\nMerci pour votre demande. Nous pouvons transporter votre envoi de 500 kg "
    "de FRA à JFK le 25 novembre 2026. Le total est de 1240 € et votre référence est R-4821.\n\n"
    "Cordialement\nAustrian Power"
)
CUSTOMER = {
    "fr": "Bonjour,\n\npouvez-vous nous envoyer une offre pour le fret aérien ? Merci.\n\n"
    "Cordialement\nMarie Dupont",
    "de": "Sehr geehrte Damen und Herren,\n\nbitte senden Sie uns ein Angebot für die "
    "Luftfracht. Vielen Dank.\n\nViele Grüße\nMax",
    "en": "Dear team,\n\ncould you please quote the air freight for us? Thank you.\n\nKind regards",
}


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"customer": CUSTOMER["fr"], "answer": FRENCH, "calls": [], "events": []}
    monkeypatch.setattr(
        db,
        "customer_mail_history",
        lambda sid, sender, before: [{"sender": sender, "subject": "RFQ", "body": state["customer"]}],
    )

    def complete(purpose: str, messages: list[dict[str, Any]], session_id: int | None, **kw: Any) -> str:
        state["calls"].append(messages)
        return state["answer"]

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(
        store, "emit", lambda kind, msg, sid=None, level="info": state["events"].append((level, msg))
    )
    return state


def _localize() -> tuple[str, str]:
    return reply_language.localize(
        SUBJECT, BODY, session_id=7, in_reply_to=42, recipient="marie@client.fr"
    )


def test_french_customer_gets_translated_reply(mailbox: dict[str, Any]) -> None:
    subject, body = _localize()
    assert len(mailbox["calls"]) == 1
    assert mailbox["calls"][0][0]["content"].startswith("Translate into French.")
    assert subject.startswith("Votre offre")
    assert "Merci pour votre demande" in body and "R-4821" in body and "500 kg" in body
    assert all(level == "info" for level, _ in mailbox["events"])


def test_dropped_number_sends_english_original(mailbox: dict[str, Any]) -> None:
    mailbox["answer"] = FRENCH.replace("500 kg", "kg")
    assert _localize() == (SUBJECT, BODY)
    assert [level for level, _ in mailbox["events"]] == ["warn"]


def test_gateway_error_sends_english_original(
    mailbox: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **kw: Any) -> str:
        raise llm.LlmBudgetError("budget")

    monkeypatch.setattr(llm, "complete", boom)
    assert _localize() == (SUBJECT, BODY)
    assert [level for level, _ in mailbox["events"]] == ["warn"]


@pytest.mark.parametrize("lang", ["de", "en"])
def test_german_and_english_customers_never_translated(mailbox: dict[str, Any], lang: str) -> None:
    mailbox["customer"] = CUSTOMER[lang]
    assert _localize() == (SUBJECT, BODY)
    assert mailbox["calls"] == []


def test_setting_off_changes_nothing(
    mailbox: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    off = dataclasses.replace(reply_language.settings, reply_in_customer_language=False)
    monkeypatch.setattr(reply_language, "settings", off)
    assert _localize() == (SUBJECT, BODY)
    assert mailbox["calls"] == [] and mailbox["events"] == []


CHINESE_CUSTOMER = "您好，请报价从 MUC 到 JFK 的空运，500 公斤。谢谢！"
CHINESE = (
    "您的空运报价 FRA到JFK\n"
    "尊敬的 Dupont 女士，\n\n感谢您的询价。我们可以在 2026年11月25日 将您 500 kg 的货物从FRA运往JFK。"
    "总价为 1240 €，您的参考号是R-4821。\n\n此致\nAustrian Power"
)


def test_chinese_customer_gets_translated_reply(mailbox: dict[str, Any]) -> None:
    """CJK has no word boundaries: codes and references glued to Chinese text still count."""
    mailbox["customer"], mailbox["answer"] = CHINESE_CUSTOMER, CHINESE
    _, body = _localize()
    assert mailbox["calls"][0][0]["content"].startswith("Translate into Chinese.")
    assert "感谢您的询价" in body and "R-4821" in body and "1240 €" in body


def test_changed_euro_amount_sends_english_original(mailbox: dict[str, Any]) -> None:
    mailbox["answer"] = FRENCH.replace("1240 €", "1 240 €").replace("Le total", "1240 Le total")
    assert _localize() == (SUBJECT, BODY)
    assert [level for level, _ in mailbox["events"]] == ["warn"]
