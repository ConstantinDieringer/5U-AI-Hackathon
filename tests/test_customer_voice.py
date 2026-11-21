"""The maintained skill profile must reach both generation and factual repair."""

import json

import pytest

from app import db, llm
from app.agents import rfq
from app.mail.voice import customer_voice
from app.models import RateOption, Rfq


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize(
    "preference,criterion",
    [
        ("fastest", "shortest air transit"),
        ("cheapest", "lowest air price"),
    ],
)
def test_writing_profile_reaches_composer_and_repair_without_extra_pass(
    tmp_path, monkeypatch, language, preference, criterion
):
    db.init_db(str(tmp_path / "voice.db"))
    request = Rfq(
        origin="FRA", destination="DEL", weight_kg=736, language=language, preference=preference
    )
    option = RateOption("Test Air", "FRA", "DEL", 2, 120000, "EUR", 120000, "sheet")
    profile = customer_voice()
    calls = []
    body = (
        "Hello,\n\nTest Air flies FRA to DEL in two days for 1.200,00 € all-in."
        if language == "en"
        else "Guten Tag,\n\nTest Air fliegt von FRA nach DEL in zwei Tagen für 1.200,00 € all-in."
    ) + "\n\nYour austrian Power Team"

    def complete(purpose, messages, *args, **kwargs):
        facts = json.loads(messages[1]["content"])["verified_facts"]
        assert facts["requested_preference"] == preference
        assert criterion in facts["selection_explanation"]
        assert facts["pickup_included"] is False
        calls.append((purpose, messages[0]["content"]))
        return json.dumps(
            {
                "body": body.replace("1.200,00", "1.300,00") if len(calls) == 1 else body,
                "answered_questions": [],
                "unavailable_questions": [],
                "addressed_constraints": [],
                "unavailable_constraints": [],
                "referenced_mail_ids": [],
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    text = rfq.compose_quote_text(request, option, 120000, None, 7)
    assert [purpose for purpose, _ in calls] == ["compose_quote", "compose_quote_repair"]
    assert all(system.endswith(profile) for _, system in calls)
    assert "1.200,00 €" in text and "1.300,00" not in text
    assert "Test Air" in text and "FRA" in text and "DEL" in text


def test_profile_is_loaded_from_package_not_working_directory(tmp_path, monkeypatch):
    expected = customer_voice()
    customer_voice.cache_clear()
    monkeypatch.chdir(tmp_path)
    assert customer_voice() == expected
