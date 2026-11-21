"""THOUSANDS_EVIDENCE in the rescue pass: a model value "1,674" is not 1.674 kg.

Kessler BOM thread, second answer: the evidence-bound rescue model may copy the customer's
"1,674" verbatim; `_rescue_value` read it as 1.674 kg. With the setting on, the value goes
through the same weight_reading as the mail: the shipment's known 1674 kg decides.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app import config, db
from app.agents import clarification
from app.mail.rfq_validation import validate
from app.models import Mail, Rfq

CUSTOMER = "kessler-feinmechanik@hackloop.mail"
RFQ = (
    "Dear Sir or Madam,\n\nCould you please quote the cheapest air freight option to BOM "
    "(Mumbai, IN)? Please arrange truck pickup from our site in 73342 (Esslingen), Germany.\n\n"
    "It consists of 11 pieces of cleaning agent, with a gross weight of 1674 kg and a total "
    "volume of 4.36 cbm. The packing specification is attached.\n\nKind regards\nJonas Kessler"
)
ANSWER = (
    "Hello Moritz,\n\nThe total gross weight for our shipment to BOM is 1,674 kg.\n\n"
    "Mit freundlichen Gruessen / Kind regards\nJonas Kessler"
)


def _setup(tmp_path, monkeypatch, on: bool, attachment_weight: str):
    db.init_db(str(tmp_path / "rescue.db"))
    settings = dataclasses.replace(
        config.settings, rfq_validation_mode="enforce", thousands_evidence=on
    )
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(clarification, "settings", settings)
    now = datetime.now(UTC).isoformat()
    root = Mail(1, 1, CUSTOMER, "team", "RE: FW: BRUSSELS AIRPORT - PPT", RFQ, now)
    db.ingest_mail(
        mail_id=1,
        sender=CUSTOMER,
        recipient="team",
        subject=root.subject,
        body=RFQ,
        category="rfq",
        received_at=now,
    )
    request = Rfq(pickup_zip="73342", destination="BOM", weight_kg=1674, volume_cbm=4.36, pieces=11)
    # a weight conflict as in the thread: mail 1674 kg, attachment figure elsewhere
    texts = {"spec.xlsx": f"Total gross weight (kg): | {attachment_weight}"}
    _, draft = validate(root, request, texts)
    draft.evidence["attachment_texts"] = texts  # as rfq.handle_rfq stores them
    assert {i["field"] for i in draft.issues} == {"weight_kg"}
    answer = Mail(2, 1, CUSTOMER, "team", "RE: BOM", ANSWER, now, references=[1])
    monkeypatch.setattr(
        clarification.llm,
        "complete",
        lambda *a, **k: json.dumps(
            {"corrections": [{"field": "weight_kg", "value": "1,674", "evidence": "1,674 kg"}]}
        ),
    )
    return root, answer, draft


def test_today_the_model_value_becomes_1_674_kg(tmp_path, monkeypatch):
    root, answer, draft = _setup(tmp_path, monkeypatch, on=False, attachment_weight="11")
    rfq, _ = clarification.rescue_with_model(answer, 1, root, draft)
    assert rfq is not None and rfq.weight_kg == 1.674  # the defect, pinned with the setting off


def test_known_figure_reads_the_model_value_as_thousands(tmp_path, monkeypatch):
    root, answer, draft = _setup(tmp_path, monkeypatch, on=True, attachment_weight="11")
    rfq, checked = clarification.rescue_with_model(answer, 1, root, draft)
    assert rfq is not None and rfq.weight_kg == 1674.0
    assert checked.evidence["corrections"]["weight_kg"] == 1674.0


def test_no_matching_figure_and_nothing_implausible_stays_unresolved(tmp_path, monkeypatch):
    """Known figures 1674 (mail) and 1500 (attachment) do not include the answer's 1764."""
    root, answer, draft = _setup(tmp_path, monkeypatch, on=True, attachment_weight="1500")
    monkeypatch.setattr(
        clarification.llm,
        "complete",
        lambda *a, **k: json.dumps(
            {"corrections": [{"field": "weight_kg", "value": "1,764", "evidence": "1,674 kg"}]}
        ),
    )
    rfq, _ = clarification.rescue_with_model(answer, 1, root, draft)
    assert rfq is None


@pytest.mark.parametrize(
    ("value", "known", "facts", "want"),
    [
        ("1,674", (Decimal(1674),), {}, 1674.0),
        ("1.674", (), {"pieces": 11}, 1674.0),
        ("1,674", (), {"pieces": 1}, None),
        ("1,674", (Decimal(1500),), {"pieces": 11}, None),
        ("1674", (), {}, 1674.0),
        ("12,5", (), {}, 12.5),
        ("1674 kg", (), {}, None),
        ("0", (), {}, None),
    ],
)
def test_rescue_value(monkeypatch, value, known, facts, want):
    settings = dataclasses.replace(config.settings, thousands_evidence=True)
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(clarification, "settings", settings)
    assert clarification._rescue_value("weight_kg", value, known, facts) == want
    assert clarification._rescue_value("volume_cbm", "4,36") == 4.36
