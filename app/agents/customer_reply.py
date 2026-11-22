"""One durable owner per customer thread; independent threads use a bounded worker pool."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from app import db, hackloop
from app.config import settings
from app.mail import multilingual
from app.mail.reply_intent import ReplyDecision, interpret, validate_evidence
from app.mail.router import address
from app.mail.style import SIGNATURE, amount, written_date
from app.models import Mail, Rfq
from app.quotes import active

log = logging.getLogger(__name__)


def enabled() -> bool:
    return settings.reply_workflow_mode == "enforce"


def enqueue(mail: Mail, sid: int) -> bool:
    if settings.reply_workflow_mode == "off":
        return False
    db.reply_enqueue(sid, dataclasses.asdict(mail), shadow=not enabled())
    return enabled()


def owns(sid: int) -> bool:
    return enabled() and db.reply_workflow(sid) is not None


def guard(sid: int, mail: Mail, version: int) -> None:
    if not db.reply_current(sid, mail.id, version):
        raise hackloop.CustomerSendBlocked(
            "Newer customer input or thread control superseded this action"
        )


def _notice(mail: Mail, sid: int, en: str, de: str, *, category: str = "workflow_answer") -> None:
    state = db.reply_workflow(sid)
    assert state is not None
    version = state["request_version"]
    effect = db.reply_effect(sid, mail.id, category, version)
    if effect["status"] == "done":
        return
    if effect["status"] == "dispatching":
        raise RuntimeError("Previous customer send has an unknown outcome")
    lang = multilingual.mail_language(mail)
    body = (
        f"{'Hallo' if lang == 'de' else 'Hello'},\n\n{de if lang == 'de' else en}\n\n{SIGNATURE}\n"
    )

    def before_send() -> None:
        guard(sid, mail, version)
        db.reply_effect(sid, mail.id, category, version, dispatch=True)

    sent = hackloop.send_customer(
        address(mail.sender),
        mail.subject,
        body,
        in_reply_to=mail.id,
        session_id=sid,
        review=False,
        language_code=lang,
        guard_transport=before_send,
    )
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=address(mail.sender),
        subject=sent["subject"],
        body=sent["body"],
        category=category,
        session_id=sid,
        sender=settings.team_mailbox,
        in_reply_to=mail.id,
        raw=sent,
    )
    db.reply_effect(sid, mail.id, category, version, result={"mail_id": sent["id"]})


def _review(mail: Mail, sid: int, items: list[str], *, notify: bool = True) -> None:
    state = db.reply_workflow(sid) or {}
    repeated = state.get("open_items") == items
    db.reply_phase(sid, "needs_review", open_items=items)
    db.emit(
        "reply_needs_review",
        "Kundenanliegen benötigt gezielte Prüfung",
        sid,
        level="warn",
        issues=items,
        mail_id=mail.id,
    )
    if notify and not repeated:
        booked_already = any(
            b.get("status") in ("booked", "confirmed")
            for b in (db.get_session(sid) or {}).get("bookings", [])
        )
        if booked_already:
            _notice(
                mail,
                sid,
                "Your shipment already has a booking. I need to check your message before I can confirm any further details or changes.",
                "Deine Sendung ist bereits gebucht. Ich muss deine Nachricht prüfen, bevor ich weitere Angaben oder Änderungen bestätigen kann.",
                category="workflow_review",
            )
            return
        _notice(
            mail,
            sid,
            "Thanks for your message. I still need to verify the open details before this shipment can be booked. We have not booked it.",
            "Danke dir für deine Nachricht. Ich muss die offenen Angaben noch prüfen, bevor die Sendung gebucht werden kann. Wir haben noch nicht gebucht.",
            category="workflow_review",
        )


def _target_offer(mail: Mail, session: dict) -> bool:
    quote = active(session)
    if not quote:
        return False
    old = {q.get("mail_id") for q in session["quotes"] if q["id"] != quote["id"]}
    parent = mail.raw.get("in_reply_to")
    if parent in old:
        return False
    # A full chain which refers to old offers but omits the current offer is stale.
    if old.intersection(mail.references) and quote.get("mail_id") not in mail.references:
        return False
    return datetime.fromisoformat(mail.received_at) >= datetime.fromisoformat(quote["sent_at"])


def _revise(mail: Mail, sid: int, session: dict, decision: ReplyDecision) -> None:
    from app.agents import bids, rfq
    from app.mail.rfq_validation import validate

    if (session.get("request") or {}).get("additional_shipments"):
        db.reply_suspend_offer(sid)
        _review(
            mail,
            sid,
            ["Bei mehreren Sendungen muss die Änderung zuerst eindeutig zugeordnet werden"],
        )
        return
    updated = dict(session.get("request") or {})
    evidence = {}
    for change in decision.changes:
        value = json.loads(change.value_json)
        updated[change.field] = value
        evidence[change.field] = {"value": value, "evidence": change.evidence, "mail_id": mail.id}
    if updated == session.get("request") and active(session) is not None:
        db.reply_phase(sid, "awaiting_customer")
        return
    from app.mail.rfq_validation import numeric_facts

    for field in ("pieces", "weight_kg", "volume_cbm"):
        if field not in evidence:
            continue
        for text in session.get("reply_sources", {}).values():
            observed = numeric_facts(text, expect=updated)
            if field in observed and abs(float(observed[field]) - float(updated[field])) > (
                0.01 if field == "volume_cbm" else 0.1
            ):
                db.reply_suspend_offer(sid)
                _review(mail, sid, ["Aktuelle Nachricht und aktueller Anhang widersprechen sich"])
                return
    # Unchanged fields below are inherited from the previous stored request; new values
    # were independently checked against the current message or its current attachments.
    # Invalidate the old offer even when the replacement is incomplete or inconsistent.
    version = db.reply_revise(sid, mail.id, updated, evidence)
    try:
        request = Rfq.model_validate(updated)
        # Inherit agreed insurance only when this correction says nothing about it.
        from app.pricing import insurance

        current_sources = [mail.body, *session.get("reply_sources", {}).values()]
        inherited_policy = (
            {}
            if any(
                insurance.requested(text) or insurance.declined(text) for text in current_sources
            )
            else {"insurance": updated.get("insurance")}
        )
        checked, draft = validate(
            mail,
            request,
            session.get("reply_sources", {}),
            [],
            {
                **inherited_policy,
                **{
                    key: value
                    for key, value in updated.items()
                    if key
                    in (
                        "origin",
                        "destination",
                        "pickup_zip",
                        "pickup_city",
                        "ready_date",
                        "arrival_deadline",
                        "preference",
                        "pieces",
                        "weight_kg",
                        "volume_cbm",
                    )
                    and value is not None
                },
            },
        )
        if checked is None or draft.issues:
            _review(
                mail,
                sid,
                [f"{i.get('field')}: {i.get('code')}" for i in draft.issues]
                or ["Unvollständige Sendungsdaten"],
            )
            return
        draft.evidence["corrections"] = {
            **draft.evidence.get("corrections", {}),
            "preference": checked.preference,
            "insurance": checked.insurance.model_dump() if checked.insurance else None,
            **({"arrival_deadline": checked.arrival_deadline} if checked.arrival_deadline else {}),
        }
        db.validation_save(sid, "ready", draft.model_dump(mode="json"), dataclasses.asdict(mail))
        request = rfq.with_stated_origin(checked, mail, sid)
        origin = rfq.resolve_origin(request)
        if not origin:
            raise ValueError("Unbekannter Abflughafen")
    except (ValueError, TypeError) as exc:
        _review(mail, sid, [str(exc)[:200]])
        return
    guard(sid, mail, version)
    bids.close(sid)
    try:
        rfq._price_and_quote(
            mail, sid, request, origin, rfq._received(mail), "enforce", revision=version
        )
    finally:
        bids.close(sid)
    if active(db.get_session(sid) or {}) is None:
        _review(
            mail, sid, ["Für die geänderten Sendungsdaten fehlt ein gültiges vollständiges Angebot"]
        )


def _answer(
    mail: Mail, sid: int, session: dict, questions: list[str], *, invite: bool = True
) -> bool:
    from app.agents import prebook

    query = "\n".join(questions)
    lang = multilingual.mail_language(mail)
    if re.search(r"resend|send.{0,20}again|erneut|nochmals", query, re.IGNORECASE):
        quote = active(session)
        if not quote:
            return False
        request = quote.get("request_snapshot") or {}
        if not request:
            return False
        total = amount(quote["total_minor"], quote["currency"])
        en = (
            f"Here is the current offer for your {request.get('pieces') or ''} pieces, "
            f"{request['weight_kg']:g} kg and {request.get('volume_cbm', 0):g} cbm. "
            f"Air freight is from {quote['origin']} to {quote['destination']} with {quote['airline']}. "
            f"The total is {total}."
        )
        de = (
            f"Hier ist das aktuelle Angebot für deine {request.get('pieces') or ''} Packstücke mit "
            f"{request['weight_kg']:g} kg und {request.get('volume_cbm', 0):g} cbm. "
            f"Die Luftfracht geht von {quote['origin']} nach {quote['destination']} mit {quote['airline']}. "
            f"Der Gesamtpreis beträgt {total}."
        )
        if request.get("pickup_zip") and (quote.get("pricing_snapshot") or {}).get("trucking"):
            en += f" Pickup from {request['pickup_zip']} and trucking to {quote['origin']} are included."
            de += f" Die Abholung in {request['pickup_zip']} und der Transport zum Flughafen {quote['origin']} sind enthalten."
        if request.get("ready_date"):
            ready = datetime.fromisoformat(request["ready_date"]).date()
            en += f" Your cargo is ready on {written_date(ready, 'en')}."
            de += f" Deine Ware ist am {written_date(ready, 'de')} bereit."
        _notice(mail, sid, en, de, category="workflow_offer_copy")
        return True
    answer = prebook.answer(
        query, prebook.facts_for(session), lang, (session.get("request") or {}).get("customer_name")
    )
    if answer.body is None:
        return False
    db.reply_phase(sid, "answering")
    # Existing answer templates are grounded in the selected offer; the shared send boundary
    # still owns customer language, punctuation, protected numbers and tracking invitation.
    paragraphs = answer.body.split("\n\n")
    content = "\n\n".join(paragraphs[1:-1] if invite else paragraphs[1:-2])
    _notice(mail, sid, content, content)
    return True


def process(mail: Mail, sid: int, *, shadow: bool = False) -> None:
    from app.agents import booking, clarification, reply_conditions, tracking

    session = db.get_session(sid)
    if (
        not session
        or address(mail.sender) != address(session.get("customer") or "")
        or db.reply_blocked(mail.id)
    ):
        return
    if db.is_paused(sid) or db.is_manual(sid):
        raise hackloop.CustomerSendBlocked("Thread is paused or manual")
    if not session["quotes"]:
        if not shadow:
            clarification.handle_reply(mail, sid)
        return
    if not shadow:
        db.reply_enroll(sid, mail.id)
        session = db.get_session(sid)
        assert session is not None
    original = mail
    mail = multilingual.prepare(mail, sid, (session.get("request") or {}).get("language") or "und")
    saved = db.reply_decision(sid, mail.id)
    if mail.attachments:
        from app.mail.rfq_sources import read_sources

        texts, media, issues = read_sources(mail, session_id=sid)
        if issues or media:
            if not shadow:
                db.reply_suspend_offer(sid)
                _review(
                    mail,
                    sid,
                    ["Aktueller Anhang benötigt eine lesbare oder widerspruchsfreie Bestätigung"],
                )
            return
        session["reply_sources"] = texts
    try:
        decision = (
            ReplyDecision.model_validate(saved) if saved else interpret(mail.body, session, sid)
        )
        validate_evidence(decision, mail.body, session.get("reply_sources"))
        db.reply_decision(sid, mail.id, decision.model_dump())
    except Exception as exc:  # noqa: BLE001 - schema, gateway and budget errors stop execution
        if shadow:
            db.emit("reply_shadow_error", type(exc).__name__, sid, level="warn", mail_id=mail.id)
            return
        db.reply_suspend_offer(sid)
        _review(mail, sid, ["Die aktuelle Nachricht konnte nicht eindeutig ausgewertet werden"])
        return
    if shadow:
        db.emit(
            "reply_shadow",
            "Kundenantwort nur ausgewertet, keine Aktion",
            sid,
            mail_id=mail.id,
            decision=decision.model_dump(),
        )
        return
    state = db.reply_workflow(sid)
    assert state is not None
    if state["last_mail_id"] > mail.id:
        return  # a delayed older delivery cannot overwrite an already processed newer input
    # Preserve a prior review's open items for repeat-loop detection.
    db.reply_seen(sid, mail.id)
    if any(b.get("status") in ("booked", "confirmed") for b in session.get("bookings", [])):
        if decision.changes:
            _review(mail, sid, ["Änderung einer bereits gebuchten Sendung"], notify=True)
        elif session["bookings"][0].get("customer_state") == booking.PRICE_ASKED:
            if (
                decision.consent == "approve"
                and not decision.conditions
                and not decision.unresolved
            ):
                price_mails = [
                    m
                    for m in session["mails"]
                    if m["direction"] == "out" and m["category"] == "price_change"
                ]
                if not price_mails or (
                    mail.references
                    and price_mails[-1]["id"] not in mail.references
                    and original.raw.get("in_reply_to") != price_mails[-1]["id"]
                ):
                    _review(
                        mail,
                        sid,
                        [
                            "Die Freigabe muss sich auf den zuletzt mitgeteilten Buchungspreis beziehen"
                        ],
                        notify=False,
                    )
                    return
                db.reply_approve_price(sid, mail.id, decision.consent_evidence)
                booking.handle_reply(mail, sid)
                latest = (db.get_session(sid) or {}).get("bookings", [])
                if latest and latest[0].get("customer_state") == booking.PRICE_ACCEPTED:
                    db.reply_phase(sid, "booked")
                else:
                    _review(mail, sid, ["Geänderter Buchungspreis benötigt Prüfung"], notify=False)
            elif decision.consent == "defer":
                db.reply_phase(sid, "awaiting_price_approval")
            else:
                _review(mail, sid, ["Freigabe des geänderten Buchungspreises ist noch offen"])
        elif tracking.is_status_question(mail.body):
            tracking.handle(original, sid, booked_session=sid)
            db.reply_phase(sid, "booked")
        return
    if decision.changes:
        _revise(mail, sid, session, decision)
        return
    if decision.unresolved:
        _review(mail, sid, decision.unresolved)
        return
    quote = active(session)
    if quote is None:
        _review(mail, sid, ["Der aktuelle Angebotsstand muss geprüft werden"])
        return
    if decision.questions and not _answer(
        mail, sid, session, decision.questions, invite=decision.consent == "none"
    ):
        _review(mail, sid, decision.questions)
        return
    if decision.consent in ("approve", "conditional"):
        if state.get("open_items") and not decision.questions:
            _review(mail, sid, state["open_items"], notify=False)
            return
        if not _target_offer(original, session):
            _notice(
                mail,
                sid,
                "Please confirm whether you would like us to book the latest updated offer. The earlier offer has been replaced and has not been booked.",
                "Möchtest du das zuletzt aktualisierte Angebot buchen lassen? Das frühere Angebot wurde ersetzt und wurde nicht gebucht.",
                category="workflow_offer_check",
            )
            db.reply_phase(sid, "awaiting_customer")
            return
        results = [
            (*reply_conditions.check(condition, quote), condition.model_dump())
            for condition in decision.conditions
        ]
        if any(result[0] is not True for result in results):
            _review(mail, sid, [r[1] for r in results if r[0] is not True], notify=False)
            _notice(
                mail,
                sid,
                "I cannot confirm that the current offer meets every condition you named. Would you like me to look for an option that does? We have not booked it.",
                "Ich kann noch nicht bestätigen, dass das aktuelle Angebot alle deine Bedingungen erfüllt. Soll ich nach einer passenden Option suchen? Wir haben noch nicht gebucht.",
                category="workflow_condition",
            )
            return
        if quote.get("valid_until") and datetime.fromisoformat(
            quote["valid_until"]
        ) <= datetime.now(UTC):
            _review(mail, sid, ["Das Angebot ist abgelaufen und benötigt eine erneute Kalkulation"])
            return
        db.reply_approve(
            sid, mail.id, quote["id"], decision.consent_evidence, [r[2] for r in results]
        )
        effect = db.reply_effect(sid, mail.id, "book", state["request_version"])
        if effect["status"] != "pending":
            _review(mail, sid, ["Früherer Buchungsversuch muss geprüft werden"], notify=False)
            return
        guard(sid, mail, state["request_version"])
        booking.handle_reply(mail, sid)
        latest = db.get_session(sid) or {}
        if any(b.get("status") in ("booked", "confirmed") for b in latest.get("bookings", [])):
            db.reply_bind_booking(sid, quote["id"])
            db.reply_effect(
                sid, mail.id, "book", state["request_version"], result={"quote_id": quote["id"]}
            )
            db.reply_phase(sid, "booked")
        else:
            _review(mail, sid, ["Buchung wurde nicht bestätigt"], notify=False)
        return
    if decision.consent == "decline":
        db.reply_phase(sid, "closed")
    elif decision.consent == "defer":
        db.reply_phase(sid, "deferred")
    elif decision.questions:
        db.reply_phase(sid, "awaiting_customer")
    else:
        _review(
            mail, sid, ["Bitte konkretisieren, welches Angebot oder welche Änderung gemeint ist"]
        )


def process_once(owner: str | None = None) -> bool:
    from app.agents import booking

    owner = owner or uuid.uuid4().hex
    row = db.reply_claim(owner, time.time(), kind="reply" if enabled() else "shadow")
    if row is None:
        return False
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(20):
            if not db.reply_heartbeat(row["id"], owner, time.time()):
                break

    pulse = threading.Thread(target=heartbeat, daemon=True, name="reply-lease")
    pulse.start()
    db.reply_set_context(owner, row["id"])
    try:
        with booking._lock(row["session_id"]):
            process(
                Mail(**json.loads(row["payload_json"])),
                row["session_id"],
                shadow=row["kind"] == "shadow",
            )
        if db.reply_unknown_effect(row["session_id"], row["mail_id"]):
            db.reply_phase(
                row["session_id"],
                "needs_review",
                open_items=["Unklarer Versand oder Buchungsausgang"],
            )
            db.reply_finish(row["id"], owner, "unknown", "External action outcome unknown")
            db.emit(
                "reply_unknown",
                "Versand oder Buchung muss manuell geprüft werden",
                row["session_id"],
                level="error",
                mail_id=row["mail_id"],
            )
        else:
            db.reply_finish(row["id"], owner)
    except hackloop.CustomerSendBlocked as exc:
        # Pause is a resumable hold. Superseded work is safely completed; the next mail owns it.
        held = db.is_paused(row["session_id"]) or db.is_manual(row["session_id"])
        db.reply_finish(row["id"], owner, "pending" if held else "done", str(exc))
    except Exception as exc:
        db.reply_phase(row["session_id"], "needs_review", open_items=[type(exc).__name__])
        db.reply_finish(row["id"], owner, "unknown", type(exc).__name__)
        db.emit(
            "reply_failed",
            "Kundenantwort angehalten, externe Aktion nicht blind wiederholen",
            row["session_id"],
            level="error",
            mail_id=row["mail_id"],
            error=type(exc).__name__,
        )
        log.exception("customer reply failed")
    finally:
        heartbeat_stop.set()
        pulse.join()
        db.reply_clear_context()
        db.close_thread_connection()
    return True


def run(stop: threading.Event) -> None:
    if settings.reply_workflow_mode == "off":
        return

    def worker() -> None:
        owner = uuid.uuid4().hex
        while not stop.is_set():
            try:
                worked = process_once(owner)
            except Exception:
                log.exception("reply worker failed")
                worked = False
            stop.wait(0.2 if worked else 1)

    with ThreadPoolExecutor(
        max_workers=settings.reply_workers, thread_name_prefix="customer-reply"
    ) as pool:
        futures = [pool.submit(worker) for _ in range(settings.reply_workers)]
        stop.wait()
        for future in futures:
            future.result()
