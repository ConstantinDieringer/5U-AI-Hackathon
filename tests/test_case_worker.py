"""Whole-case contracts: new combinations of tasks, actual files and durable effects."""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import pdfplumber
import pytest

from app import db, hackloop, llm
from app.agents import case_delivery, case_runtime, case_tools, case_worker, rfq, watchdog
from app.agents.case_models import (
    Audit,
    Citation,
    Decision,
    Finding,
    Source,
    Task,
    ToolError,
    Workspace,
)
from app.agents.case_selection import Value, evaluate
from app.agents.case_sources import discover
from app.mail import documents, poller
from app.models import Category, Mail, RateOption, Rfq
from app.pricing.portal_sync import PortalResult

CUSTOMER = "customer@hackloop.mail"
TEAM = "austrian-power@hackloop.mail"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "cases.db"))
    configured = dataclasses.replace(
        case_worker.settings,
        case_worker_enabled=True,
        runtime_dir=tmp_path / "runtime",
        inbox_dir=tmp_path / "inbox",
    )
    monkeypatch.setattr(case_worker, "settings", configured)
    monkeypatch.setattr(case_tools, "settings", configured)
    monkeypatch.setattr(case_runtime, "settings", configured)
    monkeypatch.setattr(case_delivery, "settings", configured)
    monkeypatch.setattr("app.config.settings", configured)
    monkeypatch.setattr(poller, "settings", configured)
    monkeypatch.setattr(rfq, "settings", dataclasses.replace(configured, price_adjust_pct=0))
    sent = []

    def send(to, subject, body, **kwargs):
        result = {
            "id": 1000 + len(sent),
            "from": TEAM,
            "to": to,
            "subject": subject,
            "body": body,
            **kwargs,
        }
        sent.append(result)
        return result

    monkeypatch.setattr(hackloop, "send", send)
    yield tmp_path, sent
    db.case_clear_context()
    db.close_thread_connection()


def incoming(
    mid=100,
    body="Please quote FRA to JFK, 100 kg gross, 1 cbm, 2 pieces.",
    *,
    attachments=None,
    parent=None,
    sender=CUSTOMER,
):
    raw = {
        "id": mid,
        "from": sender,
        "to": TEAM,
        "subject": "Shipment request",
        "body": body,
        "received_at": datetime.now(UTC).isoformat(),
        "attachments": attachments or [],
        "references": [parent] if parent else [],
    }
    if parent:
        raw["in_reply_to"] = parent
    sid = db.ingest_mail(
        mail_id=mid,
        sender=sender,
        recipient=TEAM,
        subject=raw["subject"],
        body=body,
        category="customer" if parent else "rfq",
        raw=raw,
        references=raw["references"],
        in_reply_to=parent,
    )
    mail = Mail(
        mid,
        parent or mid,
        sender,
        TEAM,
        raw["subject"],
        body,
        raw["received_at"],
        raw["attachments"],
        raw["references"],
        raw,
    )
    return sid, mail


def task(key, source, text):
    return Task(
        id=key, description=text, evidence=[Citation(source=source, text=text)], depends_on=[]
    )


def step(tool, args, tasks=()):
    return Decision(tasks=list(tasks), tool=tool, arguments_json=json.dumps(args), reason=tool)


def runtime(sid, mail):
    db.case_enqueue(sid, mail.id)
    job = db.case_claim("test-owner", time.time())
    assert job
    state = Workspace.model_validate(job["workspace"])
    discover(sid, state)
    return case_tools.Runtime(job, "test-owner", mail, state)


def pricing(monkeypatch):
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *a: ([], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *a: {})
    monkeypatch.setattr(rfq, "portal_result", lambda *a, **kw: PortalResult([], [], {}))
    monkeypatch.setattr(rfq.bids, "wait", lambda *a, **kw: None)
    monkeypatch.setattr(
        rfq,
        "_sheets",
        lambda r: [
            RateOption("Economy", "FRA", r.destination, 4, 100000, "EUR", 100000, "sheet"),
            RateOption("Express", "FRA", r.destination, 2, 110000, "EUR", 110000, "sheet"),
        ],
    )
    monkeypatch.setattr(
        rfq,
        "compose_quote_text",
        lambda r, o, total, *a: (
            f"Hello,\n\nAir freight FRA to {r.destination} with {o.airline}. "
            f"The total is {total // 100} €.\n\nYour austrian Power Team"
        ),
    )


def test_dispatch_two_shipments_spoken_correction_new_selection_rule_and_real_pdf(
    setup, monkeypatch
):
    _tmp_path, sent = setup
    pricing(monkeypatch)
    body = "Quote both shipments in the attached sheet and use the voice correction. Send both offers as PDF."
    sheet = "First shipment FRA to JFK. 2 pieces, 100 kg gross, 1 cbm total."
    voice = "The second shipment is FRA to SIN, 3 pieces, 200 kg gross, 2 cbm total."
    root = "mail-100"
    sid, mail = incoming(
        body=body,
        attachments=[
            {"id": 1, "filename": "shipments.txt"},
            {"id": 2, "filename": "changes.mp3"},
        ],
    )
    monkeypatch.setattr(
        hackloop, "get_attachment", lambda mid, aid: sheet.encode() if aid == 1 else b"ID3voice"
    )
    transcribed = []

    def transcribe(doc, session_id, purpose, path):
        transcribed.append((doc.kind, session_id))
        return voice

    monkeypatch.setattr(documents, "transcribe", transcribe)
    source1, source2 = root + "-attachment-1", root + "-attachment-2"
    tasks = [
        task("first", source1, sheet),
        task("second", source2, voice),
        task("document", root, "Send both offers as PDF."),
    ]
    decisions = iter(
        [
            step("read_source", {"source": source1}),
            step("read_source", {"source": source2}),
            step(
                "prepare_shipment",
                {
                    "shipment_id": "one",
                    "request_json": Rfq(
                        origin="FRA", destination="JFK", weight_kg=100, volume_cbm=1, pieces=2
                    ).model_dump_json(),
                    "evidence": [{"source": source1, "text": sheet}],
                    "task_ids": ["first"],
                },
                tasks,
            ),
            step(
                "prepare_shipment",
                {
                    "shipment_id": "two",
                    "request_json": Rfq(
                        origin="FRA", destination="SIN", weight_kg=200, volume_cbm=2, pieces=3
                    ).model_dump_json(),
                    "evidence": [{"source": source2, "text": voice}],
                    "task_ids": ["second"],
                },
            ),
            step("price_shipments", {"shipment_ids": ["one", "two"]}),
            step("create_quote_pdf", {"shipment_ids": ["one", "two"]}),
        ]
    )
    contexts = []

    def complete(purpose, messages, session_id, **kwargs):
        context = json.loads(messages[-1]["content"])
        contexts.append(context)
        if purpose == "case_audit":
            return Audit(
                findings=[
                    Finding(
                        task_id="first", status="satisfied", evidence_refs=["one"], reason="Priced"
                    ),
                    Finding(
                        task_id="second",
                        status="satisfied",
                        evidence_refs=["two"],
                        reason="Corrected and priced",
                    ),
                    Finding(
                        task_id="document",
                        status="satisfied",
                        evidence_refs=context["proposal"]["attached_artifact_ids"],
                        reason="Actual PDF attached",
                    ),
                ],
                missing_tasks=[],
                unsupported_claims=[],
            ).model_dump_json()
        try:
            return next(decisions).model_dump_json()
        except StopIteration:
            return step(
                "deliver_quote",
                {
                    "shipment_ids": ["one", "two"],
                    "artifact_ids": list(context["workspace"]["artifacts"]),
                },
            ).model_dump_json()

    monkeypatch.setattr(llm, "complete", complete)
    poller.dispatch(mail, Category.RFQ, sid)
    assert case_worker.process_once()
    assert db.case_latest(sid)["status"] == "done"
    assert transcribed == [(".mp3", sid)]
    assert len(sent) == 1 and len(sent[0]["attachments"]) == 1
    with pdfplumber.open(
        io.BytesIO(base64.b64decode(sent[0]["attachments"][0]["content_base64"]))
    ) as doc:
        assert len(doc.pages) == 2
        assert "JFK" in doc.pages[0].extract_text()
        assert "SIN" in doc.pages[1].extract_text()
        assert "200 kg" in doc.pages[1].extract_text()
    assert len(db.get_session(sid)["quotes"]) == 1
    # Exact duplicate delivery cannot restart the job or send another quote.
    poller.dispatch(mail, Category.RFQ, sid)
    assert not case_worker.process_once() and len(sent) == 1
    assert all("capabilities" in c for c in contexts if "proposal" not in c)


def test_new_input_invalidates_old_plan_and_transport_even_before_new_job_enqueues(setup):
    _, sent = setup
    sid, mail = incoming()
    ctx = runtime(sid, mail)
    db.case_set_context(ctx.job["id"], ctx.owner)
    incoming(101, "Wait. The weight is now 200 kg.", parent=100)
    with pytest.raises(RuntimeError, match="changed"):
        db.case_transport_guard()
    assert not sent


def test_expired_lease_with_inflight_send_is_never_replayed(setup):
    sid, mail = incoming()
    ctx = runtime(sid, mail)
    receipt = db.case_step(ctx.job["id"], "send", "deliver_quote", "{}")
    db.case_step_state(receipt["id"], "dispatching")
    assert db.case_claim("replacement", time.time() + 100) is None
    assert db.case_latest(sid)["status"] == "unknown"


def test_restart_resumes_safe_work_and_keeps_completed_receipts(setup):
    sid, mail = incoming()
    ctx = runtime(sid, mail)
    receipt = db.case_step(ctx.job["id"], "read", "read_source", "{}")
    db.case_step_state(receipt["id"], "done", {"source": "mail-100"})
    db.case_save(ctx.job["id"], ctx.owner, ctx.workspace.model_dump(mode="json"))
    replacement = db.case_claim("replacement", time.time() + 100)
    assert replacement and replacement["id"] == ctx.job["id"]
    assert db.case_steps(replacement["id"])[0]["result"] == {"source": "mail-100"}


def test_unknown_condition_becomes_a_question_and_waits_then_resumes(setup, monkeypatch):
    _, sent = setup
    body = "Please confirm continuous temperature between 2 and 8 degrees."
    sid, mail = incoming(body=body)
    obligation = task("temperature", "mail-100", body)
    plans = iter(
        [
            step(
                "ask_party",
                {
                    "recipient": "customer",
                    "question": "Which temperature service is required?",
                    "task_ids": ["temperature"],
                    "evidence": [obligation.evidence[0].model_dump()],
                },
                [obligation],
            ),
            step("wait_for_reply", {"reason": "Awaiting service details"}),
        ]
    )

    def complete(purpose, *a, **kw):
        if purpose == "case_audit":
            return Audit(findings=[], missing_tasks=[], unsupported_claims=[]).model_dump_json()
        return next(plans).model_dump_json()

    monkeypatch.setattr(llm, "complete", complete)
    poller.dispatch(mail, Category.RFQ, sid)
    assert case_worker.process_once()
    assert db.case_latest(sid)["status"] == "waiting" and len(sent) == 1
    _, reply = incoming(1100, "Use our existing temperature-controlled service.", parent=100)
    poller.dispatch(reply, Category.CUSTOMER, sid)
    latest = db.case_latest(sid)
    assert latest["version"] == 2 and latest["status"] == "pending"
    assert "temperature" in latest["workspace"]["tasks"]


def test_wait_wakeup_cannot_be_lost_while_a_tool_is_running(setup):
    sid, mail = incoming()
    ctx = runtime(sid, mail)
    db.case_wake(sid)
    db.case_save(ctx.job["id"], ctx.owner, ctx.workspace.model_dump(mode="json"), "waiting")
    assert db.case_latest(sid)["status"] == "pending"


def test_coverage_review_adds_a_missed_task_and_blocks_delivery(setup, monkeypatch):
    sid, mail = incoming(body="Quote FRA to JFK. Also provide a PDF.")
    ctx = runtime(sid, mail)
    missing = task("document", "mail-100", "Also provide a PDF.")
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **kw: Audit(
            findings=[], missing_tasks=[missing], unsupported_claims=[]
        ).model_dump_json(),
    )
    with pytest.raises(ToolError, match="every active task"):
        case_tools.audit(ctx, {"kind": "quote"})
    assert "document" in ctx.workspace.tasks


def test_cannot_drop_tasks_cite_invented_sources_or_create_dependency_cycles(setup):
    sid, mail = incoming(body="Quote FRA to JFK.")
    ctx = runtime(sid, mail)
    original = task("quote", "mail-100", mail.body)
    case_tools.add_tasks(ctx.workspace, [original])
    case_tools.add_tasks(ctx.workspace, [])
    assert "quote" in ctx.workspace.tasks
    with pytest.raises(ToolError, match="exact passage"):
        case_tools.add_tasks(ctx.workspace, [task("invented", "mail-100", "Customer approved")])
    cyclic = original.model_copy(update={"id": "cycle", "depends_on": ["cycle"]})
    with pytest.raises(ToolError, match="cycle"):
        case_tools.add_tasks(ctx.workspace, [cyclic])


def test_new_combined_selection_condition_uses_real_options_and_exact_money(setup, monkeypatch):
    pricing(monkeypatch)
    sentence = "Use the fastest service if it costs at most EUR 150 more than the cheapest."
    body = "FRA to JFK, 2 pieces, 100 kg gross, 1 cbm total. " + sentence
    sid, mail = incoming(body=body)
    ctx = runtime(sid, mail)
    obligation = task("quote", "mail-100", body)
    case_tools.add_tasks(ctx.workspace, [obligation])
    result = case_tools.prepare(
        ctx,
        case_tools.Prepare(
            shipment_id="one",
            request_json=Rfq(
                origin="FRA", destination="JFK", weight_kg=100, volume_cbm=1, pieces=2
            ).model_dump_json(),
            evidence=obligation.evidence,
            task_ids=["quote"],
        ),
    )
    assert result["prepared"]
    case_tools.price(ctx, case_tools.Select(shipment_ids=["one"]))
    expression = {
        "op": "le",
        "args": [
            {"ref": "candidate.total_eur"},
            {
                "op": "add",
                "args": [
                    {"ref": "cheapest.total_eur"},
                    {
                        "value": "150",
                        "unit": "EUR",
                        "evidence": {"source": "mail-100", "text": sentence},
                    },
                ],
            },
        ],
    }
    chosen = case_tools.choose(
        ctx,
        case_tools.Choose(
            shipment_id="one",
            eligibility_json=json.dumps(expression),
            score_json=json.dumps({"ref": "candidate.transit_days"}),
            evidence=obligation.evidence,
        ),
    )
    assert chosen["selected"]["airline"] == "Express" and chosen["total_eur_minor"] == 110000


@pytest.mark.parametrize(
    "expression",
    [
        {"op": "add", "args": [{"ref": "money"}, {"ref": "time"}]},
        {"ref": "__import__('os').system('anything')"},
        {"op": "div", "args": [{"ref": "money"}, {"ref": "zero"}]},
        {"value": "999", "unit": "EUR", "evidence": {"source": "mail-100", "text": "EUR 150"}},
    ],
)
def test_expressions_reject_mixed_units_execution_and_unevidenced_values(expression):
    state = Workspace(
        sources={
            "mail-100": Source(
                id="mail-100", mail_id=100, role="customer", name="RFQ", text="EUR 150"
            )
        }
    )
    values = {
        "money": Value(Decimal(100), "EUR"),
        "time": Value(Decimal(1), "days"),
        "zero": Value(Decimal(0), "scalar"),
    }
    with pytest.raises(ToolError):
        evaluate(expression, values, state)


def test_human_controls_and_case_ownership_block_legacy_watchdog(setup):
    sid, mail = incoming()
    ctx = runtime(sid, mail)
    db.takeover(sid)
    with pytest.raises(ToolError, match="human controls"):
        ctx.guard()
    assert watchdog.force_quote(sid) == "case_worker"
    assert not db.case_claim("other", time.time())


def test_required_pdf_is_not_silently_removed_after_mailbox_refusal(setup, monkeypatch):
    sent = []

    def refuse(*a, **kw):
        sent.append(kw)
        raise hackloop.HackloopError("POST", "/v1/mailbox/messages", 422, "attachment refused")

    monkeypatch.setattr(hackloop, "send", refuse)
    with pytest.raises(hackloop.HackloopError):
        hackloop.send_customer(
            CUSTOMER,
            "Your offer",
            "Hello,\n\nHere is your offer.\n\nThanks",
            language_code="en",
            review=False,
            attachments=[
                {
                    "filename": "offer.pdf",
                    "content_type": "application/pdf",
                    "content_base64": "AA==",
                }
            ],
            require_attachments=True,
        )
    assert len(sent) == 1


def test_case_call_budget_is_persisted_per_input_not_spent_by_old_correspondence(setup):
    sid, mail = incoming()
    ctx = runtime(sid, mail)
    assert db.reserve_case_call(sid, ctx.job["id"], 1)
    assert not db.reserve_case_call(sid, ctx.job["id"], 1)
    _, reply = incoming(101, "Use 200 kg instead.", parent=100)
    db.case_enqueue(sid, reply.id)
    new_job = db.case_claim("new-owner", time.time())
    assert new_job and db.reserve_case_call(sid, new_job["id"], 1)
    assert not db.reserve_case_call(sid + 1, new_job["id"], 2)


def test_restart_after_terminal_receipt_finishes_without_another_model_or_send(setup, monkeypatch):
    sid, mail = incoming()
    ctx = runtime(sid, mail)
    receipt = db.case_step(ctx.job["id"], "send", "deliver_quote", "{}")
    db.case_step_state(receipt["id"], "done", {"mail_id": 1000})
    db.case_heartbeat(ctx.job["id"], ctx.owner, time.time() - 1000)
    monkeypatch.setattr(
        llm, "complete", lambda *a, **kw: pytest.fail("Terminal receipt must settle job")
    )
    assert case_worker.process_once()
    assert db.case_latest(sid)["status"] == "done"


def test_later_pdf_request_uses_frozen_offer_without_pricing_again(setup, monkeypatch):
    _, sent = setup
    sid, _mail = incoming()
    request = Rfq(origin="FRA", destination="JFK", weight_kg=100, volume_cbm=1, pieces=2)
    option = RateOption("Economy", "FRA", "JFK", 4, 100000, "EUR", 100000, "sheet")
    db.set_request(sid, request.model_dump())
    db.emit(
        "pricing_audit",
        "Frozen offer",
        sid,
        request=request.model_dump(),
        selected=dataclasses.asdict(option),
        options=[dataclasses.asdict(option)],
        trucking=None,
    )
    db.record_quote(
        sid,
        total_minor=100000,
        currency="EUR",
        airline="Economy",
        origin="FRA",
        destination="JFK",
        transit_days=4,
        source="sheet",
    )
    _, reply = incoming(101, "Please send that offer as a PDF.", parent=100)
    ctx = runtime(sid, reply)
    loaded = case_tools.load_offer(ctx, case_tools.Empty())
    result = case_tools.pdf(ctx, case_tools.Select(shipment_ids=loaded["shipment_ids"]))
    monkeypatch.setattr(case_delivery, "audit", lambda *a, **kw: None)
    case_tools.answer(
        ctx,
        case_tools.Answer(
            body="Hello,\n\nHere is the requested copy of your offer.\n\nYour austrian Power Team",
            artifact_ids=[result["artifact_id"]],
        ),
    )
    assert len(sent) == 1 and len(sent[0]["attachments"]) == 1
    assert len(db.get_session(sid)["quotes"]) == 1
    with pdfplumber.open(
        io.BytesIO(base64.b64decode(sent[0]["attachments"][0]["content_base64"]))
    ) as document:
        assert "JFK" in document.pages[0].extract_text()


def test_uncertain_question_transport_stops_without_automatic_retry(setup, monkeypatch):
    sid, mail = incoming(body="Please quote this shipment.")
    obligation = task("details", "mail-100", mail.body)
    proposal = step(
        "ask_party",
        {
            "recipient": "customer",
            "question": "What are the weight and volume?",
            "task_ids": ["details"],
            "evidence": [obligation.evidence[0].model_dump()],
        },
        [obligation],
    )
    monkeypatch.setattr(
        llm,
        "complete",
        lambda purpose, *a, **kw: (
            Audit(findings=[], missing_tasks=[], unsupported_claims=[]).model_dump_json()
            if purpose == "case_audit"
            else proposal.model_dump_json()
        ),
    )
    attempts = []

    def uncertain(*a, **kw):
        attempts.append(1)
        db.case_transport_guard()
        raise TimeoutError("Delivery may have happened")

    monkeypatch.setattr(hackloop, "send_customer", uncertain)
    poller.dispatch(mail, Category.RFQ, sid)
    assert case_worker.process_once()
    assert db.case_latest(sid)["status"] == "unknown"
    assert not case_worker.process_once() and len(attempts) == 1


def test_fresh_carrier_evidence_allows_replanning_with_the_same_tool_arguments(setup, monkeypatch):
    sid, mail = incoming()
    attempts = []

    def investigate(ctx, args):
        attempts.append(1)
        if len(attempts) == 1:
            db.ingest_mail(
                mail_id=101,
                sender="carrier@hackloop.mail",
                recipient=TEAM,
                subject="Service evidence",
                body="The service is available.",
                category="carrier_bid",
                references=[100],
            )
            raise ToolError("New carrier evidence arrived; retry the check")
        return {"confirmed": True}

    monkeypatch.setitem(
        case_tools.TOOLS,
        "investigate_service",
        case_tools.Tool("Read newly available service evidence", case_tools.Empty, investigate),
    )
    plans = iter(
        [
            step("investigate_service", {}),
            step("investigate_service", {}),
            step("request_review", {"reason": "Stop this isolated evaluation"}),
        ]
    )
    monkeypatch.setattr(llm, "complete", lambda *a, **kw: next(plans).model_dump_json())
    poller.dispatch(mail, Category.RFQ, sid)
    assert case_worker.process_once()
    assert len(attempts) == 2
    assert db.case_latest(sid)["status"] == "review"


def test_customer_can_replace_an_unreadable_old_attachment_with_current_written_facts(setup):
    sid, mail = incoming(attachments=[{"id": 1, "filename": "broken.pdf"}])
    ctx = runtime(sid, mail)
    old_source = "mail-100-attachment-1"
    ctx.workspace.sources[old_source].problem = "Unreadable"
    assert db.case_save(ctx.job["id"], ctx.owner, ctx.workspace.model_dump(mode="json"), "waiting")
    correction = "Ignore the broken PDF. The shipment is FRA to JFK, 100 kg gross, 1 cbm total."
    _, reply = incoming(101, correction, parent=100)
    newer = runtime(sid, reply)
    replacement = case_tools.ReplaceSource(
        source_id=old_source, evidence=Citation(source="mail-101", text=correction)
    )
    case_tools.replace_source(newer, replacement)
    assert newer.workspace.sources[old_source].superseded_by == replacement.evidence


def test_successful_business_action_does_not_close_other_unanswered_tasks(setup, monkeypatch):
    from app.agents import customer_reply

    sid, mail = incoming(body="Please book and provide the documentation.")
    db.record_quote(
        sid,
        total_minor=100000,
        currency="EUR",
        airline="Economy",
        origin="FRA",
        destination="JFK",
        transit_days=4,
        source="sheet",
    )
    ctx = runtime(sid, mail)
    case_tools.add_tasks(
        ctx.workspace,
        [
            task("booking", "mail-100", "Please book"),
            task("documents", "mail-100", "provide the documentation."),
        ],
    )

    def booking_reply(*args):
        db.record_outgoing(
            mail_id=1000,
            recipient=CUSTOMER,
            subject="Booking",
            body="Booked",
            category="booking_confirm",
            session_id=sid,
            in_reply_to=100,
        )

    monkeypatch.setattr(customer_reply, "process", booking_reply)
    result = case_tools.delegate(ctx, case_tools.Delegate(workflow="booking"))
    assert result["completed"] and ctx.stop_state is None
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **kw: Audit(
            findings=[
                Finding(
                    task_id="booking",
                    status="satisfied",
                    evidence_refs=["mail-1000"],
                    reason="Confirmed",
                ),
                Finding(
                    task_id="documents",
                    status="unmet",
                    evidence_refs=[],
                    reason="Document not sent",
                ),
            ],
            missing_tasks=[],
            unsupported_claims=[],
        ).model_dump_json(),
    )
    discover(sid, ctx.workspace)
    with pytest.raises(ToolError, match="remains open"):
        case_tools.complete(ctx, case_tools.Empty())
    assert ctx.stop_state is None


def test_case_bid_slots_resume_real_replies_and_isolate_changed_requests(setup):
    from app.agents import bids
    from app.pricing.reference import carriers

    sid, mail = incoming()
    ctx = runtime(sid, mail)
    carrier = next(c for c in carriers() if c.kind == "airline")
    old_slot = bids.case_slot(sid, ctx.job["id"], "shipment-one-100kg")
    changed_slot = bids.case_slot(sid, ctx.job["id"], "shipment-one-200kg")
    assert old_slot != changed_slot and bids.owner(old_slot) == sid
    db.record_outgoing(
        mail_id=1000,
        recipient=carrier.email,
        subject="Bid request",
        body="FRA to JFK, 100 kg",
        category="bid_request",
        session_id=sid,
    )
    db.kv_set("bid.slot.1000", str(old_slot))
    db.ingest_mail(
        mail_id=1001,
        sender=carrier.email,
        recipient=TEAM,
        subject="Bid reply",
        body="Our all-in total is EUR 200. Transit time is 2 days.",
        category="carrier_bid",
        in_reply_to=1000,
        references=[1000],
    )
    try:
        bids.resume_case_slot(sid, old_slot)
        assert len(bids.offers(old_slot)) == 1
        bids.close(old_slot)
        bids.resume_case_slot(sid, old_slot)
        assert len(bids.offers(old_slot)) == 1
        bids.resume_case_slot(sid, changed_slot)
        assert not bids.offers(changed_slot)
    finally:
        bids.close(old_slot)
        bids.close(changed_slot)
