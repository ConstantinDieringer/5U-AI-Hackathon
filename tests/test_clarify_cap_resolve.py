"""CLARIFY_CAP_RESOLVE: two questions spent and still open never ends silently.

Kessler BOM thread: after the second answer the weight was still open, the cap suppressed a
third question (`clarification_loop`) and the round ended without any mail. With the setting
on, the cap uses the one plausible figure of the thread, else the best quote from what the
mail states (dangerous goods only once we asked for the documents, the rule 5U scores), else
it sends nothing and puts the session first in "Zu tun" with the open items named.
Runs through the real flow with stubbed mail, portal and gateway (tests/test_recent_rounds_e2e).
"""

from __future__ import annotations

import pytest

from app import db
from app.agents import clarification
from app.models import Mail, RfqDraft
from tests.test_recent_rounds_e2e import (
    ROUND_39,
    ROUND_39_CUSTOMER,
    ROUND_39_FACTS,
    World,
    _quote,
    build_world,
)

NO_ANSWER = "Hello,\n\nthanks, noted.\n\nBest regards,\nLena Seidel"


def _dg_thread(w: World, *, forget_dg_asked: bool = False) -> int:
    """Round 39's request; the customer twice answers without addressing the DG question."""
    w.extraction = ROUND_39_FACTS
    w.packing_spec(
        [
            ("Total gross weight (kg):", 378),
            ("Total volume (cbm):", 1.86),
            ("Dangerous goods:", "UN3481, Class 9, lithium ion batteries packed with equipment"),
        ]
    )
    root = w.rfq(10132, ROUND_39_CUSTOMER, "Air freight FRA to MIA", ROUND_39, attachments=1)
    assert len(w.to(ROUND_39_CUSTOMER)) == 1 and "DGD" in w.to(ROUND_39_CUSTOMER)[0]["body"]
    w.answer(root, NO_ANSWER)
    assert len(w.to(ROUND_39_CUSTOMER)) == 2  # today's second question, DG again
    if forget_dg_asked:
        db.kv_set("clarify.asked.10132", "[]")
    w.answer(root, NO_ANSWER)
    return 10132


def _kinds(sid: int) -> list[str]:
    return [e["kind"] for e in db.get_session(sid)["events"]]


def test_today_the_cap_ends_silently(tmp_path, monkeypatch):
    w = build_world(tmp_path, monkeypatch, clarify_cap_resolve=False)
    sid = _dg_thread(w)
    assert _quote(sid) is None and len(w.to(ROUND_39_CUSTOMER)) == 2
    assert "clarification_loop" in _kinds(sid)
    assert "clarification_cap_open" not in _kinds(sid)
    assert w.network_blocked == 0


def test_dg_asked_then_quote_at_the_cap(tmp_path, monkeypatch):
    """The scorer's rule (round 41): ask for the DG documents, then quote."""
    w = build_world(tmp_path, monkeypatch, clarify_cap_resolve=True)
    sid = _dg_thread(w)
    quote = _quote(sid)
    assert quote is not None, _kinds(sid)[-6:]
    mails = w.to(ROUND_39_CUSTOMER)
    assert len(mails) == 3 and mails[2]["id"] == quote["mail_id"]  # 2 questions, 1 quote
    assert db.get_session(sid)["request"]["weight_kg"] == 378.0
    assert clarification.asked_fields(sid) >= {"dangerous_goods"}
    assert w.network_blocked == 0


def test_dg_never_asked_is_never_quoted(tmp_path, monkeypatch):
    w = build_world(tmp_path, monkeypatch, clarify_cap_resolve=True)
    sid = _dg_thread(w, forget_dg_asked=True)
    assert _quote(sid) is None and len(w.to(ROUND_39_CUSTOMER)) == 2
    assert "clarification_cap_open" in _kinds(sid)
    top = db.attention_items()[0]
    assert top["session_id"] == sid and top["kind"] == "clarification_cap_open"
    assert "Gefahrgut-Unterlagen" in top["reason"]
    assert db.validation_get(sid)["state"] == "manual_review"
    assert db.validation_get(sid)["send_state"] == "capped"


# ---------------------------------------------------------------- the thread's one figure

BODY = (
    "Please arrange truck pickup from our site in 73342 Esslingen. 11 pieces, gross weight "
    "of 1674 kg, total volume of 4.36 cbm."
)


def _draft(observations: list[tuple[str, str]]) -> RfqDraft:
    return RfqDraft(
        facts={"weight_kg": 1674.0, "volume_cbm": 4.36, "pieces": 11},
        evidence={"weight_kg": {"observations": observations}},
        issues=[{"field": "weight_kg", "code": "conflict"}],
    )


@pytest.fixture
def thread(tmp_path):
    db.init_db(str(tmp_path / "figure.db"))
    db.ingest_mail(
        mail_id=1,
        sender="c@example.com",
        recipient="team",
        subject="BOM",
        body=BODY,
        category="rfq",
        received_at="2026-11-21T20:59:11+00:00",
    )
    root = Mail(1, 1, "c@example.com", "team", "BOM", BODY, "2026-11-21T20:59:11+00:00")
    db.validation_save(1, "awaiting_clarification", {}, root.__dict__)

    def answer(body: str) -> Mail:
        return Mail(3, 1, "c@example.com", "team", "Re: BOM", body, "2026-11-21T21:00:00+00:00")

    return answer


@pytest.mark.parametrize(
    ("observations", "answer", "want"),
    [
        # the header misread (11) and the decimal reading (1.674) are ruled out by 11 pieces
        # in 4.36 cbm: only 1674 is left
        ([("body", "1674"), ("spec.xlsx", "11")], "The weight is 1,674 kg.", 1674.0),
        ([("body", "1674"), ("spec.xlsx", "11")], "Thanks, we will check.", 1674.0),
        # two plausible weights: no figure, no guess
        ([("body", "1674"), ("spec.xlsx", "1500")], "Thanks, we will check.", None),
        ([("body", "1674")], "Sorry, it is 1,764 kg.", None),
        ([("body", "1674")], "Each piece is 152 kg.", None),
    ],
)
def test_thread_figure(thread, observations, answer, want):
    mail = thread(answer)
    assert clarification.thread_figure("weight_kg", _draft(observations), 1, mail) == want


def test_thread_figure_volume(thread):
    draft = RfqDraft(
        facts={"weight_kg": 1674.0, "volume_cbm": 4.36},
        evidence={"volume_cbm": {"observations": [("body", "4.36"), ("spec.xlsx", "4.36")]}},
        issues=[{"field": "volume_cbm", "code": "missing"}],
    )
    assert clarification.thread_figure("volume_cbm", draft, 1, thread("ok")) == 4.36
    assert clarification.thread_figure("volume_cbm", draft, 1, thread("No, 5.2 cbm.")) is None
