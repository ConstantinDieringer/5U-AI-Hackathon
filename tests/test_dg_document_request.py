"""Dangerous-goods clarification asks for documents without releasing the quote gate."""

import pytest

from app.agents.clarification import text_for
from app.mail.rfq_validation import validate
from app.models import Mail, Rfq, RfqDraft


@pytest.mark.parametrize("lang", ["en", "de"])
def test_dg_question_explicitly_requests_documents_and_prior_review(lang):
    draft = RfqDraft(
        issues=[
            {"field": "dangerous_goods", "code": "conflict_or_unsupported"},
            {"field": "pickup_zip", "code": "conflict"},
        ]
    )
    text = text_for(draft, lang)
    assert "DGD" in text and "SDS" in text and "UN" in text
    assert ("postal code" if lang == "en" else "Postleitzahl") in text
    assert "(" not in text and ")" not in text
    assert ("before we can quote or book" if lang == "en" else "bevor wir ein Angebot") in text
    assert "quote right away" not in text


def test_correct_packing_totals_do_not_clear_dg_evidence():
    mail = Mail(
        1,
        1,
        "customer@example.com",
        "team",
        "FRA to MIA",
        "Please quote FRA to MIA. 240 kg gross, total volume 2 cbm, 4 pieces.",
        "2026-11-21T18:00:00Z",
    )
    candidate = Rfq(origin="FRA", destination="MIA", weight_kg=240, volume_cbm=2, pieces=4)
    _, draft = validate(
        mail,
        candidate,
        {"packing.pdf": "Dangerous goods: UN3481, Class 9"},
        corrections={"weight_kg": 240, "volume_cbm": 2, "pieces": 4},
    )
    assert any(issue["field"] == "dangerous_goods" for issue in draft.issues)
    assert "shipper's declaration" in text_for(draft, "en")
    assert "DGD" in text_for(draft, "en") and "SDS" in text_for(draft, "en")


def test_ordinary_missing_weight_does_not_request_dg_documents():
    draft = RfqDraft(issues=[{"field": "weight_kg", "code": "missing"}])
    assert "DGD" not in text_for(draft, "en")
