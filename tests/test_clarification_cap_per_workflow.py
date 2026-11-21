"""The cap of two clarifications counts per workflow, not per thread.

Before: one RFQ question (pickup ZIP) plus one booking question ("shall we book?") used up
the thread's two questions. The next hesitant reply raised an error-level
`clarification_loop` ("Weitere Rückfrage unterdrückt: Session braucht einen Menschen") and a
#sos alert, although no round was at risk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app import db, hackloop
from app.agents import booking, clarification
from app.models import Mail
from tests.test_booking import CUSTOMER, TEAM, _quoted_session

SID = 1223
HESITATE_1 = "Thanks, we will look at it internally."
HESITATE_2 = "Thanks, I'll check whether we can proceed and get back to you."
HESITATE_3 = "Still checking internally, will come back to you."


@pytest.fixture
def sent(tmp_path, monkeypatch) -> list[str]:
    db.init_db(str(tmp_path / "test.db"))
    out: list[str] = []

    def send(to: str, subject: str, body: str, **_k: Any) -> dict[str, Any]:
        out.append(body)
        return {"id": 2000 + len(out), "subject": subject, "body": body}

    monkeypatch.setattr(hackloop, "send_customer", send)
    _quoted_session(SID)
    return out


def _rfq_question(mail_id: int = 8000) -> None:
    db.record_outgoing(
        mail_id=mail_id,
        recipient=CUSTOMER,
        subject="Re: Fastest STR-EZE",
        body="Could you send the postal code of the collection address?",
        category="clarification",
        session_id=SID,
        in_reply_to=SID,
        references=[SID],
        raw={},
    )


def _reply(mid: int, body: str) -> None:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Re: Fastest STR-EZE",
        body=body,
        category="customer",
        received_at=now,
        in_reply_to=SID,
        references=[SID],
    )
    mail = Mail(mid, SID, CUSTOMER, TEAM, "Re: Fastest STR-EZE", body, now, references=[SID])
    if not clarification.handle_reply(mail, SID):  # same order as the router
        booking.handle_reply(mail, SID)


def _loops() -> list[dict[str, Any]]:
    return [e for e in db.get_session(SID)["events"] if e["kind"] == "clarification_loop"]


def test_rfq_question_does_not_use_up_booking_questions(sent) -> None:
    _rfq_question()
    _reply(3000, HESITATE_1)
    _reply(3001, HESITATE_2)
    assert len(sent) == 2  # both booking questions go out
    assert _loops() == []
    assert clarification._questions_sent(SID, "booking") == 2
    assert clarification._questions_sent(SID, "rfq") == 1


def test_booking_cap_is_a_warning_not_an_error(sent) -> None:
    _reply(3000, HESITATE_1)
    _reply(3001, HESITATE_2)
    _reply(3002, HESITATE_3)
    assert len(sent) == 2  # the cap still holds: no third booking question
    loops = _loops()
    assert len(loops) == 1 and loops[0]["level"] == "warn"


def test_booking_questions_do_not_use_up_rfq_questions(sent) -> None:
    _reply(3000, HESITATE_1)
    _reply(3001, HESITATE_2)
    _rfq_question(8001)
    assert clarification._questions_sent(SID, "rfq") == 1


def test_rfq_questions_still_count_toward_the_rfq_cap(sent) -> None:
    _rfq_question(8000)
    _rfq_question(8001)
    assert clarification._questions_sent(SID, "rfq") == 2
