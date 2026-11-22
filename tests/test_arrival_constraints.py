"""Round 83/23465: cheapest that arrives in time, not the shortest transit at any price."""

import dataclasses
import time
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app import db
from app.agents import bids, clarification, rfq
from app.mail import selection_intent
from app.mail.rfq_validation import validate
from app.models import Mail, RateOption, Rfq
from app.pricing.portal_sync import PortalResult
from app.pricing.pricing import earliest_schedule, pick, within_arrival_deadline
from app.ui import drafts

TODAY = date(2026, 11, 22)
BODY = (
    "Could you please provide an all-in air freight quote in EUR from STR to MEL? "
    "The shipment will be ready on 2026-11-26, and we will deliver it to STR ourselves.\n\n"
    "6 pieces, gross weight 1841 kg, total volume 4.63 cbm.\n\n"
    "The goods must arrive at MEL no later than 2026-12-02. "
    "Among the options that meet this deadline, price will determine our choice."
)


def request(**changes):
    return Rfq(
        origin="STR",
        destination="MEL",
        weight_kg=1841,
        volume_cbm=4.63,
        pieces=6,
        ready_date="2026-11-26",
        preference="fastest",
    ).model_copy(update=changes)


def mail(body=BODY):
    return Mail(
        23465,
        23465,
        "sonnberg-roesterei@hackloop.mail",
        "team@hackloop.mail",
        "Air freight STR to MEL",
        body,
        datetime.now(UTC).isoformat(),
    )


def options():
    return [
        RateOption(name, "STR", "MEL", days, price, "EUR", price, source)
        for name, days, price, source in (
            ("Yellowtail Air Freight", 5, 756000, "sheet"),
            ("Aurelia Air Cargo", 3, 804000, "portal"),
            ("Solace Air Cargo", 4, 606000, "portal"),
        )
    ]


def test_23465_wrong_model_preference_is_corrected_from_customer_evidence():
    checked, draft = validate(mail(), request())
    assert not draft.issues
    assert checked.arrival_deadline == "2026-12-02" and checked.preference == "cheapest"
    chosen = pick(within_arrival_deadline(checked, options(), TODAY), checked.preference)
    assert chosen.airline == "Solace Air Cargo" and chosen.total_eur_minor == 606000
    assert 804000 - chosen.total_eur_minor == 198000


@pytest.mark.parametrize(
    "text",
    [
        "Delivery must be by December 2, 2026. Choose the cheapest option that gets there in time.",
        "Please select the lowest cost service arriving by 2 December 2026.",
        "Die Ware muss spätestens am 2. Dezember 2026 ankommen. Der Preis entscheidet.",
        "Bitte die günstigste Verbindung mit Ankunft bis 02.12.2026.",
        "The goods must arrive no later than 2026-12-02. No need for the fastest option.",
    ],
)
def test_price_priority_with_a_date_is_not_fastest(text):
    data, issues, _ = selection_intent.checked(request().model_dump(), {"body": text})
    assert not issues
    assert data["preference"] == "cheapest" and data["arrival_deadline"] == "2026-12-02"


def test_genuine_fastest_priority_remains_fastest():
    text = "Please quote the fastest option. It must arrive by 2 December 2026."
    data, issues, _ = selection_intent.checked(request().model_dump(), {"body": text})
    assert not issues and data["preference"] == "fastest"
    chosen = pick(within_arrival_deadline(Rfq.model_validate(data), options(), TODAY), "fastest")
    assert chosen.airline == "Aurelia Air Cargo"


def test_negated_price_priority_does_not_override_explicit_fastest():
    data, issues, _ = selection_intent.checked(
        request().model_dump(),
        {
            "body": "Choose the fastest option arriving by 2 December 2026. Price will not determine our choice."
        },
    )
    assert not issues and data["preference"] == "fastest"


def test_ready_departure_and_validity_dates_are_not_arrival_deadlines():
    text = "Ready by 2026-11-26. Quote valid until 2026-12-02. Latest departure 2026-11-28."
    assert selection_intent.arrival_dates(text) == {}
    data, issues, _ = selection_intent.checked(request().model_dump(), {"body": text})
    assert not issues and data["arrival_deadline"] is None


def test_self_delivery_to_export_airport_is_not_a_destination_deadline():
    text = "We will deliver the shipment to STR by 26 November 2026."
    data, issues, _ = selection_intent.checked(request().model_dump(), {"body": text})
    assert not issues and data["arrival_deadline"] is None


def test_unproven_or_conflicting_constraints_block_instead_of_disappearing():
    _, issues, _ = selection_intent.checked(
        request(arrival_deadline="2026-12-02").model_dump(), {"body": "Please quote STR to MEL."}
    )
    assert issues == [{"field": "arrival_deadline", "code": "unproven"}]
    _, issues, _ = selection_intent.checked(
        request().model_dump(),
        {
            "body": "Arrival no later than 2026-12-02.",
            "spec": "Latest arrival 2026-12-03.",
        },
    )
    assert issues == [{"field": "arrival_deadline", "code": "conflict"}]
    with pytest.raises(ValidationError):
        Rfq.model_validate({**request().model_dump(), "arrival_deadline": "2026-02-30"})


def test_quoted_old_deadline_does_not_become_a_current_requirement():
    root = mail(
        "Please quote STR to MEL, 1841 kg, 4.63 cbm.\n\n"
        "On Monday wrote:\nArrival no later than 2026-12-02."
    )
    checked, _ = validate(root, request())
    assert checked.arrival_deadline is None


def test_cheapest_late_service_is_filtered_before_selection_and_airline_bid_balancing():
    r = request(preference="cheapest", arrival_deadline="2026-11-30")
    late = RateOption("Too late", "STR", "MEL", 7, 10000, "EUR", 10000, "bid")
    eligible = within_arrival_deadline(r, [late, *options()], TODAY)
    assert {o.airline for o in eligible} == {"Aurelia Air Cargo", "Solace Air Cargo"}
    feasible_bids = within_arrival_deadline(r, [late, options()[2]], TODAY)
    assert rfq.best_air([], feasible_bids, r.preference).airline == "Solace Air Cargo"


def test_arrival_forecast_uses_same_readiness_pickup_and_today_as_quote_copy():
    r = request(arrival_deadline="2026-11-30", pickup_zip="70629")
    assert earliest_schedule(r, 4, TODAY) == rfq.schedule(r, 4, TODAY)
    assert earliest_schedule(r, 4, TODAY)[1] == date(2026, 12, 1)
    assert within_arrival_deadline(r, [options()[2]], TODAY) == []
    assert earliest_schedule(request(ready_date="2026-11-01"), 4, TODAY)[0] == TODAY


def test_missing_readiness_and_incomplete_priority_require_clarification():
    _, issues, _ = selection_intent.checked(request(ready_date=None).model_dump(), {"body": BODY})
    assert {i["field"] for i in issues} == {"ready_date"}
    _, issues, _ = selection_intent.checked(
        request().model_dump(),
        {"body": "I want the cheapest and fastest service. Arrival by 2026-12-02."},
    )
    assert {i["field"] for i in issues} == {"preference"}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "arrival.db"))
    root = mail()
    db.ingest_mail(
        mail_id=root.id,
        sender=root.sender,
        recipient=root.to,
        subject=root.subject,
        body=root.body,
        category="rfq",
        received_at=root.received_at,
    )
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *a: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *a: {})
    monkeypatch.setattr(rfq, "portal_result", lambda *a, **k: PortalResult([], [], {}))
    monkeypatch.setattr(rfq, "_sheets", lambda *a: options())
    monkeypatch.setattr(bids, "wait", lambda *a, **k: None)
    monkeypatch.setattr(rfq, "_quote_pdf", lambda *a, **k: None)
    monkeypatch.setattr(
        rfq, "within_arrival_deadline", lambda r, opts: within_arrival_deadline(r, opts, TODAY)
    )
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, price_adjust_pct=0))
    monkeypatch.setattr(
        rfq, "compose_quote_text", lambda r, o, *a: f"Checked quote for {o.airline}"
    )
    sent = []
    monkeypatch.setattr(
        rfq.hackloop,
        "send_customer",
        lambda to, subject, body, **k: (
            sent.append((body, k)) or {"id": 23592, "body": body, "subject": subject}
        ),
    )
    return root, sent


@pytest.mark.parametrize("forced", [False, True])
def test_full_quote_path_23465_checks_intent_even_with_stored_wrong_extraction(flow, forced):
    root, sent = flow
    db.set_request(root.id, request().model_dump())
    rfq._price_and_quote(root, root.id, request(), "STR", time.time(), "enforce", forced=forced)
    session = db.get_session(root.id)
    assert len(sent) == 1
    assert session["quotes"][0]["airline"] == "Solace Air Cargo"
    assert session["quotes"][0]["total_minor"] == 606000
    assert session["request"]["preference"] == "cheapest"
    assert session["request"]["arrival_deadline"] == "2026-12-02"
    assert any(e["kind"] == "arrival_selection" for e in session["events"])


def test_no_feasible_option_asks_before_claim_and_never_quotes_late(flow, monkeypatch):
    root, sent = flow
    only_late = dataclasses.replace(options()[2], transit_days=30)
    monkeypatch.setattr(rfq, "_sheets", lambda *a: [only_late])
    monkeypatch.setattr(
        clarification, "settings", dataclasses.replace(clarification.settings, clarify_once=False)
    )
    rfq._price_and_quote(root, root.id, request(), "STR", time.time(), "enforce")
    session = db.get_session(root.id)
    assert not session["quotes"] and session["first_reply_at"] is None
    assert len(sent) == 1 and "latest arrival date" in sent[0][0]
    assert session["validation"]["state"] == "awaiting_clarification"
    assert session["validation"]["send_state"] == "sent"


def test_direct_deadline_answer_overrides_original_limit_and_survives_revalidation():
    corrections = selection_intent.answer(
        "Arrival on 4 December 2026 would work.", {"arrival_deadline"}
    )
    checked, draft = validate(mail(), request(), corrections=corrections)
    assert not draft.issues and checked.arrival_deadline == "2026-12-04"


def test_clarified_arrival_limit_resumes_without_reinstating_original_date(flow, monkeypatch):
    root, sent = flow
    only_late = dataclasses.replace(options()[2], transit_days=30)
    monkeypatch.setattr(rfq, "_sheets", lambda *a: [only_late])
    monkeypatch.setattr(
        clarification, "settings", dataclasses.replace(clarification.settings, clarify_once=False)
    )
    _, draft = validate(root, request())
    draft.evidence["corrections"] = {"weight_kg": 1841}
    db.validation_save(root.id, "ready", draft.model_dump(), dataclasses.asdict(root))
    rfq._price_and_quote(root, root.id, request(), "STR", time.time(), "enforce")
    reply = dataclasses.replace(
        root, id=23600, body="Arrival by 10 January 2027 works for us.", references=[root.id]
    )
    db.ingest_mail(
        mail_id=reply.id,
        sender=reply.sender,
        recipient=reply.to,
        subject=reply.subject,
        body=reply.body,
        category="customer",
        received_at=reply.received_at,
        references=reply.references,
    )
    assert clarification.handle_reply(reply, root.id)
    session = db.get_session(root.id)
    assert len(sent) == 2 and len(session["quotes"]) == 1
    assert session["request"]["arrival_deadline"] == "2027-01-10"


def test_strict_before_is_not_an_inclusive_latest_date():
    assert list(selection_intent.arrival_dates("Arrival must be before 2 December 2026.")) == [
        "2026-12-01"
    ]
    assert selection_intent.arrival_dates("Arrival must not be before 2 December 2026.") == {}


def test_relative_required_arrival_is_not_silently_ignored():
    _, issues, _ = selection_intent.checked(
        request().model_dump(), {"body": "The goods must arrive by next Friday."}
    )
    assert issues == [{"field": "arrival_deadline", "code": "missing"}]


def test_explicit_price_priority_wins_even_without_a_deadline():
    data, issues, _ = selection_intent.checked(
        request().model_dump(), {"body": "Price will determine our choice."}
    )
    assert not issues and data["preference"] == "cheapest"


def test_emergency_draft_uses_same_intent_and_deadline_gate(flow, monkeypatch):
    root, _ = flow
    db.set_request(root.id, request().model_dump())
    monkeypatch.setattr(drafts, "sheet_options", lambda *a: options())
    monkeypatch.setattr(drafts, "portal_result", lambda *a, **k: PortalResult([], [], {}))
    result = drafts.build(root.id)
    assert result["choice"]["airline"] == "Solace Air Cargo" and result["total_minor"] == 606000


def test_forced_selection_retains_deadline_evidence_from_cached_attachment(flow):
    root, _ = flow
    import json

    db.kv_set(
        f"rfq.sources.{root.id}",
        json.dumps(
            {"texts": {"spec.txt": "Latest arrival 2 December 2026. Price determines our choice."}}
        ),
    )
    root = dataclasses.replace(root, body="Please quote STR to MEL, ready on 26 November 2026.")
    checked, draft = rfq.checked_selection(root, root.id, request(arrival_deadline="2026-12-02"))
    assert not draft.issues and checked.preference == "cheapest"


def test_late_preferred_airline_cannot_bypass_arrival_filter(flow, monkeypatch):
    from app.agents import carrier_prefs

    root, sent = flow
    monkeypatch.setattr(carrier_prefs, "preferred", lambda *a: {"aurelia air cargo"})
    opts = [
        dataclasses.replace(o, transit_days=30) if o.airline == "Aurelia Air Cargo" else o
        for o in options()
    ]
    monkeypatch.setattr(rfq, "_sheets", lambda *a: opts)
    rfq._price_and_quote(root, root.id, request(), "STR", time.time(), "enforce")
    assert len(sent) == 1
    assert db.get_session(root.id)["quotes"][0]["airline"] == "Solace Air Cargo"


def test_multiple_shipments_each_use_the_constraint_gate_before_one_combined_reply(
    flow, monkeypatch
):
    root, sent = flow
    monkeypatch.setattr(
        rfq,
        "_sheets",
        lambda r: [dataclasses.replace(o, destination=r.destination) for o in options()],
    )
    rfq._price_and_quote_multi(
        root, root.id, request(), "STR", [request(destination="DFW")], time.time(), "enforce"
    )
    session = db.get_session(root.id)
    assert len(sent) == 1 and len(session["quotes"]) == 1
    audits = [e["payload"] for e in session["events"] if e["kind"] == "pricing_audit"]
    assert len(audits) == 2
    assert all(a["selected"]["airline"] == "Solace Air Cargo" for a in audits)


def test_one_infeasible_shipment_does_not_send_a_partial_combined_quote(flow, monkeypatch):
    root, sent = flow
    monkeypatch.setattr(
        rfq,
        "_sheets",
        lambda r: [
            dataclasses.replace(
                o,
                destination=r.destination,
                transit_days=30 if r.destination == "DFW" else o.transit_days,
            )
            for o in options()
        ],
    )
    rfq._price_and_quote_multi(
        root, root.id, request(), "STR", [request(destination="DFW")], time.time(), "enforce"
    )
    session = db.get_session(root.id)
    assert len(sent) == 1 and "latest arrival date" in sent[0][0]
    assert not session["quotes"] and session["first_reply_at"] is None
