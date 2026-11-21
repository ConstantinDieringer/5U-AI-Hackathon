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
    assert "DGD" in text and "SDS" in text and "UN" in text and "Pickup ZIP" in text
    assert (
        "before we can quote or book" if lang == "en" else "bevor wir ein Angebot"
    ) in text
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
    candidate = Rfq(
        origin="FRA", destination="MIA", weight_kg=240, volume_cbm=2, pieces=4
    )
    _, draft = validate(
        mail,
        candidate,
        {"packing.pdf": "Dangerous goods: UN3481, Class 9"},
        corrections={"weight_kg": 240, "volume_cbm": 2, "pieces": 4},
    )
    assert any(issue["field"] == "dangerous_goods" for issue in draft.issues)
    assert "dangerous goods documents" in text_for(draft, "en")


def test_ordinary_missing_weight_does_not_request_dg_documents():
    draft = RfqDraft(issues=[{"field": "weight_kg", "code": "missing"}])
    assert "DGD" not in text_for(draft, "en")


def test_dg_announcement_waits_and_next_mail_with_documents_is_consumed(tmp_path, monkeypatch):
    from app import db
    from app.agents import clarification

    db.init_db(str(tmp_path / "dg.db"))
    sid = db.ingest_mail(mail_id=100, sender="c@x.example", recipient="t", subject="RFQ",
                         body="b", category="rfq")
    db.validation_save(sid, "awaiting_clarification",
                       {"issues": [{"field": "dangerous_goods"}], "evidence": {}, "facts": {}},
                       {"id": 100, "thread_root": 100, "sender": "c@x.example", "to": "t",
                        "subject": "RFQ", "body": "b", "received_at": db.now()})
    db.clarification_claim(sid)
    db.clarification_sent(sid, "sent", 101)
    assert db.clarification_reply_claim(sid, 102)
    db.clarification_wait(sid)
    assert db.validation_get(sid)["state"] == "awaiting_clarification"
    assert db.clarification_reply_claim(sid, 103)  # the mail with the documents
