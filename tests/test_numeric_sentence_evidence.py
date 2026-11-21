"""Labelled totals in ordinary sentences are evidence, not a reason to ask again."""

from decimal import Decimal

import pytest

from app.mail.rfq_validation import numeric_facts, validate
from app.models import Mail, Rfq


@pytest.mark.parametrize("connector", ["is", "of", ":", "|"])
def test_numeric_sentence_and_label_forms(connector):
    facts = numeric_facts(
        f"Total gross weight {connector} 1234 kg. Total volume {connector} 2.5 cbm."
    )
    assert facts["weight_kg"] == Decimal(1234)
    assert facts["volume_cbm"] == Decimal("2.5")


def test_explicit_current_weight_does_not_trigger_redundant_clarification():
    body = (
        "Please arrange the truck from our site in 41007 to ATL. "
        "The gross weight is 1754 kg and the total volume is 6.83 cbm. 10 pieces."
    )
    mail = Mail(
        1, 1, "customer@example.com", "team", "ATL", body, "2026-11-21T17:20:00Z"
    )
    candidate = Rfq(destination="ATL", weight_kg=1754, volume_cbm=6.83, pieces=10)
    result, draft = validate(mail, candidate)
    assert result.weight_kg == 1754
    assert draft.evidence["weight_kg"]["source"] == "body"
    assert not draft.issues


@pytest.mark.parametrize(
    "text",
    ["gross weight is not 500 kg", "net weight is 500 kg", "gross weight is unknown"],
)
def test_non_gross_or_unconfirmed_sentence_is_not_evidence(text):
    assert "weight_kg" not in numeric_facts(text)


@pytest.mark.parametrize("separator", [": | ", " | ", ": ", "|: "])
def test_serialized_table_label_separators(separator):
    text = (
        f"Total gross weight (kg){separator}840\n"
        f"Total volume (cbm){separator}3.25\nTotal pieces{separator}7"
    )
    assert numeric_facts(text) == {
        "weight_kg": Decimal(840),
        "volume_cbm": Decimal("3.25"),
        "pieces": Decimal(7),
    }


@pytest.mark.parametrize(
    "field,attachment",
    [
        ("weight_kg", "Total gross weight (kg): | 840"),
        ("volume_cbm", "Total volume (cbm): | 3.25"),
        ("pieces", "Total pieces: | 7"),
    ],
)
def test_table_conflict_requires_confirmation_even_if_model_chose_attachment(
    field, attachment
):
    body = (
        "Please quote FRA to DEL. We deliver to FRA ourselves. "
        "Total gross weight: 920 kg. Total volume: 4.5 cbm. 9 pieces."
    )
    mail = Mail(
        1, 1, "customer@example.com", "team", "FRA to DEL", body, "2026-11-21T18:00:00Z"
    )
    values = {"weight_kg": 840, "volume_cbm": 3.25, "pieces": 7}
    request = Rfq(
        origin="FRA", destination="DEL", weight_kg=920, volume_cbm=4.5, pieces=9
    )
    request = request.model_copy(update={field: values[field]})
    _, draft = validate(mail, request, {"packing.xlsx": attachment})
    assert any(
        issue["field"] == field and issue["code"] == "conflict"
        for issue in draft.issues
    )
    checked, corrected = validate(
        mail, request, {"packing.xlsx": attachment}, corrections={field: values[field]}
    )
    assert not corrected.issues
    assert getattr(checked, field) == values[field]


@pytest.mark.parametrize(
    "text",
    [
        "Total gross weight (kg): | unknown\n840",
        "Net weight (kg): | 840",
        "Total gross weight (kg): | not 840",
    ],
)
def test_table_separator_does_not_turn_unknown_or_net_into_gross(text):
    assert "weight_kg" not in numeric_facts(text)
