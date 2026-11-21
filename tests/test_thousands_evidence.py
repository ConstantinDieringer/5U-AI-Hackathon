"""THOUSANDS_EVIDENCE: "1,674 kg" is 1674 kg when the shipment says so, never 1.674 kg.

Kessler BOM thread: the customer answered our weight question with "The total gross weight
for our shipment to BOM is 1,674 kg." numeric_facts skipped the figure as ambiguous, the
CLARIFY_ONCE free-text rule and the rescue model's value both turned it into 1.674 kg. One
rule, weight_reading, now decides in all four places; with the setting off every result is
today's (pinned below, including today's 1.674).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from app import config
from app.agents import clarification
from app.mail.rfq_validation import free_text_answer, numeric_facts, validate, weight_reading
from app.models import Mail, Rfq

ANSWER = "The total gross weight for our shipment to BOM is 1,674 kg."


@pytest.fixture
def flags(monkeypatch):
    def set_(**changes: bool) -> None:
        settings = dataclasses.replace(config.settings, **changes)
        monkeypatch.setattr(config, "settings", settings)
        monkeypatch.setattr(clarification, "settings", settings)

    return set_


K = Decimal  # a known figure of the shipment

# (text, known figures, today's result, result with THOUSANDS_EVIDENCE)
CASES = [
    ("Gross weight: 1,674 kg", (K(1674),), {}, {"weight_kg": K(1674)}),
    ("Gross weight: 1.674 kg", (K(1674),), {}, {"weight_kg": K(1674)}),
    ("Total gross weight (kg): | 1,674", (K(1674),), {}, {"weight_kg": K(1674)}),
    # no known figure: several pieces / pallets / a volume make 1.674 kg implausible
    (
        "Gross weight: 1,674 kg, 11 pieces",
        (),
        {"pieces": K(11)},
        {"weight_kg": K(1674), "pieces": K(11)},
    ),
    ("2 pallets, gross weight: 1.674 kg", (), {}, {"weight_kg": K(1674)}),
    (
        "Gross weight: 1,674 kg, total volume 4.36 cbm",
        (),
        {"volume_cbm": K("4.36")},
        {"weight_kg": K(1674), "volume_cbm": K("4.36")},
    ),
    # stays ambiguous
    ("Gross weight: 1,674 kg", (), {}, {}),  # nothing makes either reading implausible
    ("Gross weight: 1,674 kg, 1 piece", (), {}, {}),
    (
        "Gross weight: 1,674 kg, total volume 0.01 cbm",
        (),
        {"volume_cbm": K("0.01")},
        {"volume_cbm": K("0.01")},
    ),
    ("Gross weight: 1,674 kg", (K(1500),), {}, {}),  # a known figure that disagrees
    ("Gross weight: 1,674", (K(1674),), {}, {}),  # no unit
    # must not change
    ("Gross weight: 12,5 kg", (), {"weight_kg": K("12.5")}, {"weight_kg": K("12.5")}),
    ("Total volume: 0,08 cbm", (), {"volume_cbm": K("0.08")}, {"volume_cbm": K("0.08")}),
    ("Gross weight: 1.228,5 kg", (K("1228.5"),), {}, {}),
    ("Gross weight: 2,5 t", (), {"weight_kg": K(2500)}, {"weight_kg": K(2500)}),
    ("Gross weight: 1674 kg", (), {"weight_kg": K(1674)}, {"weight_kg": K(1674)}),
]


@pytest.mark.parametrize(("text", "known", "today", "on"), CASES)
def test_numeric_facts(flags, text, known, today, on):
    flags(thousands_evidence=False)
    assert numeric_facts(text, known=known) == today
    flags(thousands_evidence=True)
    assert numeric_facts(text, known=known) == on


def test_extraction_is_a_known_figure(flags):
    flags(thousands_evidence=True)
    assert numeric_facts("Gross weight: 1,674 kg", expect={"weight_kg": 1674.0}) == {
        "weight_kg": K(1674)
    }
    assert numeric_facts("Gross weight: 1,674 kg", expect={"weight_kg": 1.674}) == {}


def test_unit_evidence_keeps_its_agreement_rule_off_and_on(flags):
    """UNIT_EVIDENCE's own two-reading rule is now weight_reading; off it is unchanged."""
    for on in (False, True):
        flags(unit_evidence=True, thousands_evidence=on)
        assert numeric_facts("1,228 kg gross", expect={"weight_kg": 1228}) == {"weight_kg": K(1228)}
        assert numeric_facts("1,228 lbs gross", expect={"weight_kg": 557.0})["weight_kg"] == (
            K("1228") * K("0.45359237")
        )
        assert numeric_facts("1,228 kg gross", expect={"weight_kg": 900}) == {}


def _kessler(body: str) -> Mail:
    return Mail(1, 1, "c@example.com", "team", "BOM", body, "2026-11-21T20:59:11Z")


def test_validate_reads_the_mail_and_attachment_with_known_figures(flags):
    body = (
        "Please arrange truck pickup from our site in 73342 Esslingen. Cheapest to BOM. "
        "11 pieces, gross weight of 1,674 kg, total volume of 4.36 cbm."
    )
    request = Rfq(pickup_zip="73342", destination="BOM", weight_kg=1674, volume_cbm=4.36, pieces=11)
    attachment = {"spec.xlsx": "Total gross weight (kg): | 1.674\nTotal volume (cbm): | 4.36"}
    flags(thousands_evidence=False, unit_evidence=False)
    _, draft = validate(_kessler(body), request, attachment)
    assert ("weight_kg", "missing") in {(i["field"], i["code"]) for i in draft.issues}
    # UNIT_EVIDENCE (default true since main's defaults change) reads the mail's figure when
    # the extraction agrees, but nothing reads the unit-less attachment figure
    flags(thousands_evidence=False, unit_evidence=True)
    _, draft = validate(_kessler(body), request, attachment)
    assert not draft.issues
    assert draft.evidence["weight_kg"]["observations"] == [("body", "1674")]
    for unit_evidence in (False, True):
        flags(thousands_evidence=True, unit_evidence=unit_evidence)
        _, draft = validate(_kessler(body), request, attachment)
        assert not draft.issues
    assert draft.evidence["weight_kg"]["observations"] == [
        ("body", "1674"),
        ("spec.xlsx", "1674"),
    ]


def test_free_text_answer(flags):
    flags(thousands_evidence=False)  # today, CLARIFY_ONCE only: the 1.674 kg bug
    assert free_text_answer(ANSWER, {"weight_kg"}, "BOM") == {"weight_kg": 1.674}
    flags(thousands_evidence=True)
    assert free_text_answer(ANSWER, {"weight_kg"}, "BOM", known=(K(1674),)) == {"weight_kg": 1674.0}
    assert free_text_answer(ANSWER, {"weight_kg"}, "BOM", facts={"pieces": 11}) == {
        "weight_kg": 1674.0
    }
    assert free_text_answer(ANSWER, {"weight_kg"}, "BOM", known=(K(1500),)) == {}
    assert free_text_answer("It is 12,5 kg.", {"weight_kg"}, "BOM") == {"weight_kg": 12.5}


def test_weight_reading_never_guesses_a_multi_group_figure(flags):
    flags(thousands_evidence=True)
    assert weight_reading("1,228,000", facts={"pieces": 4}) is None
    assert weight_reading("1,228,000", known=(K(1228000),)) == K(1228000)
