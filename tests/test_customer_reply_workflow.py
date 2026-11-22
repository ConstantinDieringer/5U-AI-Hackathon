"""Thread 24802 and the version, concurrency and transport boundaries it exposed."""

from __future__ import annotations

import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db, hackloop, llm
from app.agents import bids, booking, customer_reply, followup, reply_conditions, rfq
from app.mail import multilingual, reply_intent
from app.mail.reply_intent import Change, Condition, ReplyDecision
from app.models import Mail, RateOption, Rfq
from app.pricing.reference import Carrier
from app.quotes import active, booked, first

SID = 24802
CUSTOMER = "hofmann-textilwerk@hackloop.mail"
ROOT = "Please quote the cheapest air freight to DFW including pickup from 22554 on 26 November 2026. There are 9 pieces, 422 kg and 3.94 cbm."
UPDATE = "The final packing list has changed to 13 pieces, 611 kg gross weight, 5.71 cbm. Everything else stays the same. Could you update your offer with these figures, please?"


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "workflow.db"))
    monkeypatch.setattr(
        customer_reply,
        "settings",
        dataclasses.replace(customer_reply.settings, reply_workflow_mode="enforce"),
    )
    monkeypatch.setattr(multilingual, "prepare", lambda mail, *args: mail)
    monkeypatch.setattr(multilingual, "mail_language", lambda *args: "en")
    monkeypatch.setattr(
        booking, "settings", dataclasses.replace(booking.settings, airline_confirm_wait_s=0)
    )
    yield
    bids.close(SID)


@pytest.fixture
def api(monkeypatch):
    sent, bookings = [], []
    counter = [25055]

    def send(to, subject, body, **kwargs):
        counter[0] += 1
        result = {"id": counter[0], "to": to, "subject": subject, "body": body, **kwargs}
        sent.append(result)
        return result

    def book(rate_id, reference):
        bookings.append((rate_id, reference))
        return {
            "booking_reference": "BK-TEST",
            "rate_id": rate_id,
            "price": "1500",
            "currency": "EUR",
        }

    monkeypatch.setattr(hackloop, "send", send)
    monkeypatch.setattr(hackloop, "book", book)
    return SimpleNamespace(sent=sent, bookings=bookings, counter=counter)


def quoted(sid=SID):
    stamp = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    db.ingest_mail(
        mail_id=sid,
        sender=CUSTOMER,
        recipient="austrian-power@hackloop.mail",
        subject="Air freight to DFW",
        body=ROOT,
        category="rfq",
        received_at=stamp,
    )
    request = Rfq(
        origin="HAM",
        destination="DFW",
        pickup_zip="22554",
        pieces=9,
        weight_kg=422,
        volume_cbm=3.94,
        ready_date="2026-11-26",
        customer_name="Petra",
    ).model_dump()
    db.set_request(sid, request)
    option = dataclasses.asdict(
        RateOption("Meridian", "HAM", "DFW", 4, 140000, "EUR", 140000, "portal", rate_id="R-ONE")
    )
    audit = {
        "request": request,
        "selected": option,
        "options": [option, {**option, "airline": "Other", "total_eur_minor": 180000}],
        "trucking": {"total_eur": "100", "transit_days": 0},
    }
    db.emit("pricing_audit", "verified", sid, **audit)
    db.record_outgoing(
        mail_id=sid + 232,
        recipient=CUSTOMER,
        subject="Air freight HAM to DFW",
        body="Original offer",
        category="quote",
        session_id=sid,
        sender="austrian-power@hackloop.mail",
        in_reply_to=sid,
        sent_at=stamp,
    )
    db.record_quote(
        sid,
        total_minor=150000,
        currency="EUR",
        air_total_minor=140000,
        air_currency="EUR",
        airline="Meridian",
        origin="HAM",
        destination="DFW",
        transit_days=4,
        source="portal",
        rate_id="R-ONE",
        mail_id=sid + 232,
        alternatives=[option],
    )
    return db.get_session(sid)


def incoming(body, mid=25055, sid=SID, parent=None, refs=None):
    parent = sid + 232 if parent is None else parent
    refs = [sid, parent] if refs is None else refs
    stamp = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient="austrian-power@hackloop.mail",
        subject="Air freight HAM to DFW",
        body=body,
        category="customer",
        received_at=stamp,
        in_reply_to=parent,
        references=refs,
    )
    return Mail(
        mid,
        sid,
        CUSTOMER,
        "austrian-power@hackloop.mail",
        "Air freight HAM to DFW",
        body,
        stamp,
        references=refs,
        raw={"in_reply_to": parent},
    )


def run(mail):
    customer_reply.enqueue(mail, mail.thread_root)
    assert customer_reply.process_once()


def stub_pricing(monkeypatch):
    option = RateOption(
        "Meridian", "HAM", "DFW", 4, 140000, "EUR", 140000, "portal", rate_id="R-TWO"
    )
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *args: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *args: {})
    monkeypatch.setattr(
        rfq,
        "portal_result",
        lambda *args, **kwargs: SimpleNamespace(options=[option], email_fallbacks=[], errors=[]),
    )
    monkeypatch.setattr(rfq, "_sheets", lambda *args: [])
    monkeypatch.setattr(bids, "request", lambda *args: 0)
    monkeypatch.setattr(bids, "wait", lambda *args, **kwargs: [])
    truck = bids.Offer(
        Carrier("Truck", "trucker", "truck@hackloop.mail", ("22",)), Decimal(100), 0, 0
    )
    monkeypatch.setattr(rfq, "best_truck", lambda *args: truck)
    monkeypatch.setattr(
        rfq,
        "compose_quote_text",
        lambda request, *args: (
            f"Hi Petra,\n\nYour updated shipment has {request.pieces} pieces and weighs {request.weight_kg:g} kg. The total is 1.500 €.\n\nLet me know if you would like us to book it.\n\nYour austrian Power Team\n"
        ),
    )


def test_24802_update_reprices_preserves_unspecified_fields_and_scoring(monkeypatch, api):
    initial = quoted()
    stub_pricing(monkeypatch)
    run(incoming(UPDATE))
    result = db.get_session(SID)
    assert result["workflow"]["phase"] == "awaiting_customer"
    assert result["request"]["pieces"] == 13
    assert result["request"]["weight_kg"] == 611
    assert result["request"]["volume_cbm"] == 5.71
    assert result["request"]["pickup_zip"] == "22554"
    assert result["request"]["ready_date"] == "2026-11-26"
    assert result["deadline_at"] == initial["deadline_at"]
    assert first(result)["id"] == first(initial)["id"]
    assert active(result)["id"] != first(result)["id"]
    assert "replaces" in api.sent[-1]["body"]
    assert not api.bookings


def test_go_ahead_books_current_revision_once(monkeypatch, api):
    quoted()
    stub_pricing(monkeypatch)
    run(incoming(UPDATE))
    offer = active(db.get_session(SID))
    mail = incoming(
        "Please go ahead and book it.",
        mid=api.counter[0] + 1,
        parent=offer["mail_id"],
        refs=[SID, offer["mail_id"]],
    )
    api.counter[0] = mail.id
    run(mail)
    customer_reply.enqueue(mail, SID)
    assert not customer_reply.process_once()
    assert api.bookings == [("R-TWO", f"AP-{SID}")]
    assert booked(db.get_session(SID))["id"] == offer["id"]
    assert db.reply_workflow(SID)["phase"] == "booked"


def test_old_offer_approval_does_not_book_revision(monkeypatch, api):
    quoted()
    stub_pricing(monkeypatch)
    run(incoming(UPDATE))
    mail = incoming("Please go ahead and book it.", mid=25070)
    api.counter[0] = mail.id
    run(mail)
    assert not api.bookings
    assert "latest updated offer" in api.sent[-1]["body"]


def test_question_is_answered_without_nudge_or_literal_yes(monkeypatch, api):
    quoted()
    mail = incoming(
        "Please hold off on booking for now. Could you confirm whether the offer includes truck pickup from our site in 22554 on 26 November 2026, with air freight through to DFW?"
    )
    run(mail)
    assert api.sent and "includes pickup" in api.sent[-1]["body"]
    assert not api.bookings
    assert followup.due(db.get_session(SID), datetime.now(UTC) + timedelta(minutes=2)) is None


@pytest.mark.parametrize("fulfilled", [True, False, None])
def test_conditional_go_ahead_checks_evidence(monkeypatch, api, fulfilled):
    quoted()
    text = "If this is your most economical option, please go ahead and book it."
    decision = ReplyDecision(
        consent="conditional",
        consent_evidence=text,
        conditions=[Condition(kind="cheapest", value="", evidence="most economical option")],
    )
    monkeypatch.setattr(customer_reply, "interpret", lambda *args: decision)
    if fulfilled is False:
        with db.tx() as c:
            row = c.execute(
                "SELECT id,pricing_snapshot_json FROM quotes WHERE session_id=?", (SID,)
            ).fetchone()
            audit = json.loads(row["pricing_snapshot_json"])
            audit["options"][1]["total_eur_minor"] = 10000
            c.execute(
                "UPDATE quotes SET pricing_snapshot_json=? WHERE id=?",
                (json.dumps(audit), row["id"]),
            )
    if fulfilled is None:
        with db.tx() as c:
            c.execute("UPDATE quotes SET pricing_snapshot_json=NULL WHERE session_id=?", (SID,))
    run(incoming(text))
    assert bool(api.bookings) is (fulfilled is True)
    if fulfilled is not True:
        assert followup.due(db.get_session(SID)) is None


def test_mixed_change_and_approval_needs_new_offer_approval(monkeypatch, api):
    quoted()
    stub_pricing(monkeypatch)
    text = "Please book it, but with 611 kg."
    monkeypatch.setattr(
        customer_reply,
        "interpret",
        lambda *args: ReplyDecision(
            changes=[Change(field="weight_kg", value_json="611", evidence="611 kg")],
            consent="approve",
            consent_evidence="Please book it",
        ),
    )
    run(incoming(text))
    assert active(db.get_session(SID))["request_snapshot"]["weight_kg"] == 611
    assert not api.bookings


def test_failed_repricing_blocks_old_offer(monkeypatch, api):
    quoted()
    monkeypatch.setattr(rfq, "_price_and_quote", lambda *args, **kwargs: None)
    run(incoming(UPDATE))
    session = db.get_session(SID)
    assert active(session) is None and first(session) is not None
    assert session["workflow"]["phase"] == "needs_review"
    assert followup.due(session) is None


def test_newer_mail_during_pricing_prevents_stale_quote(monkeypatch, api):
    quoted()
    stub_pricing(monkeypatch)
    original = rfq.compose_quote_text

    def compose(*args):
        incoming("Hold off please", mid=25070)
        return original(*args)

    monkeypatch.setattr(rfq, "compose_quote_text", compose)
    run(incoming(UPDATE))
    assert not api.sent
    assert active(db.get_session(SID)) is None


def test_duplicate_and_restart_after_dispatch_never_resend(api):
    quoted()
    mail = incoming("Please go ahead and book it.")
    customer_reply.enqueue(mail, SID)
    assert db.reply_claim("crashed", time.time() - 200) is not None
    db.reply_enroll(SID, mail.id)
    db.reply_effect(SID, mail.id, "book", 1, dispatch=True)
    assert db.reply_claim("replacement", time.time()) is None
    assert db.reply_workflow(SID)["phase"] == "needs_review"
    assert not api.bookings and not api.sent


def test_decision_is_cached_and_shadow_does_not_send(monkeypatch, api):
    quoted()
    calls = []
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *args, **kwargs: calls.append(1) or ReplyDecision(consent="defer").model_dump_json(),
    )
    mail = incoming("I will review this internally and come back tomorrow.")
    customer_reply.process(mail, SID, shadow=True)
    customer_reply.process(mail, SID, shadow=True)
    assert len(calls) == 1 and not api.sent and not api.bookings
    assert db.reply_workflow(SID) is None


def test_hallucinated_changes_or_quoted_approval_are_rejected():
    with pytest.raises(ValueError):
        reply_intent.validate_evidence(
            ReplyDecision(changes=[Change(field="weight_kg", value_json="611", evidence="611 kg")]),
            "Please use 422 kg.",
        )
    with pytest.raises(ValueError):
        reply_intent.validate_evidence(
            ReplyDecision(consent="approve", consent_evidence="please book it"),
            "Thanks\n\n> please book it",
        )


def test_planned_date_does_not_prove_delivery_condition():
    q = first(quoted())
    verdict, _ = reply_conditions.check(
        Condition(kind="arrival_by", value="2026-12-01", evidence="arrive by 1 December"), q
    )
    assert verdict is None


def test_old_carrier_bid_is_not_reused_after_revision():
    session = quoted()
    db.reply_enroll(SID, 25055)
    db.reply_revise(SID, 25055, {**session["request"], "weight_kg": 611}, {})
    assert not db.reply_accepts_bid(SID, 25070, "[AP-24802]", [SID])
    assert db.reply_accepts_bid(SID, 25071, "[AP-24802] [RV-2]", [SID])


def test_100_threads_parallel_claims_preserve_per_thread_order():
    for sid in range(1, 101):
        db.ingest_mail(
            mail_id=sid,
            sender=CUSTOMER,
            recipient="austrian-power@hackloop.mail",
            subject="RFQ",
            body="quote",
            category="rfq",
            received_at=db.now(),
        )
        db.reply_enqueue(sid, {"id": sid + 1000})
        db.reply_enqueue(sid, {"id": sid + 2000})

    def claim(index):
        try:
            return db.reply_claim(str(index), time.time())
        finally:
            db.close_thread_connection()

    with ThreadPoolExecutor(max_workers=16) as pool:
        claimed = list(pool.map(claim, range(100)))
    assert len({r["session_id"] for r in claimed}) == 100
    assert all(r["mail_id"] == r["session_id"] + 1000 for r in claimed)
    assert db.reply_claim("extra", time.time()) is None


def test_pending_input_stops_followup_before_interpretation():
    quoted()
    mail = incoming(UPDATE)
    customer_reply.enqueue(mail, SID)
    db.reply_enroll(SID, mail.id)
    db.reply_phase(SID, "awaiting_customer")
    assert followup.due(db.get_session(SID), datetime.now(UTC) + timedelta(minutes=2)) is None


def test_complete_24802_customer_history_never_books_the_old_offer(monkeypatch, api):
    from app.agents import prebook

    quoted()
    stub_pricing(monkeypatch)
    fixture = json.loads((Path(__file__).parent / "fixtures" / "thread_24802.json").read_text())

    def reading(*args, **kwargs):
        evidence = json.loads(args[1][1]["content"])["current_customer_evidence"]
        if "go ahead and book it" in evidence:
            return ReplyDecision(
                consent="conditional",
                consent_evidence="please go ahead and book it",
                conditions=[
                    Condition(kind="cheapest", value="", evidence="most economical option")
                ],
            ).model_dump_json()
        questions = [s.strip() for s in prebook._SENTENCE.split(evidence) if prebook.is_question(s)]
        return ReplyDecision(
            questions=questions, consent="defer" if "hold off" in evidence else "none"
        ).model_dump_json()

    monkeypatch.setattr(llm, "complete", reading)
    for record in fixture["messages"][1:]:
        api.counter[0] = max(api.counter[0], record["id"])
        run(
            incoming(
                record["body"],
                mid=record["id"],
                parent=record["in_reply_to"],
                refs=record["references"],
            )
        )
        assert not api.bookings
        assert len(db.get_session(SID)["quotes"]) == 2
    session = db.get_session(SID)
    assert session["request"]["weight_kg"] == 611
    assert "611 kg" in api.sent[-1]["body"]
    assert "1.500 €" in api.sent[-1]["body"]
    assert not [m for m in session["mails"] if m["category"] == "followup"]


def test_a_model_cannot_approve_an_explicit_stop_or_invent_a_value():
    with pytest.raises(ValueError):
        reply_intent.validate_evidence(
            ReplyDecision(consent="approve", consent_evidence="book it"), "Please don't book it."
        )
    with pytest.raises(ValueError):
        reply_intent.validate_evidence(
            ReplyDecision(changes=[Change(field="weight_kg", value_json="999", evidence="611 kg")]),
            "Please use 611 kg.",
        )


def test_attachment_corrections_use_only_named_current_sources(monkeypatch, api):
    from app.mail import rfq_sources

    quoted()
    stub_pricing(monkeypatch)
    text = "Please update the shipment using the attached final packing list."
    monkeypatch.setattr(
        rfq_sources,
        "read_sources",
        lambda *args, **kwargs: (
            {"packing.txt": "13 pieces, gross weight 611 kg, volume 5.71 cbm"},
            [],
            [],
        ),
    )
    monkeypatch.setattr(
        customer_reply,
        "interpret",
        lambda *args: ReplyDecision(
            changes=[
                Change(field="pieces", value_json="13", evidence="13 pieces", source="packing.txt"),
                Change(
                    field="weight_kg",
                    value_json="611",
                    evidence="gross weight 611 kg",
                    source="packing.txt",
                ),
                Change(
                    field="volume_cbm",
                    value_json="5.71",
                    evidence="volume 5.71 cbm",
                    source="packing.txt",
                ),
            ]
        ),
    )
    mail = incoming(text)
    mail.attachments = [{"id": 1, "filename": "packing.txt"}]
    run(mail)
    assert active(db.get_session(SID))["request_snapshot"]["weight_kg"] == 611


def test_unknown_booking_outcome_does_not_retry_on_new_input(monkeypatch, api):
    quoted()

    def uncertain(*args):
        raise hackloop.HackloopError("POST", "/book", None, "transport timeout")

    monkeypatch.setattr(hackloop, "book", uncertain)
    run(incoming("Please go ahead and book it."))
    mail = incoming("Please go ahead and book it.", mid=25070)
    customer_reply.enqueue(mail, SID)
    assert not customer_reply.process_once()
    assert db.reply_has_pending(SID)
    assert db.reply_workflow(SID)["phase"] == "needs_review"
    assert not api.bookings


def test_restart_before_external_action_resumes_same_revision(monkeypatch, api):
    quoted()
    stub_pricing(monkeypatch)
    mail = incoming(UPDATE)
    customer_reply.enqueue(mail, SID)
    assert db.reply_claim("dead", time.time() - 200) is not None
    session = db.get_session(SID)
    decision = reply_intent.rules(UPDATE, session)
    db.reply_decision(SID, mail.id, decision.model_dump())
    db.reply_revise(
        SID, mail.id, {**session["request"], "pieces": 13, "weight_kg": 611, "volume_cbm": 5.71}, {}
    )
    assert customer_reply.process_once()
    assert active(db.get_session(SID))["request_version"] == 2
    assert len(api.sent) == 1


def test_100_customer_threads_complete_once_with_mocked_services(api):
    for sid in range(1, 101):
        quoted(sid)
        customer_reply.enqueue(
            incoming("Please go ahead and book it.", mid=10000 + sid, sid=sid), sid
        )
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: customer_reply.process_once(), range(100)))
    assert len(api.bookings) == 100
    assert len({reference for _, reference in api.bookings}) == 100
    assert all(db.reply_workflow(sid)["phase"] == "booked" for sid in range(1, 101))


@pytest.mark.parametrize(
    "value,evidence,expected",
    [
        ('{"amount_minor":160000,"currency":"EUR"}', "only if the total is below 1600 EUR", True),
        ('{"amount_minor":1600000,"currency":"EUR"}', "only if the total is below 1600 EUR", None),
        ('{"amount_minor":140000,"currency":"EUR"}', "only if the total is below 1400 EUR", False),
    ],
)
def test_price_caps_must_match_the_customer_evidence(value, evidence, expected):
    quote = first(quoted())
    assert (
        reply_conditions.check(Condition(kind="price_cap", value=value, evidence=evidence), quote)[
            0
        ]
        is expected
    )


def test_new_airline_price_acceptance_uses_current_price_without_rebooking(api):
    session = quoted()
    quote_id = first(session)["id"]
    db.record_booking(
        SID,
        booking_reference="BK-EXISTING",
        rate_id="R-ONE",
        airline="Meridian",
        price_minor=140000,
        currency="EUR",
    )
    db.reply_bind_booking(SID, quote_id)
    db.set_booking_customer_state(SID, booking.PRICE_ASKED, 175000)
    db.record_outgoing(
        mail_id=25050,
        recipient=CUSTOMER,
        subject="Updated booking price",
        body="The new total is 1.750 €",
        category="price_change",
        session_id=SID,
        sender="austrian-power@hackloop.mail",
        in_reply_to=25034,
    )
    run(incoming("Please go ahead and book it.", parent=25050, refs=[SID, 25034, 25050]))
    assert not api.bookings
    assert db.get_session(SID)["bookings"][0]["customer_state"] == booking.PRICE_ACCEPTED
    assert "1.750 €" in api.sent[-1]["body"]


def test_paused_threads_resume_without_spending_a_reading(api):
    quoted()
    mail = incoming("Please go ahead and book it.")
    customer_reply.enqueue(mail, SID)
    db.set_paused(SID, True)
    assert not customer_reply.process_once()
    assert not api.bookings
    db.set_paused(SID, False)
    assert customer_reply.process_once()
    assert len(api.bookings) == 1


def test_change_after_booking_does_not_claim_that_nothing_is_booked(api):
    session = quoted()
    db.record_booking(
        SID,
        booking_reference="BK-EXISTING",
        rate_id="R-ONE",
        airline="Meridian",
        price_minor=140000,
        currency="EUR",
    )
    db.reply_bind_booking(SID, first(session)["id"])
    run(incoming(UPDATE))
    assert "already has a booking" in api.sent[-1]["body"]
    assert "We have not booked" not in api.sent[-1]["body"]
    assert not api.bookings


def test_additive_migration_preserves_legacy_quote_and_never_replays_consent(tmp_path, api):
    quoted()
    with db.tx() as c:
        for column in (
            "request_version",
            "request_snapshot_json",
            "pricing_snapshot_json",
            "supersedes_id",
        ):
            c.execute(f"ALTER TABLE quotes DROP COLUMN {column}")
        c.execute("ALTER TABLE bookings DROP COLUMN quote_id")
    db.init_db()
    db.init_db()
    session = db.get_session(SID)
    assert first(session)["total_minor"] == 150000
    assert session["workflow"] is None
    assert not api.sent and not api.bookings
    db.reply_enroll(SID, 25055)
    assert active(db.get_session(SID)) is None


def test_tracking_in_owned_thread_uses_queue_and_one_durable_send(monkeypatch, api):
    from app.agents import tracking
    from app.mail import poller
    from app.models import Category

    session = quoted()
    db.record_booking(
        SID,
        booking_reference="BK-EXISTING",
        rate_id="R-ONE",
        airline="Meridian",
        price_minor=140000,
        currency="EUR",
    )
    db.reply_bind_booking(SID, first(session)["id"])
    mail = incoming("Where is my shipment?")
    poller.dispatch(mail, Category.TRACKING, SID)
    assert not api.sent
    assert customer_reply.process_once()
    assert len(api.sent) == 1
    assert db.reply_workflow(SID)["phase"] == "booked"
    # A repeat direct invocation also reuses the persisted send outcome.
    tracking.handle(mail, SID, booked_session=SID)
    assert len(api.sent) == 1
