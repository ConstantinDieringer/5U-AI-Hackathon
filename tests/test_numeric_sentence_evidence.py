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
