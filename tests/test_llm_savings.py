"""Savings must not drop shipment conditions, questions, or gateway accounting."""

import dataclasses

import httpx
import pytest
from openai import BadRequestError

from app import db, llm
from app.agents import rfq
from app.mail.standard_rfq import extract_standard
from app.models import Mail, RateOption, Rfq

BODY = """Hello,
Please quote air freight.
Origin: FRA
Destination: DEL
Gross weight: 736 kg
Volume: 6.88 cbm
Pieces: 11
Ready date: 2026-11-24
Preference: cheapest
Thanks."""


def mail(body=BODY):
    return Mail(
        10,
        10,
        "customer@hackloop.mail",
        "team@hackloop.mail",
        "Air freight FRA to DEL",
        body,
        "2026-11-21T17:00:00+00:00",
    )


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))


@pytest.mark.parametrize(
    "extra",
    [
        "\nArrival must be before Friday.",
        "\nIs this dangerous goods service?",
        "\nGross weight: 800 kg",
        "\nOrigin: HAM",
        "\nOn Monday wrote:\nold cargo",
    ],
)
def test_unknown_or_conflicting_lines_use_full_extraction(extra):
    assert extract_standard(mail(BODY + extra)) is None


def test_standard_values_and_attachment_exclusion():
    parsed = extract_standard(mail())
    assert parsed.origin == "FRA" and parsed.destination == "DEL"
    assert parsed.weight_kg == 736 and parsed.volume_cbm == 6.88
    assert parsed.ready_date == "2026-11-24" and parsed.pieces == 11
    assert extract_standard(dataclasses.replace(mail(), attachments=[{"id": 1}])) is None
    assert extract_standard(mail(BODY.replace("6.88", "6.888"))) is None
    assert extract_standard(mail(BODY.replace("2026-11-24", "2026-02-30"))) is None


def test_original_self_delivery_format():
    body = (
        "Hi,\n\nCould you quote an all-in price in EUR for the cheapest air freight option "
        "to DEL (Delhi, IN)?\n\nWe’ll deliver the shipment to FRA ourselves. It’ll be ready "
        "on 2026-11-24: 11 pieces of printed marketing material, 736 kg gross, 6.88 cbm."
        "\n\nThanks\n\nSara\nLindqvist Medtech"
    )
    parsed = extract_standard(mail(body))
    assert parsed.origin == "FRA" and parsed.customer_name == "Sara"


def test_local_extraction_uses_one_short_verification(monkeypatch):
    calls = []

    def complete(purpose, *args, **kwargs):
        calls.append((purpose, kwargs))
        return '{"ok":true}'

    monkeypatch.setattr(llm, "complete", complete)
    parsed = rfq.extract_rfq(mail(), 10)
    assert parsed.weight_kg == 736
    assert [c[0] for c in calls] == ["verify_standard_rfq"]
    assert calls[0][1]["max_tokens"] == 64


@pytest.mark.parametrize("answer", ['{"ok":false}', "invalid", '{"ok":1}'])
def test_failed_verification_falls_back(monkeypatch, answer):
    purposes = []

    def complete(purpose, *args, **kwargs):
        purposes.append(purpose)
        return (
            answer
            if purpose == "verify_standard_rfq"
            else extract_standard(mail()).model_dump_json()
        )

    monkeypatch.setattr(llm, "complete", complete)
    assert rfq.extract_rfq(mail(), 10).origin == "FRA"
    assert purposes == ["verify_standard_rfq", "extract_rfq"]


def test_standard_quote_skips_composer_but_special_questions_do_not(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "complete", lambda purpose, *a, **k: calls.append(purpose) or "{}")
    request = Rfq(origin="FRA", destination="DEL", weight_kg=736, volume_cbm=6.88)
    option = RateOption("Airline", "FRA", "DEL", 3, 10000, "EUR", 10000, "sheet")
    body = rfq.compose_quote_text(request, option, 10000, None, 10)
    assert "100,00 €" in body and calls == []
    rfq.compose_quote_text(
        request.model_copy(update={"customer_questions": ["Weekend delivery?"]}),
        option,
        10000,
        None,
        10,
    )
    assert calls == ["compose_quote"]


def test_cache_key_ignores_customer_payload_and_changes_with_schema():
    prefix = {"role": "system", "content": "Extract fields"}
    a = llm._prompt_cache_key(
        "gpt-5-nano", "extract", [prefix, {"role": "user", "content": "Alice"}], None
    )
    b = llm._prompt_cache_key(
        "gpt-5-nano", "extract", [prefix, {"role": "user", "content": "Bob"}], None
    )
    assert a == b
    assert a != llm._prompt_cache_key("gpt-5-nano", "extract", [prefix], {"type": "json_object"})


def test_gateway_cache_rejection_has_safe_fallback(monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(llm, "_cache_key_supported", True)

    def create(**kw):
        calls.append(kw)
        if "prompt_cache_key" in kw:
            response = httpx.Response(400, request=httpx.Request("POST", "https://example.test"))
            raise BadRequestError(
                "Unsupported parameter: prompt_cache_key", response=response, body=None
            )
        return SimpleNamespace(
            model="gpt-5-nano",
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        )

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    assert llm.complete("probe", [{"role": "user", "content": "Hi"}], 10) == "ok"
    assert len(calls) == 2 and "prompt_cache_key" not in calls[1]
    assert len(db.session_llm_usage(10)["calls"]) == 1
