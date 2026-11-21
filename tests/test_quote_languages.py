"""Requested language survives source reading, composition, repair and wire formatting."""

import json
from datetime import date

import pytest

from app import db, hackloop
from app.agents import rfq
from app.mail.language import detect
from app.mail.style import SIGNATURE, amount
from app.mail.subjects import shipment_subject
from app.models import Mail, RateOption, Rfq


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))


def request(lang):
    return Rfq(
        origin="DUS",
        destination="SYD",
        weight_kg=279,
        volume_cbm=2.47,
        pieces=9,
        ready_date="2026-11-26",
        preference="fastest",
        language=lang,
    )


def option():
    return RateOption("Nova Atlas Cargo", "DUS", "SYD", 3, 202000, "EUR", 202000, "sheet")


def result(body):
    return json.dumps(
        {
            "body": body,
            "answered_questions": [],
            "unavailable_questions": [],
            "addressed_constraints": [],
            "unavailable_constraints": [],
        }
    )


@pytest.mark.parametrize(
    "lang,month",
    [
        ("fr", "novembre"),
        ("es", "noviembre"),
        ("it", "novembre"),
        ("nl", "november"),
        ("pl", "listopada"),
        ("tr", "Kasım"),
    ],
)
def test_fallback_and_actual_customer_wire_preserve_language(lang, month, monkeypatch):
    calls = []

    def unavailable(purpose, *args, **kwargs):
        calls.append(purpose)
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(rfq.llm, "complete", unavailable)
    body = rfq.compose_quote_text(
        request(lang), option(), 202000, None, 1, today=date(2026, 11, 21)
    )
    assert detect(body.replace(SIGNATURE, "")) == lang
    assert month in body and "2026" in body
    assert body.count(amount(202000)) == 1
    assert "DUS" in body and "SYD" in body and "Nova Atlas Cargo" in body
    sent = []

    def send(to, subject, body, in_reply_to=None, references=None):
        sent.append(
            {"id": 999, "to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to}
        )
        return sent[-1]

    monkeypatch.setattr(hackloop, "send", send)
    db.ingest_mail(
        mail_id=123,
        sender="customer@hackloop.mail",
        recipient="team@hackloop.mail",
        subject="Request",
        body=body,
        category="rfq",
    )
    db.set_request(123, request(lang).model_dump())
    hackloop.send_customer(
        "customer@hackloop.mail",
        shipment_subject("DUS", "SYD", lang, ""),
        body,
        in_reply_to=123,
        session_id=123,
        review=False,
    )
    assert sent[0]["in_reply_to"] == 123
    assert detect(sent[0]["body"].replace(SIGNATURE, "")) == lang
    from app.mail.tracking_notice import NOTICES

    assert NOTICES[(lang, False)] in sent[0]["body"]
    assert NOTICES[("en", False)] not in sent[0]["body"]
    assert "reply_translation" not in calls  # verified copy: no translation call at send


@pytest.mark.usefixtures("compose_repair_on")
@pytest.mark.parametrize("lang", ["fr", "es", "it", "nl", "pl", "tr"])
def test_composer_and_repair_receive_full_language_code(lang, monkeypatch):
    calls = []
    expected = rfq.quote_text(request(lang), option(), 202000, None, 1)
    english = rfq.quote_text(request("en"), option(), 202000, None, 1)

    def complete(purpose, messages, *args, **kwargs):
        facts = json.loads(messages[1]["content"])["verified_facts"]
        calls.append((purpose, facts["language"]))
        return result(english if len(calls) == 1 else expected)

    monkeypatch.setattr(rfq.llm, "complete", complete)
    body = rfq.compose_quote_text(request(lang), option(), 202000, None, 1)
    assert calls == [("compose_quote", lang), ("compose_quote_repair", lang)]
    assert body == expected


def test_french_request_is_not_overridden_by_english_signature_or_old_thread():
    body = (
        "Bonjour, nous souhaitons l'option la plus rapide pour notre envoi. "
        "La marchandise sera prête et nous la déposerons à DUS. "
        "Pouvez-vous nous communiquer votre tarif pour SYD ? Merci et bonne journée.\n"
        "Cheers, Klaus\nWeidner Spielwaren GmbH\n\n"
        "-----Original Message-----\nHello, please quote our old shipment from HAM to JFK."
    )
    mail = Mail(1, 1, "customer@hackloop.mail", "team@hackloop.mail", "Devis", body, "")
    assert rfq._request_language(mail, 1, "en") == "fr"


def test_attachment_request_prose_outweighs_short_english_wrapper():
    mail = Mail(
        1,
        1,
        "customer@hackloop.mail",
        "team@hackloop.mail",
        "Request",
        "Hello, please find our request attached. Cheers, Klaus",
        "",
    )
    db.kv_set(
        "rfq.sources.1",
        json.dumps(
            {
                "texts": {
                    "request.txt": (
                        "Bonjour, nous avons une demande pour votre service. La marchandise est prête. "
                        "Pouvez-vous nous proposer le transport pour notre envoi et votre tarif ? "
                        "Merci pour votre aide et pour votre réponse."
                    )
                }
            }
        ),
    )
    assert rfq._request_language(mail, 1, "fr") == "fr"


def test_unknown_template_still_quotes_in_english_for_send_translation(monkeypatch):
    # no quote at all loses the round; the English copy is translated at send
    # (app/mail/reply_language.py), with a warn event when that is not safe
    monkeypatch.setattr(rfq.llm, "complete", lambda *a, **k: "invalid json")
    body = rfq.compose_quote_text(request("ja"), option(), 202000, None, 1)
    monkeypatch.setattr(hackloop, "send", lambda **kw: pytest.fail("untranslated fallback sent"))
    with pytest.raises(hackloop.CustomerSendBlocked):
        hackloop.send_customer(
            "customer@hackloop.mail", "DUS to SYD", body, language_code="ja", review=False
        )


@pytest.mark.parametrize(
    "lang,claim",
    [
        ("fr", "L'heure limite est fixée au 26 novembre 2026."),
        ("es", "La hora límite es el 26 noviembre 2026."),
        ("it", "L'orario limite è il 26 novembre 2026."),
    ],
)
def test_localized_unsupported_cutoff_claim_still_fails_validation(lang, claim):
    from app.mail.quote_locales import COPY

    body = rfq.quote_text(request(lang), option(), 202000, None, 1)
    body = body.replace(COPY[lang]["cutoff"], claim)
    with pytest.raises(ValueError, match="cutoff_notice"):
        rfq._validate_composed(result(body), request(lang), option(), amount(202000))
