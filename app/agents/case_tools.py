"""Composable investigation, pricing and business capabilities for case planning."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal

from pydantic import BaseModel

from app import db, hackloop
from app.agents import bids, rfq
from app.agents.case_delivery import answer, audit, deliver, load_offer, pdf
from app.agents.case_models import (
    Shipment,
    ToolError,
)
from app.agents.case_runtime import (
    Answer,
    Ask,
    Choose,
    Delegate,
    Deliver,
    Empty,
    Pause,
    Prepare,
    Read,
    ReplaceSource,
    Runtime,
    Search,
    Select,
    Supersede,
    add_tasks,
    selected,
    unpack,
)
from app.agents.case_sources import cited, discover, read_source, shipment_mail
from app.config import settings
from app.mail import compliance
from app.mail.rfq_validation import validate
from app.mail.style import SIGNATURE
from app.models import RateOption, Rfq
from app.pricing.reference import carrier_by_email, carriers

__all__ = ["add_tasks"]


def inspect_case(ctx: Runtime, args: Empty) -> dict:
    discover(ctx.sid, ctx.workspace)
    session = db.get_session(ctx.sid) or {}
    return {
        "sources": [
            {
                "id": s.id,
                "role": s.role,
                "name": s.name,
                "read": s.read,
                "length": len(s.text),
                "preview": s.text[:1200],
                "problem": s.problem,
            }
            for s in ctx.workspace.sources.values()
        ],
        "carriers": [asdict(c) for c in carriers()],
        "request_schema": Rfq.model_json_schema(),
        "quotes": session.get("quotes", []),
        "bookings": session.get("bookings", []),
        "deadline": session.get("deadline_at"),
    }


def read(ctx: Runtime, args: Read) -> dict:
    return read_source(ctx.sid, ctx.workspace, args.source, args.offset)


def search(ctx: Runtime, args: Search) -> dict:
    hits = []
    for source in ctx.workspace.sources.values():
        start = 0
        while len(hits) < 30:
            index = source.text.casefold().find(args.query.casefold(), start)
            if index < 0:
                break
            hits.append(
                {
                    "source": source.id,
                    "offset": max(0, index - 150),
                    "text": source.text[max(0, index - 150) : index + 500],
                }
            )
            start = index + len(args.query)
    return {"matches": hits}


def prepare(ctx: Runtime, args: Prepare) -> dict:
    if not set(args.task_ids) <= ctx.workspace.tasks.keys():
        raise ToolError("A shipment must name its known tasks")
    source_mail = shipment_mail(ctx.mail, ctx.workspace, args.evidence)
    if refusal := compliance.check(source_mail.body):
        raise ToolError(f"Shipment cannot be offered: {refusal}")
    request = Rfq.model_validate_json(args.request_json)
    if request.additional_shipments:
        raise ToolError("Prepare each shipment separately, with its own sources and conditions")
    checked, draft = validate(source_mail, request)
    if checked is None or draft.issues:
        return {"prepared": False, "issues": draft.issues, "facts": draft.facts}
    key = args.shipment_id
    previous = ctx.workspace.shipments.get(key)
    if previous is None and len(ctx.workspace.shipments) >= 9:
        raise ToolError("A case supports at most nine separately priced shipments")
    slot = previous.slot if previous else len(ctx.workspace.shipments) + 1
    ctx.workspace.shipments[key] = Shipment(
        id=key,
        request=checked.model_dump(mode="json"),
        evidence=args.evidence,
        task_ids=args.task_ids,
        slot=slot,
    )
    ctx.workspace.prices.pop(key, None)
    ctx.workspace.artifacts.clear()
    return {"prepared": True, "shipment_id": key, "request": checked.model_dump(mode="json")}


def price(ctx: Runtime, args: Select) -> dict:
    # The existing engine owns rates, carrier caps, FX, insurance and option selection.
    # No external-send context marker for reads; its bid requests are durably deduplicated.
    results = {}
    for shipment in selected(ctx, args.shipment_ids):
        ctx.guard()
        request = Rfq.model_validate(shipment.request)
        source_mail = shipment_mail(ctx.mail, ctx.workspace, shipment.evidence)
        origin = rfq.resolve_origin(request)
        if origin is None:
            raise ToolError("Shipment needs a supported departure airport or pickup location")
        request_key = json.dumps(
            {"shipment_id": shipment.id, "request": shipment.request}, sort_keys=True
        )
        bid_slot = bids.case_slot(ctx.sid, ctx.job["id"], request_key)
        bids.resume_case_slot(ctx.sid, bid_slot)
        try:
            result = rfq._price(
                source_mail,
                ctx.sid,
                request,
                origin,
                rfq._received(ctx.mail),
                "enforce",
                slot=bid_slot,
            )
        except rfq.SelectionBlocked as exc:
            results[shipment.id] = {
                "priced": False,
                "issues": exc.draft.issues
                if exc.draft
                else [{"field": "arrival_deadline", "code": "no_feasible_option"}],
            }
            continue
        finally:
            bids.close(bid_slot)
        ctx.guard()
        if result is None:
            results[shipment.id] = {"priced": False, "reason": "No complete verified price"}
            continue
        raw = {
            "rfq": result.rfq.model_dump(mode="json"),
            "choice": asdict(result.choice),
            "total": result.total,
            "truck": asdict(result.truck) if result.truck else None,
            "options": [asdict(o) for o in result.options],
            "insurance_minor": result.insurance_minor,
        }
        ctx.workspace.prices[shipment.id] = json.loads(json.dumps(raw, default=str))
        results[shipment.id] = {"priced": True, **ctx.workspace.prices[shipment.id]}
    return {"shipments": results}


def choose(ctx: Runtime, args: Choose) -> dict:
    from app.agents.case_selection import Value, evaluate
    from app.pricing.pricing import to_minor

    cited(ctx.workspace, args.evidence)
    selected(ctx, [args.shipment_id], priced=True)
    current = unpack(ctx.workspace.prices[args.shipment_id])
    if "offer_quote_id" in ctx.workspace.prices[args.shipment_id]:
        raise ToolError("An existing offer is immutable; use the versioned correction workflow")
    options = current.options
    if not options:
        raise ToolError("There are no verified eligible options to compare")

    def total(option: RateOption) -> int:
        base = option.total_eur_minor + (to_minor(current.truck.total_eur) if current.truck else 0)
        if settings.price_adjust_pct:
            base = to_minor(
                Decimal(base) / 100 * (100 - Decimal(str(settings.price_adjust_pct))) / 100
            )
        return base + current.insurance_minor

    cheapest = min(options, key=total)
    fastest = min(options, key=lambda o: (o.transit_days, total(o)))
    eligibility, scoring = json.loads(args.eligibility_json), json.loads(args.score_json)
    ranked = []
    for option in options:
        values = {}
        for prefix, candidate in (
            ("candidate", option),
            ("cheapest", cheapest),
            ("fastest", fastest),
        ):
            values[prefix + ".total_eur"] = Value(Decimal(total(candidate)) / 100, "EUR")
            values[prefix + ".transit_days"] = Value(Decimal(candidate.transit_days), "days")
        permitted = evaluate(eligibility, values, ctx.workspace)
        score = evaluate(scoring, values, ctx.workspace)
        if permitted.unit != "bool" or score.unit == "bool":
            raise ToolError("Eligibility must be boolean and score must be numeric")
        if permitted.value:
            ranked.append((score.value, total(option), option.transit_days, option))
    if not ranked:
        raise ToolError("No real option fulfils the evidenced selection conditions")
    choice = min(ranked, key=lambda row: row[:3])[-1]
    result = ctx.workspace.prices[args.shipment_id]
    result["choice"], result["total"] = asdict(choice), total(choice)
    result["selection_rule"] = {
        "eligibility": eligibility,
        "score": scoring,
        "evidence": [citation.model_dump() for citation in args.evidence],
    }
    ctx.workspace.artifacts.clear()
    return {
        "shipment_id": args.shipment_id,
        "selected": asdict(choice),
        "total_eur_minor": total(choice),
        "selection_rule": result["selection_rule"],
    }


def ask(ctx: Runtime, args: Ask) -> dict:
    cited(ctx.workspace, args.evidence)
    if not set(args.task_ids) <= ctx.workspace.tasks.keys():
        raise ToolError("Question must name known tasks")
    customer = args.recipient == "customer"
    carrier = None if customer else carrier_by_email(args.recipient)
    if not customer and carrier is None:
        raise ToolError("Recipient must be this customer or a carrier in the trusted catalog")
    if carrier is not None:
        from app.agents import carrier_prefs

        banned = carrier_prefs.excluded(ctx.mail.sender)
        banned.update(
            name
            for shipment in ctx.workspace.shipments.values()
            for name in shipment.request.get("excluded_airlines", [])
        )
        if carrier.name.lower() in banned:
            raise ToolError("Customer excluded this carrier")
        contacted = set()
        for row in (db.get_session(ctx.sid) or {}).get("mails", []):
            party = (
                carrier_by_email(row.get("recipient") or "") if row["direction"] == "out" else None
            )
            if party is not None and party.kind == carrier.kind:
                contacted.add(party.email)
        limit = (
            settings.max_bid_carriers if carrier.kind == "airline" else settings.max_trucker_bids
        )
        if carrier.email not in contacted and len(contacted) >= limit:
            raise ToolError("The existing carrier contact limit is exhausted")
    audit(
        ctx,
        {"question": args.question, "recipient": args.recipient, "task_ids": args.task_ids},
        question=True,
    )
    # A second phrasing cannot repeat the same external request for the same tasks/party.
    identity = sorted(args.task_ids), args.recipient
    for observation in ctx.workspace.observations:
        if (
            observation.ok
            and observation.tool == "ask_party"
            and observation.result.get("identity") == list(identity)
        ):
            raise ToolError("Already asked these tasks; wait for the party's reply")
    ctx.effect()
    body = "Hello,\n\n" + args.question + "\n\n" + SIGNATURE + "\n"
    recipient = ctx.mail.sender if customer else carrier.email
    send = hackloop.send_customer if customer else hackloop.send
    options = {"session_id": ctx.sid, "review": False} if customer else {}
    sent = send(
        recipient, f"[AP-{ctx.sid}] Shipment question", body, in_reply_to=ctx.mail.id, **options
    )
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=recipient,
        subject=sent.get("subject", "Shipment question"),
        body=sent.get("body", body),
        category="case_question",
        session_id=ctx.sid,
        in_reply_to=ctx.mail.id,
        raw=sent,
    )
    return {
        "mail_id": sent["id"],
        "recipient": recipient,
        "identity": list(identity),
        "awaiting_reply": True,
    }


def supersede(ctx: Runtime, args: Supersede) -> dict:
    if args.task_id not in ctx.workspace.tasks:
        raise ToolError("Unknown task")
    cited(ctx.workspace, [args.evidence])
    if ctx.workspace.sources[args.evidence.source].mail_id != ctx.mail.id:
        raise ToolError("Only the current customer input can supersede an obligation")
    # Semantic validity is checked again against the original sources before delivery.
    ctx.workspace.superseded[args.task_id] = args.evidence
    return {"superseded": args.task_id, "evidence": args.evidence.model_dump()}


def replace_source(ctx: Runtime, args: ReplaceSource) -> dict:
    source = ctx.workspace.sources.get(args.source_id)
    if source is None or source.attachment_id is None:
        raise ToolError("Only a known attachment may be replaced")
    cited(ctx.workspace, [args.evidence])
    replacement = ctx.workspace.sources[args.evidence.source]
    if replacement.mail_id != ctx.mail.id or replacement.id == source.id:
        raise ToolError("A replacement requires explicit evidence from the current customer input")
    source.superseded_by = args.evidence
    return {"source_id": source.id, "replaced_by": args.evidence.model_dump()}


def wait(ctx: Runtime, args: Pause) -> dict:
    if not any(o.ok and o.tool == "ask_party" for o in ctx.workspace.observations):
        raise ToolError("There is no pending external question to wait for")
    ctx.stop_state = "waiting"
    return {"waiting": True, "reason": args.reason}


def review(ctx: Runtime, args: Pause) -> dict:
    db.emit("case_needs_review", args.reason, ctx.sid, level="error", job_id=ctx.job["id"])
    ctx.stop_state = "review"
    return {"needs_review": True, "reason": args.reason}


def delegate(ctx: Runtime, args: Delegate) -> dict:
    from app.agents import customer_reply, tracking

    ctx.guard()
    session = db.get_session(ctx.sid) or {}
    if args.workflow in ("booking", "customer_reply"):
        if not session.get("quotes"):
            raise ToolError("There is no current offer to accept or revise")
        if any("shipment" in option for q in session["quotes"] for option in q["alternatives"]):
            raise ToolError(
                "Multiple-offer booking requires an explicit shipment target and human review"
            )
        ctx.effect()
        customer_reply.process(ctx.mail, ctx.sid)
    else:
        ctx.effect()
        tracking.handle(ctx.mail, ctx.sid)
    latest = db.get_session(ctx.sid) or {}
    new_mails = [
        m["id"]
        for m in latest.get("mails", [])
        if m["direction"] == "out" and m["id"] not in {r["id"] for r in session.get("mails", [])}
    ]
    # A rejected business operation is feedback, never a false successful completion.
    phase = (latest.get("workflow") or {}).get("phase")
    if phase == "needs_review" or (
        not new_mails and latest.get("bookings") == session.get("bookings")
    ):
        return {
            "completed": False,
            "workflow": args.workflow,
            "open_items": (latest.get("workflow") or {}).get("open_items", []),
            "reason": "Business workflow did not produce a confirmed result",
        }
    return {"completed": True, "workflow": args.workflow, "mail_ids": new_mails}


def complete(ctx: Runtime, args: Empty) -> dict:
    audit(
        ctx,
        {
            "kind": "completion",
            "receipts": [
                observation.model_dump(mode="json")
                for observation in ctx.workspace.observations
                if observation.ok
            ],
        },
    )
    ctx.stop_state = "done"
    return {"completed": True, "all_tasks_checked": True}


@dataclass(frozen=True)
class Tool:
    description: str
    arguments: type[BaseModel]
    run: Callable


TOOLS: dict[str, Tool] = {
    "complete_case": Tool(
        "After business actions, independently verify that EVERY remaining task is fulfilled before closing the case",
        Empty,
        complete,
    ),
    "replace_source": Tool(
        "Record an explicit current customer replacement of an obsolete/unreadable attachment; final coverage review verifies the replacement",
        ReplaceSource,
        replace_source,
    ),
    "read_current_offer": Tool(
        "Load immutable offered shipment snapshots to answer questions or regenerate a requested PDF",
        Empty,
        load_offer,
    ),
    "inspect_case": Tool(
        "Discover messages, files, trusted carriers and the shipment schema", Empty, inspect_case
    ),
    "read_source": Tool(
        "Read/transcribe one file or read a 16000-character source page", Read, read
    ),
    "search_sources": Tool("Find passages and offsets in already read sources", Search, search),
    "prepare_shipment": Tool(
        "Validate one sourced shipment; preserves independent service conditions", Prepare, prepare
    ),
    "price_shipments": Tool(
        "Run deterministic pricing for prepared shipments; never sends a quote", Select, price
    ),
    "create_quote_pdf": Tool(
        "Create and inspect a multipage PDF from priced shipment IDs", Select, pdf
    ),
    "select_by_expression": Tool(
        "Choose among real eligible options using composable comparisons/arithmetic. Nodes are "
        "{ref: candidate.total_eur}, {bool: true}, {value: decimal string, unit: EUR/days/scalar, "
        "evidence: {source,text}}, or {op: add/sub/mul/div/eq/ne/lt/le/gt/ge/and/or/not,args: [...]}. "
        "Available references are candidate/cheapest/fastest.total_eur and .transit_days. "
        "Costs include trucking and insurance. Unsupported service facts remain unresolved.",
        Choose,
        choose,
    ),
    "ask_party": Tool(
        "Ask this customer or a trusted carrier for missing facts; no booking", Ask, ask
    ),
    "supersede_task": Tool(
        "Record a customer's explicit current withdrawal/replacement of an old task",
        Supersede,
        supersede,
    ),
    "wait_for_reply": Tool(
        "Persist work and wait after a question; new input resumes it", Pause, wait
    ),
    "request_review": Tool(
        "Surface an unsupported capability or unresolved ambiguity to a person", Pause, review
    ),
    "deliver_quote": Tool(
        "Audit all tasks and send one quote with verified real PDF attachments", Deliver, deliver
    ),
    "answer_questions": Tool(
        "Audit and send a factual answer grounded in existing offers", Answer, answer
    ),
    "run_business_workflow": Tool(
        "Use existing guarded booking, versioned correction or tracking workflow",
        Delegate,
        delegate,
    ),
}


def catalog() -> dict:
    return {
        name: {"description": tool.description, "arguments": tool.arguments.model_json_schema()}
        for name, tool in TOOLS.items()
    }
