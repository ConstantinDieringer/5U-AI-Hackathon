"""Regression: the Kessler BOM thread (21:59, RE: FW: BRUSSELS AIRPORT - PPT / 104 PCS 5,390 KGS).

Production: question 1 asked for the DG documents and the weight, the customer promised the
documents, question 2 asked for the weight, the customer answered "... is 1,674 kg", the
weight stayed open, the two-question cap suppressed everything and the round got no mail.

The thread is not in any database here. The customer mails below are verbatim as shown in
the thread; the packing specification is rebuilt in 5U's XLSX layout (mail 2256) with the
column order that reproduces the first question (see tests/test_totals_same_line.py); the
carrier bids are placeholders (the round's real bids are not stored). Everything runs
through the real flow with stubbed mail, portal and gateway; nothing leaves the machine.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import db
from app.agents import clarification
from tests.test_recent_rounds_e2e import ANSWER_41, World, _quote, build_world
from tests.test_totals_same_line import (
    KESSLER,
    KESSLER_FACTS,
    KESSLER_RFQ,
    KESSLER_SUBJECT,
    packing_rows,
)

ANSWER_1 = (  # 21:59:31, verbatim
    "Thank you, good point. We will send you the dangerous goods documents (shipper's "
    "declaration and safety data sheet) shortly.\n\n"
    "Mit freundlichen Gruessen / Kind regards\nJonas Kessler\nLogistics Coordinator\n"
    "Kessler Feinmechanik GmbH, Ulm"
)
ANSWER_2 = (  # 21:59:50, verbatim
    "Hello Moritz,\n\nThe total gross weight for our shipment to BOM is 1,674 kg.\n\n"
    "Mit freundlichen Gruessen / Kind regards\nJonas Kessler\nLogistics Coordinator\n"
    "Kessler Feinmechanik GmbH, Ulm"
)
HEADER_LAST = ("Pieces", "Length (cm)", "Width (cm)", "Height (cm)", "Gross weight (kg)")
KESTREL = "kestrel-airways@hackloop.mail"
NECKARSTROM = "neckarstrom-kurier@hackloop.mail"
BIDS = {  # placeholders
    KESTREL: "Our all-in price is EUR 4120.00, transit time 3 days.",
    NECKARSTROM: "Our all-in price is EUR 210.00, pickup within 1 day.",
}
SID = 21_959
OFF = {"totals_same_line": False, "thousands_evidence": False, "clarify_cap_resolve": False}


def _world(tmp_path, monkeypatch, **flags: bool) -> World:
    w = build_world(tmp_path, monkeypatch, **{**OFF, **flags})
    w.extraction = KESSLER_FACTS
    w.replies = dict(BIDS)
    return w


def _run_thread(w: World, rows: list[tuple[Any, ...]] | None = None) -> list[dict[str, Any]]:
    """The customer answers each of our questions in order, as the 5U customer agent did."""
    w.packing_spec(rows or packing_rows(HEADER_LAST))
    root = w.rfq(SID, KESSLER, KESSLER_SUBJECT, KESSLER_RFQ, attachments=1)
    for answer in (ANSWER_1, ANSWER_2):
        questions = [m for m in w.to(KESSLER) if (_quote(SID) or {}).get("mail_id") != m["id"]]
        if _quote(SID) is not None or len(questions) == 0:
            break
        if answer is ANSWER_2 and len(questions) < 2:
            break  # no second question, so no second answer
        w.answer(root, answer)
    return w.to(KESSLER)


def _kinds() -> list[str]:
    return [e["kind"] for e in db.get_session(SID)["events"]]


def test_production_today_two_questions_then_silence(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    mails = _run_thread(w)
    assert _quote(SID) is None
    assert len(mails) == 2
    assert "UN1760" in mails[0]["body"] and "gross weight" in mails[0]["body"]
    assert "gross weight" in mails[1]["body"] and "UN1760" not in mails[1]["body"]
    assert "clarification_loop" in _kinds()
    assert w.network_blocked == 0


@pytest.mark.parametrize(
    ("flags", "questions"),
    [
        ({"totals_same_line": True}, 1),  # no false conflict: only the DG question
        ({"thousands_evidence": True}, 2),  # the second answer is read as 1674 kg
        ({"clarify_cap_resolve": True}, 2),  # the cap takes the thread's one plausible weight
        ({"totals_same_line": True, "thousands_evidence": True, "clarify_cap_resolve": True}, 1),
    ],
    ids=["same_line", "thousands", "cap_resolve", "all"],
)
def test_thread_ends_with_a_quote_on_1674_kg(tmp_path, monkeypatch, flags, questions):
    w = _world(tmp_path, monkeypatch, **flags)
    mails = _run_thread(w)
    quote = _quote(SID)
    assert quote is not None, _kinds()[-6:]
    assert len(mails) == questions + 1 and mails[-1]["id"] == quote["mail_id"]
    assert len(mails) - 1 <= 2
    assert db.get_session(SID)["request"]["weight_kg"] == 1674.0
    # what the airline and the trucker were asked to price
    for carrier in (KESTREL, NECKARSTROM):
        assert all("1674 kg gross" in m["body"] for m in w.to(carrier)), w.to(carrier)
    assert "UN1760" in mails[0]["body"]  # the DG question was asked before the quote
    assert w.network_blocked == 0


# ---------------------------------------------------------------- round 41, pinned


@pytest.mark.parametrize(
    "promise",
    [
        ANSWER_1,
        ANSWER_41,  # round 41 (mail 10580), accepted by 5U after only this promise
    ],
    ids=["kessler", "round_41"],
)
def test_round_41_a_promise_of_the_dg_documents_is_enough_to_quote(tmp_path, monkeypatch, promise):
    """Ask for DGD and SDS, then quote: that is the rule 5U scores. Commit 6fdc811 made the
    gate wait for attached documents and 26b5b9b reverted it a minute later because round 41
    was accepted after a promise alone. If this fails, someone made the gate wait again."""
    w = _world(tmp_path, monkeypatch, totals_same_line=True)
    w.packing_spec(packing_rows(HEADER_LAST))
    root = w.rfq(SID, KESSLER, KESSLER_SUBJECT, KESSLER_RFQ, attachments=1)
    assert _quote(SID) is None
    question = w.to(KESSLER)
    assert len(question) == 1
    assert "DGD" in question[0]["body"] and "SDS" in question[0]["body"]
    w.attachments.clear()  # the answer carries no documents
    w.answer(root, promise)
    assert _quote(SID) is not None, _kinds()[-6:]
    assert len(w.to(KESSLER)) == 2
    draft = db.validation_get(SID)["draft"]
    assert draft["evidence"]["corrections"]["dangerous_goods"] == "dg_documents_received"


# ---------------------------------------------------------------- cap, nothing usable

UNSURE = "Hello,\n\nthanks, we are checking this with our warehouse.\n\nKind regards\nJonas"


def test_cap_without_a_usable_figure_sends_nothing_and_flags_first(tmp_path, monkeypatch):
    """Mail 1674 kg, packing list total 1500 kg (both plausible), two answers that settle
    nothing: no third mail, no quote, an error event and the first "Zu tun" entry."""
    w = _world(
        tmp_path,
        monkeypatch,
        totals_same_line=True,
        thousands_evidence=True,
        clarify_cap_resolve=True,
    )
    rows = [r for r in packing_rows(HEADER_LAST) if not (r and r[0] == "Dangerous goods:")]
    rows = [
        ("Total gross weight (kg):", 1500) if r[:1] == ("Total gross weight (kg):",) else r
        for r in rows
    ]
    w.packing_spec(rows)
    root = w.rfq(SID, KESSLER, KESSLER_SUBJECT, KESSLER_RFQ, attachments=1)
    w.answer(root, UNSURE)
    w.answer(root, UNSURE)
    mails = w.to(KESSLER)
    assert len(mails) == 2 and _quote(SID) is None
    assert all("gross weight" in m["body"] for m in mails)
    events = [e for e in db.get_session(SID)["events"] if e["kind"] == "clarification_cap_open"]
    assert len(events) == 1 and events[0]["level"] == "error"
    assert events[0]["message"].endswith("Offen: Gewicht")
    top = db.attention_items()[0]
    assert (top["session_id"], top["kind"]) == (SID, "clarification_cap_open")
    assert "Gewicht" in top["reason"]
    todo = db.status_overview()["todo"]
    assert todo and todo[0]["session_id"] == SID and todo[0]["tone"] == "red"
    assert db.validation_get(SID)["state"] == "manual_review"
    assert clarification.asked_fields(SID) == {"weight_kg"}
    assert w.network_blocked == 0
