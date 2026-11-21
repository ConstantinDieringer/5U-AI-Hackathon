"""Tracking instructions survive generation and a missing notice is repaired once."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db, hackloop, llm
from app.agents import tracking_notice
from app.mail.style import SIGNATURE
from app.mail.tracking_notice import NOTICES, ensure_notice, has_notice

CUSTOMER = "sara@example.test"
TEAM = "austrian-power@hackloop.mail"
SID = 500
LEGACY = (
    "We'll keep you posted. If you have any questions about the status, just reply to this mail."
)


@pytest.fixture(autouse=True)
def store(tmp_path):
    path = str(tmp_path / "notice.db")
    db.init_db(path)
    return path


def session(*, booked=False, body="Our quote is 100 €."):
    db.ingest_mail(
        mail_id=SID,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Your shipment",
        body="Please quote FRA to DEL",
        category="rfq",
    )
    db.record_outgoing(
        mail_id=501,
        recipient=CUSTOMER,
        subject="Your shipment",
        body=body,
        category="quote",
        session_id=SID,
        in_reply_to=SID,
    )
    db.record_quote(
        SID,
        total_minor=10000,
        currency="EUR",
        airline="Test Air",
        origin="FRA",
        destination="DEL",
        transit_days=2,
        source="sheet",
        mail_id=501,
    )
    if booked:
        db.record_booking(
            SID,
            booking_reference="BK-ABC123",
            rate_id=None,
            airline="Test Air",
            price_minor=10000,
            currency="EUR",
            status="confirmed",
        )


def transport(monkeypatch):
    sent = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: sent.append(kw) or {"id": 600})
    return sent


@pytest.mark.parametrize("booked", [False, True])
def test_customer_wire_has_tracking_notice_even_if_reviewer_removes_it(monkeypatch, booked):
    from app.agents import email_review

    session(booked=booked)
    monkeypatch.setattr(
        email_review,
        "review_customer_mail",
        lambda body, *a: body.replace(NOTICES[("en", booked)], ""),
    )
    sent = transport(monkeypatch)
    result = hackloop.send_customer(
        CUSTOMER, "Re: Your shipment", "Hi Sara,\n\n100 €.\n\n" + SIGNATURE, session_id=SID
    )
    assert result["body"] == sent[0]["body"]
    assert result["body"].count(NOTICES[("en", booked)]) == 1
    assert result["body"].index("tracking update") < result["body"].index(SIGNATURE)
    assert "100 €" in result["body"] and result["body"].endswith(SIGNATURE + "\n")


def test_carrier_gets_no_customer_promise(monkeypatch):
    session()
    sent = transport(monkeypatch)
    hackloop.send_customer(
        "carrier@example.test", "Rate", "Please quote", session_id=SID, review=False
    )
    assert "tracking update" not in sent[0]["body"]


def test_missing_notice_is_repaired_once_in_same_thread_without_llm(monkeypatch):
    session(booked=True)
    sent = transport(monkeypatch)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("No LLM needed"))
    assert tracking_notice.repair(SID)
    assert not tracking_notice.repair(SID)
    assert len(sent) == 1 and sent[0]["to"] == CUSTOMER and sent[0]["in_reply_to"] == SID
    assert NOTICES[("en", True)] in sent[0]["body"]
    stored = db.get_session(SID)
    assert stored["mails"][-1]["category"] == "tracking_notice"
    assert len(stored["quotes"]) == 1 and stored["quotes"][0]["total_minor"] == 10000


@pytest.mark.parametrize("control", ["pause", "global_pause", "manual", "decline", "no_quote"])
def test_controls_and_declines_never_receive_a_recovery(monkeypatch, control):
    session()
    if control == "pause":
        db.set_paused(SID, True)
    if control == "global_pause":
        db.set_global_pause(True)
    if control == "manual":
        db.takeover(SID)
    if control == "no_quote":
        db.set_status(SID, "error")
    if control == "decline":
        db.ingest_mail(
            mail_id=502,
            sender=CUSTOMER,
            recipient=TEAM,
            subject="No",
            body="Please do not proceed with this shipment",
            category="customer",
            references=[SID],
            in_reply_to=501,
        )
    sent = transport(monkeypatch)
    assert not tracking_notice.repair(SID) and not sent


def test_valid_legacy_notice_does_not_generate_a_new_mail(monkeypatch):
    session(booked=True, body=LEGACY)
    sent = transport(monkeypatch)
    assert has_notice(LEGACY)
    assert not tracking_notice.repair(SID) and not sent


def test_missing_local_confirmation_is_checked_by_its_referenced_original(monkeypatch):
    session(booked=True)
    db.ingest_mail(
        mail_id=503,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Thanks",
        body="Thanks for the booking confirmation",
        category="customer",
        references=[SID],
        in_reply_to=502,
    )
    checked = []
    monkeypatch.setattr(
        hackloop,
        "get_message",
        lambda mid: (
            checked.append(mid) or {"id": mid, "from": TEAM, "to": CUSTOMER, "body": LEGACY}
        ),
    )
    sent = transport(monkeypatch)
    assert not tracking_notice.repair(SID) and not sent and checked == [502]
    assert not tracking_notice.repair(SID) and checked == [502]


def test_uncertain_transport_or_restart_never_duplicates_a_notice(monkeypatch):
    session(booked=True)
    attempts = []

    def uncertain(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("delivery may have happened")

    monkeypatch.setattr(hackloop, "send", uncertain)
    assert not tracking_notice.repair(SID)
    assert not tracking_notice.repair(SID)
    assert len(attempts) == 1
    assert db.kv_get(f"tracking.notice.recovery.{SID}") == "unknown"
    assert any(e["kind"] == "tracking_notice_unknown" for e in db.get_session(SID)["events"])


def test_concurrent_repair_workers_only_send_once(monkeypatch):
    session(booked=True)
    sent = transport(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(tracking_notice.repair, [SID, SID]))
    assert sum(results) == 1 and len(sent) == 1


def test_signature_paragraphs_and_german_formality():
    body = "Hallo Sara,\n\nIhre Sendung ist gebucht.\n\n" + SIGNATURE
    result = ensure_notice(body, "de", True)
    assert "können Sie" in result
    assert result == ensure_notice(result, "de", True)
    assert result.endswith(SIGNATURE + "\n")


def test_pending_price_approval_only_promises_tracking_after_booking(monkeypatch):
    from app.agents import booking

    session(booked=True)
    db.set_booking_customer_state(SID, booking.PRICE_ASKED)
    sent = transport(monkeypatch)
    hackloop.send_customer(
        CUSTOMER,
        "Price change",
        "Please confirm the new price.\n\n" + SIGNATURE,
        session_id=SID,
        review=False,
    )
    assert NOTICES[("en", False)] in sent[0]["body"]


def test_crashed_reservation_survives_restart_and_alerts_only_once(monkeypatch, store):
    session(booked=True)
    assert db.claim_tracking_notice(SID)
    db.init_db(store)
    sent = transport(monkeypatch)
    assert not tracking_notice.repair(SID)
    assert not tracking_notice.repair(SID)
    assert not sent
    assert (
        len([e for e in db.get_session(SID)["events"] if e["kind"] == "tracking_notice_unknown"])
        == 1
    )


def test_failed_local_recording_does_not_resend_received_mail(monkeypatch):
    session(booked=True)
    sent = transport(monkeypatch)

    def fail(**kwargs):
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(db, "record_outgoing", fail)
    with pytest.raises(RuntimeError):
        tracking_notice.repair(SID)
    assert not tracking_notice.repair(SID)
    assert len(sent) == 1 and db.kv_get(f"tracking.notice.recovery.{SID}") == "sent:600"


def test_pretransport_refusal_can_be_retried_later(monkeypatch):
    session(booked=True)
    original = hackloop.send_customer

    def blocked(*args, **kwargs):
        raise hackloop.CustomerSendBlocked("human paused before transport")

    monkeypatch.setattr(hackloop, "send_customer", blocked)
    assert not tracking_notice.repair(SID)
    assert db.kv_get(f"tracking.notice.recovery.{SID}") is None
    monkeypatch.setattr(hackloop, "send_customer", original)
    sent = transport(monkeypatch)
    assert tracking_notice.repair(SID) and len(sent) == 1


def test_denied_tracking_is_not_treated_as_an_invitation():
    assert not has_notice("No tracking update is available. Please do not reply.")


def test_confirmed_customer_notice_does_not_change_quote_metrics(monkeypatch):
    session(booked=True)
    before = db.get_session(SID)
    transport(monkeypatch)
    tracking_notice.tick()
    after = db.get_session(SID)
    assert before["first_reply_at"] == after["first_reply_at"]
    assert before["quotes"] == after["quotes"]
    assert before["bookings"] == after["bookings"]


def test_final_gate_blocks_if_notice_insertion_regresses(monkeypatch):
    from app.mail import tracking_notice as wording
    from app.mail.outgoing import CustomerStyleError

    session()
    monkeypatch.setattr(wording, "ensure_notice", lambda body, *args: body)
    sent = transport(monkeypatch)
    with pytest.raises(CustomerStyleError, match="Required tracking"):
        hackloop.send_customer(
            CUSTOMER, "Quote", "Our quote is 100 €.", session_id=SID, review=False
        )
    assert not sent


def test_definitive_rate_rejection_is_retried_without_changing_the_quote(monkeypatch):
    session(booked=True)

    def reject(**kwargs):
        raise hackloop.HackloopError("POST", "/v1/mailbox/messages", 429, "rate limit")

    monkeypatch.setattr(hackloop, "send", reject)
    assert not tracking_notice.repair(SID)
    assert db.kv_get(f"tracking.notice.recovery.{SID}") is None
    sent = transport(monkeypatch)
    assert tracking_notice.repair(SID) and len(sent) == 1
