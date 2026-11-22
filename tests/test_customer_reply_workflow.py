"""Thread 24802 and the version, concurrency and transport boundaries it exposed."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import db, hackloop
from app.agents import bids, booking, customer_reply, followup, rfq
from app.mail import multilingual
from app.mail.reply_intent import Condition, ReplyDecision
from app.mail.style import amount
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
            f"Hi Petra,\n\nYour updated shipment has {request.pieces} pieces and weighs {request.weight_kg:g} kg. The total is {amount(args[1])}.\n\nLet me know if you would like us to book it.\n\nYour austrian Power Team\n"
        ),
    )


@pytest.mark.core
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


@pytest.mark.core
def test_old_offer_approval_does_not_book_revision(monkeypatch, api):
    quoted()
    stub_pricing(monkeypatch)
    run(incoming(UPDATE))
    mail = incoming("Please go ahead and book it.", mid=25070)
    api.counter[0] = mail.id
    run(mail)
    assert not api.bookings
    assert "latest updated offer" in api.sent[-1]["body"]


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
    db.init_db(str(tmp_path / "workflow.db"))
    db.init_db(str(tmp_path / "workflow.db"))
    session = db.get_session(SID)
    assert first(session)["total_minor"] == 150000
    assert session["workflow"] is None
    assert not api.sent and not api.bookings
    db.reply_enroll(SID, 25055)
    assert active(db.get_session(SID)) is None


@pytest.mark.core
def test_pdf_ask_after_quote_sends_offer_pdf_not_review(monkeypatch, api):
    """A customer who sent us files asks for our offer as a PDF: the offer copy goes out with
    the code-rendered PDF, the thread waits for them, nothing is booked or reviewed."""
    import base64
    import io

    import pdfplumber

    quoted()
    body = "We sent you our packing list earlier. Could you send me your offer as a PDF?"
    decision = ReplyDecision(questions=["Could you send me your offer as a PDF?"])
    monkeypatch.setattr(customer_reply, "interpret", lambda *args: decision)
    run(incoming(body))
    [copy] = api.sent
    assert "current offer" in copy["body"] and "PDF" in copy["body"]
    [pdf] = copy["attachments"]
    assert pdf["content_type"] == "application/pdf"
    with pdfplumber.open(io.BytesIO(base64.b64decode(pdf["content_base64"]))) as doc:
        assert amount(150000) in (doc.pages[0].extract_text() or "")
    assert db.reply_workflow(SID)["phase"] == "awaiting_customer"
    assert not api.bookings


def test_own_pdf_is_not_a_pdf_ask_after_quote(monkeypatch, api):
    """Counterexample: the customer only sends their own PDF; no offer PDF goes out."""
    quoted()
    monkeypatch.setattr(customer_reply, "interpret", lambda *args: ReplyDecision())
    run(incoming("Please find attached the packing list as a PDF."))
    assert all(not m.get("attachments") for m in api.sent)
    assert db.reply_workflow(SID)["phase"] == "needs_review"


def test_silent_customer_still_gets_a_fourth_follow_up(api, monkeypatch):
    """With a budget above three (FOLLOWUP_MAX), a fourth nudge still goes out, and a full
    budget stops further nudges (counterexample). The default budget is 2."""
    monkeypatch.setattr(
        followup,
        "settings",
        dataclasses.replace(followup.settings, followup_max=10, followup_after_s=5.0),
    )
    quoted()

    def nudge(n):
        db.record_outgoing(
            mail_id=SID + 300 + n,
            recipient=CUSTOMER,
            subject="Air freight HAM to DFW",
            body=f"Follow-up {n}",
            category=followup.CATEGORY,
            session_id=SID,
            sender="austrian-power@hackloop.mail",
            in_reply_to=SID + 232,
            sent_at=(datetime.now(UTC) - timedelta(seconds=45)).isoformat(),
        )

    for n in range(3):
        nudge(n)
    assert followup.due(db.get_session(SID)) == ("offer", 3)
    for n in range(3, followup.settings.followup_max):
        nudge(n)
    assert followup.due(db.get_session(SID)) is None


# ------------------------------------------- changed cargo in shadow mode (round 172)

ROUND_172 = "Sorry for the change. The shipment details have changed according to the final packing list: 13 pieces, 611 kg gross weight, 5.71 cbm.\n\nEverything else stays the same. Could you please update your offer accordingly?"


def shadow(monkeypatch):
    monkeypatch.setattr(
        customer_reply,
        "settings",
        dataclasses.replace(customer_reply.settings, reply_workflow_mode="shadow"),
    )


def legacy(mail):
    """The poller in shadow mode: shadow evaluation, then the legacy reply path."""
    if not customer_reply.enqueue(mail, mail.thread_root):
        booking.handle_reply(mail, mail.thread_root)
    while customer_reply.process_once():
        pass


@pytest.mark.core
def test_changed_cargo_in_shadow_mode_gets_a_revised_offer_and_books_it(monkeypatch, api):
    """Round 172: the customer changed the cargo after our quote and asked for an update; the
    legacy path took the thread to manual and nobody answered within the round."""
    shadow(monkeypatch)
    quoted()
    stub_pricing(monkeypatch)
    legacy(incoming(ROUND_172))
    session = db.get_session(SID)
    assert not db.is_manual(SID)
    assert session["request"]["pieces"] == 13 and session["request"]["weight_kg"] == 611
    assert "replaces our previous offer" in api.sent[-1]["body"]
    offer = active(session)
    assert offer["rate_id"] == "R-TWO" and offer["mail_id"] == api.sent[-1]["id"]
    yes = incoming(
        "Please go ahead and book it.",
        mid=api.counter[0] + 1,
        parent=offer["mail_id"],
        refs=[SID, offer["mail_id"]],
    )
    api.counter[0] = yes.id
    legacy(yes)
    assert api.bookings == [("R-TWO", f"AP-{SID}")]


def test_restated_cargo_in_shadow_mode_stays_on_the_legacy_path(monkeypatch, api):
    """Counterexample: figures that match the quote are no change; the thread is not promoted."""
    shadow(monkeypatch)
    quoted()
    restated = incoming(
        "Just to confirm, it is 9 pieces, 422 kg and 3.94 cbm. When could it depart?"
    )
    legacy(restated)
    assert not customer_reply.enforced(SID)
    assert db.kv_get(customer_reply._promoted_key(SID)) is None
    assert not [a for a in api.sent if "replaces our previous offer" in a["body"]]
    stale = incoming("Please book it.", mid=25090)
    db.reply_enqueue(SID, dataclasses.asdict(stale), shadow=False)  # left from enforce mode
    assert not customer_reply.process_once()
    assert api.bookings == []
