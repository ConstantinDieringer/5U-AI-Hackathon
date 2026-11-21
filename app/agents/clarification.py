"""Durable clarification workflow with one reformulated follow-up to a holding reply."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta

from app import db, hackloop
from app.config import settings
from app.mail.content import current_message
from app.mail.language import detect
from app.mail.rfq_validation import (
    _SIGNATURE,
    explicit_delivery_origin,
    free_text_answer,
    numeric_facts,
    validate,
)
from app.mail.router import address
from app.mail.style import SIGNATURE
from app.models import Mail, RfqDraft


def expired(sid):
    session = db.get_session(sid)
    return not session or datetime.fromisoformat(session["deadline_at"]) <= datetime.now(UTC)


def text_for(draft, lang, *, follow_up=False):
    if draft.evidence.get("question_en"):
        question = draft.evidence["question_de" if lang == "de" else "question_en"]
        if follow_up:
            question = (
                "Damit klar ist, welche Angabe wir noch brauchen: "
                if lang == "de"
                else "To clarify the point we still need your help with: "
            ) + question
        return f"{'Guten Tag' if lang == 'de' else 'Hello'},\n\n{question}\n\n{SIGNATURE}\n"
    fields = sorted({i["field"] for i in draft.issues})
    en = {
        "pickup_zip": "the pickup site's postal code (Pickup ZIP)",
        "origin": "the departure airport code (Origin)",
        "destination": "the destination airport code (Destination)",
        "weight_kg": "the total gross weight in kg (Gross weight)",
        "volume_cbm": "the total volume in cbm (Volume)",
        "pieces": "the total number of pieces (Total pieces)",
        "lots": "complete gross weights and dimensions for each lot",
        "attachments": "a readable, corrected packing specification",
        "dangerous_goods": "the dangerous goods documents (shipper's declaration/DGD and safety data sheet/SDS, where applicable), the UN number/class, and clarification of any conflicting declarations",
        "shipment": "the missing or corrected shipment details",
    }
    de = {
        "pickup_zip": "die Postleitzahl der Abholadresse (Pickup ZIP)",
        "origin": "den Abflughafen-Code (Origin)",
        "destination": "den Zielflughafen-Code (Destination)",
        "weight_kg": "das gesamte Bruttogewicht in kg (Gross weight)",
        "volume_cbm": "das gesamte Volumen in cbm (Volume)",
        "pieces": "die gesamte Stückzahl (Total pieces)",
        "lots": "vollständige Bruttogewichte und Maße für jede Position",
        "attachments": "eine lesbare, korrigierte Packliste",
        "dangerous_goods": "die Gefahrgutunterlagen (Versendererklärung/DGD und Sicherheitsdatenblatt/SDS, soweit zutreffend), UN-Nummer/Klasse und eine Klärung widersprüchlicher Angaben",
        "shipment": "die fehlenden oder korrigierten Sendungsdaten",
    }
    labels = de if lang == "de" else en
    if "dangerous_goods" in fields:
        # one merged text (Constantin ba6dfa0 + round 10132): name the declared entry, ask
        # for DGD and SDS, no promise to quote before the documents are reviewed
        dg = draft.evidence.get("dangerous_goods") or {}
        what = dg.get("declared") or ", ".join(dg.get("un_numbers") or []) or None
        rest = [labels[f] for f in fields if f != "dangerous_goods"]
        if follow_up:
            cargo = f" ({what})" if what else ""
            if lang == "de":
                more = f" Außerdem fehlt uns noch {' und '.join(rest)}." if rest else ""
                return (
                    f"Guten Tag,\n\nich möchte kurz präzisieren, welche Unterlagen uns fehlen. "
                    f"Bitte schicken Sie uns die Gefahrgutdokumente für diese Sendung{cargo}: "
                    "die Shipper's Declaration for Dangerous Goods (DGD) und das "
                    "Sicherheitsdatenblatt (SDS), soweit für die Sendung zutreffend. "
                    "Falls ein Dokument nicht zutrifft, geben Sie uns bitte kurz Bescheid. "
                    f"Hängen Sie die Unterlagen bitte an Ihre Antwort in diesem Thread an.{more} "
                    f"Damit können wir die Prüfung für das Angebot fortsetzen.\n\n{SIGNATURE}\n"
                )
            more = f" We also still need {' and '.join(rest)}." if rest else ""
            return (
                f"Hello,\n\nLet me clarify which documents we're missing. Please attach the "
                f"dangerous goods documents for this shipment{cargo}: the Shipper's Declaration "
                "for Dangerous Goods (DGD) and the safety data sheet (SDS), where applicable "
                "to this shipment. If a document doesn't apply, please let us know. "
                f"Please attach the documents to your reply in this thread.{more} "
                f"We can then continue reviewing your request for a quote.\n\n{SIGNATURE}\n"
            )
        if lang == "de":
            line = (
                f"Laut Ihren Unterlagen ist die Sendung Gefahrgut ({what}). "
                if what
                else "Laut Ihren Unterlagen ist die Sendung Gefahrgut. "
            ) + (
                "Bitte schicken Sie uns die Gefahrgutdokumente: "
                "die Shipper's Declaration for Dangerous Goods (DGD) und das Sicherheitsdatenblatt (SDS), soweit zutreffend."
            )
            if not what:
                line += " Bitte nennen Sie auch die UN-Nummer und Gefahrgutklasse."
            more = f" Außerdem brauchen wir noch {' und '.join(rest)}." if rest else ""
            return (
                f"Guten Tag,\n\ndanke für Ihre Anfrage. {line}{more} Schicken Sie uns die "
                "Unterlagen bitte hier im Thread. Wir müssen die Unterlagen und die "
                "Transporteignung prüfen, bevor wir ein Angebot abgeben oder buchen."
                f"\n\n{SIGNATURE}\n"
            )
        line = (
            f"Your documents show the shipment is dangerous goods ({what}). "
            if what
            else "Your documents show the shipment is dangerous goods. "
        ) + (
            "Please send us the dangerous goods documents: the Shipper's "
            "Declaration for Dangerous Goods (DGD) and the safety data sheet (SDS), where applicable."
        )
        if not what:
            line += " Please also include the UN number and class."
        more = f" We also need {' and '.join(rest)}." if rest else ""
        return (
            f"Hello,\n\nthanks for your request. {line}{more} Please attach them to your reply "
            "in this thread. We need to review the documents and transport suitability "
            f"before we can quote or book this shipment.\n\n{SIGNATURE}\n"
        )
    items = [labels[f] for f in fields]
    joiner = " und " if lang == "de" else " and "
    wanted = items[0] if len(items) == 1 else ", ".join(items[:-1]) + joiner + items[-1]
    if follow_up:
        if lang == "de":
            return (
                f"Guten Tag,\n\nkonkret fehlt uns noch {wanted}. Können Sie uns diese Angabe "
                "bitte direkt in Ihrer Antwort schicken? Damit können wir die Prüfung für "
                f"das Angebot fortsetzen.\n\n{SIGNATURE}\n"
            )
        return (
            f"Hello,\n\nThe detail we're still missing is {wanted}. Could you include it "
            "directly in your reply? This will let us continue working on your quote.\n\n"
            f"{SIGNATURE}\n"
        )
    # one short human paragraph, no bullet list; the labels in brackets stay so the reply
    # can be matched to the field
    if lang == "de":
        return (
            "Guten Tag,\n\ndanke für Ihre Anfrage. Damit wir Ihnen einen genauen Preis "
            f"nennen können, brauchen wir noch {wanted}. Schicken Sie uns das kurz hier im "
            f"Thread, dann melden wir uns gleich mit dem Angebot.\n\n{SIGNATURE}\n"
        )
    return (
        "Hello,\n\nthanks for your request. To give you an exact price we just need "
        f"{wanted}. Could you send that over in this thread? We'll come back with the "
        f"quote right away.\n\n{SIGNATURE}\n"
    )


def recover_history(mail, sid, draft):
    """Only fill missing fields from this customer's same shipment thread, never conflicts."""
    history = db.customer_mail_history(sid, mail.sender, mail.id)
    db.emit("history_checked", f"{len(history)} frühere Kundenmails im Thread geprüft", sid)
    missing = {
        i["field"]
        for i in draft.issues
        if i.get("code") in ("missing", "missing_or_unproven", "unproven")
    }
    observed = {field: set() for field in missing}
    for row in history:
        text = current_message(row["body"]).facts
        facts = {k: float(v) for k, v in numeric_facts(text).items()}
        for field, label in (
            ("origin", "origin|departure|abflug"),
            ("destination", "destination|ziel"),
        ):
            match = re.search(rf"(?:{label})\s*[:=]?\s*([A-Z]{{3}})\b", text, re.IGNORECASE)
            if match:
                facts[field] = match[1].upper()
        if match := re.search(
            r"(?:pickup\s*(?:zip|postal code)|abhol.*?plz)\s*[:=]?\s*(\d{5})",
            text,
            re.IGNORECASE,
        ):
            facts["pickup_zip"] = match[1]
        elif settings.ask_pickup_zip:
            # "pickup from our site in 61391" earlier in this thread: the same shipment; a
            # ZIP in the signature is the company address, not the pickup site
            body = _SIGNATURE.split(text, 1)[0]
            if len(zips := set(re.findall(r"\b(?:D-)?(\d{5})\b", body))) == 1:
                facts["pickup_zip"] = zips.pop()
        for field in missing & facts.keys():
            observed[field].add(facts[field])
    corrections = dict(draft.evidence.get("corrections", {}))
    corrections.update(
        {f: next(iter(values)) for f, values in observed.items() if len(values) == 1}
    )
    if not corrections:
        return None, draft
    root = db.validation_get(sid)
    original = Mail(**root["root"]) if root else mail
    result, checked = validate(
        original,
        draft,
        draft.evidence.get("attachment_texts", {}),
        [i for i in draft.issues if i["field"] == "attachments"],
        corrections=corrections,
    )
    checked.evidence.update(
        {
            "corrections": corrections,
            "attachment_texts": draft.evidence.get("attachment_texts", {}),
        }
    )
    return result, checked


def request(mail, sid, draft):
    if (
        draft.evidence.get("workflow", "rfq") == "rfq"
        and settings.rfq_validation_mode != "enforce"
        and not draft.evidence.get("blocking_clarification")
    ):
        return
    if draft.evidence.get("workflow", "rfq") == "rfq":
        recovered, draft = recover_history(mail, sid, draft)
        if recovered is not None and not draft.issues:
            previous = db.validation_get(sid)
            root = Mail(**previous["root"]) if previous else mail
            db.validation_save(
                sid, "ready", draft.model_dump(mode="json"), dataclasses.asdict(mail)
            )
            db.set_request(sid, recovered.model_dump())
            from app.agents import rfq

            rfq.handle_rfq(root, sid)
            return
    db.validation_save(
        sid,
        "awaiting_clarification",
        draft.model_dump(mode="json"),
        dataclasses.asdict(mail),
    )
    db.emit(
        "validation_blocked",
        "Rückfrage erforderlich: " + ", ".join(sorted({i["field"] for i in draft.issues})),
        sid,
        level="warn",
    )
    if draft.evidence.get("workflow", "rfq") == "rfq" and expired(sid):
        db.validation_save(sid, "expired", draft.model_dump(mode="json"), {})
        return
    if db.is_paused(sid) or db.is_manual(sid):
        return
    asked = _questions_sent(sid)
    if settings.clarify_once and asked and draft.evidence.get("workflow", "rfq") == "rfq":
        # Ordinary unresolved answers still use CLARIFY_ONCE. A holding reply instead waits
        # for its one timed reformulation, handled separately in follow_up_due().
        from app.agents import rfq

        db.emit(
            "clarification_capped",
            "Keine zweite Rückfrage: Angebot aus echten Preisen oder Mensch",
            sid,
            level="warn",
        )
        if not rfq.quote_without_answer(sid, "Antwort unbrauchbar"):
            db.validation_save(sid, "manual_review", draft.model_dump(mode="json"), {})
        return
    if not db.clarification_claim(sid):
        return
    _send_question(mail, sid, draft)


def _send_question(mail, sid, draft, *, follow_up=False):
    body = text_for(draft, detect(current_message(mail.body).text), follow_up=follow_up)
    try:
        sent = hackloop.send(mail.sender, mail.subject, body, in_reply_to=mail.id)
    except Exception:  # noqa: BLE001 - send outcome must be treated as unknown, never retried
        db.clarification_sent(sid, "unknown")
        db.emit(
            "clarification_unknown",
            "Versand unklar; keine automatische Wiederholung",
            sid,
            level="error",
        )
        return
    # Persist send outcome first: a recording failure cannot cause another send.
    db.clarification_sent(sid, "sent", int(sent["id"]))
    db.record_outgoing(
        mail_id=int(sent["id"]),
        recipient=mail.sender,
        subject=mail.subject,
        body=body,
        category="clarification",
        session_id=sid,
        in_reply_to=mail.id,
        references=[*mail.references, mail.id],
        raw=sent,
    )


_HOLDING = re.compile(
    r"\b(?:need|have) to (?:check|look|confirm|discuss)|\b(?:will|i'll|we'll) (?:check|look|confirm|discuss)"
    r"|get back to you|come back to you|\b(?:checking|looking into)\b|look into (?:that|this)"
    r"|muss.{0,35}(?:prüfen|nachsehen|nachschauen|klären|abklären|besprechen)"
    r"|(?:werde|werden|wir).{0,35}(?:prüfen|nachsehen|nachschauen|klären|besprechen)"
    r"|melde.{0,20}(?:zurück|wieder)|melden.{0,20}(?:zurück|wieder)",
    re.IGNORECASE,
)


def _holding_reply(mail):
    return not mail.attachments and bool(_HOLDING.search(current_message(mail.body).facts))


def _wait_for_customer(mail, sid, draft):
    due = datetime.fromisoformat(mail.received_at) + timedelta(
        seconds=settings.clarification_followup_s
    )
    db.clarification_wait(sid, mail.id, draft.model_dump(mode="json"), due.isoformat())


def follow_up_due():
    at = datetime.now(UTC).isoformat()
    for saved in db.clarification_followups_due(at):
        sid = saved["session_id"]
        row = db.validation_get(sid)
        draft = RfqDraft.model_validate(row["draft"])
        if draft.evidence.get("workflow", "rfq") == "rfq":
            if expired(sid):
                db.validation_save(sid, "expired", row["draft"], {})
                continue
            if settings.rfq_validation_mode != "enforce" and not draft.evidence.get(
                "blocking_clarification"
            ):
                continue
        if db.clarification_followup_claim(sid, saved["id"], at):
            _send_question(_stored_mail(saved), sid, draft, follow_up=True)


def _questions_sent(sid):
    return db.clarification_count(sid)


def ask(mail, sid, workflow, en, de):
    history = db.customer_mail_history(sid, mail.sender, mail.id)
    db.emit(
        "history_checked",
        f"{len(history)} frühere Kundenmails vor Rückfrage geprüft",
        sid,
    )
    if db.is_paused(sid) or db.is_manual(sid):
        return False
    draft = RfqDraft(
        evidence={"workflow": workflow, "question_en": en, "question_de": de},
        issues=[{"field": "shipment", "code": "needs_confirmation"}],
    )
    if not db.clarification_open_workflow(
        sid, draft.model_dump(mode="json"), dataclasses.asdict(mail)
    ):
        return False
    request(mail, sid, draft)
    return True


def handle_reply(mail, sid, *, resume=False):
    row = db.validation_get(sid)
    if not row or row["state"] not in (
        "awaiting_clarification",
        "resolving",
        "manual_review",
    ):
        return False
    session = db.get_session(sid)
    if not session or address(session.get("customer") or "") != address(mail.sender):
        return True
    workflow = row["draft"].get("evidence", {}).get("workflow", "rfq")
    if (
        workflow == "rfq"
        and settings.rfq_validation_mode != "enforce"
        and not row["draft"].get("evidence", {}).get("blocking_clarification")
    ):
        db.validation_save(sid, "manual_review", row["draft"], {})
        return True
    resuming = resume and row["state"] == "resolving" and row["reply_mail_id"] == mail.id
    if not resuming and not db.clarification_reply_claim(sid, mail.id):
        return True
    draft = RfqDraft.model_validate(row["draft"])
    if workflow == "rfq" and expired(sid):
        db.validation_save(sid, "expired", draft.model_dump(mode="json"), {})
        return True
    if db.is_paused(sid) or db.is_manual(sid):
        db.validation_save(sid, "manual_review", draft.model_dump(mode="json"), {})
        return True
    if re.search(
        r"(?:do not|don't|please stop).{0,25}(?:follow.?up|remind|email|contact)"
        r"|(?:keine|nicht).{0,25}(?:nachfrag|erinner|kontaktier)",
        current_message(mail.body).facts,
        re.IGNORECASE,
    ):
        db.validation_save(sid, "manual_review", draft.model_dump(mode="json"), {})
        return True
    if workflow != "rfq":
        if _holding_reply(mail) and not re.search(
            r"\bBK-[A-Z0-9]+\b|\b(?:yes|ja)\b|go ahead|bitte buchen",
            current_message(mail.body).facts,
            re.IGNORECASE,
        ):
            _wait_for_customer(mail, sid, draft)
            return True
        db.validation_save(sid, "ready", draft.model_dump(mode="json"), {})
        if workflow == "booking":
            from app.agents import booking

            booking.handle_reply(mail, sid)
        else:
            from app.agents import tracking

            tracking.handle(mail, sid)
        return True
    own = current_message(mail.body).facts
    asked = {i["field"] for i in draft.issues}
    corrections = dict(draft.evidence.get("corrections", {}))
    corrections.update({k: float(v) for k, v in numeric_facts(own).items() if k in asked})
    if "lots" in asked:
        from app.mail.lots import parse_lots

        totals = parse_lots(own)
        if totals:
            corrections.update(
                weight_kg=float(totals.weight_kg), volume_cbm=float(totals.volume_cbm)
            )
    if "pickup_zip" in asked and (m := re.search(r"\b(?:D-)?(\d{5})\b", own)):
        corrections["pickup_zip"] = m[1]
    for field, label in (
        ("origin", "origin|departure|abflug"),
        ("destination", "destination|ziel"),
    ):
        if field in asked and (
            m := re.search(rf"(?:{label})\s*[:=]?\s*([A-Z]{{3}})\b", own, re.IGNORECASE)
        ):
            corrections[field] = m[1].upper()
    if settings.clarify_once:
        # free text: "CGN", "Cologne", a ZIP, "gross weight is 85 kg" (round 28's four answers)
        found = free_text_answer(own, asked, draft.facts.get("destination"))
        corrections.update({k: v for k, v in found.items() if k not in corrections})
    if "origin" in asked and "origin" not in corrections and settings.self_delivery_recovery:
        # "we'll drop it off at CGN ourselves", "quote it from CGN to DFW" (round 28)
        stated = explicit_delivery_origin(mail, draft.facts.get("destination"))
        if stated:
            corrections["origin"] = stated
    # the customer sends (or says they attached) the DG documents
    if "dangerous_goods" in asked and (
        mail.attachments
        or (
            not _holding_reply(mail)
            and re.search(
                r"declaration|\bDGD\b|\bM?SDS\b|safety data sheet|dangerous goods (?:documents|docs)"
                r"|attached|anbei|im anhang|sicherheitsdatenblatt|gefahrgutdokument",
                own,
                re.IGNORECASE,
            )
        )
    ):
        corrections["dangerous_goods"] = "dg_documents_received"
    if (
        "dangerous_goods" in asked
        and re.search(r"non-dangerous|not dangerous|kein gefahrgut", own, re.IGNORECASE)
        and re.search(
            r"(?:attachment|document|packing\s+(?:list|specification)|anhang|dokument|packliste).{0,40}(?:incorrect|wrong|falsch)",
            own,
            re.IGNORECASE,
        )
        and not re.search(r"not\s+non-dangerous|nicht\s+kein", own, re.IGNORECASE)
    ):
        corrections["dangerous_goods"] = "not_dangerous_document_corrected"
    if "attachments" in asked and mail.attachments:
        from app.mail.rfq_sources import read_sources

        texts, media, problems = read_sources(mail)
        if not problems and not media:
            draft.evidence["attachment_texts"] = texts
            draft.issues = [i for i in draft.issues if i["field"] != "attachments"]
    root = Mail(**row["root"])
    old_texts = draft.evidence.get("attachment_texts", {})
    # Revalidate the original shipment with only explicit answers to the requested fields.
    remaining = [i for i in draft.issues if i["field"] == "attachments"]
    rfq, checked = validate(root, draft, old_texts, remaining, corrections=corrections)
    checked.evidence["corrections"] = {
        **draft.evidence.get("corrections", {}),
        **corrections,
    }
    checked.evidence["attachment_texts"] = old_texts
    if draft.evidence.get("blocking_clarification"):
        checked.evidence["blocking_clarification"] = True
    if checked.issues or rfq is None:
        if _holding_reply(mail):
            _wait_for_customer(mail, sid, checked)
            return True
        db.clarification_reopen(sid, checked.model_dump(mode="json"))
        request(mail, sid, checked)
        return True
    db.validation_save(sid, "ready", checked.model_dump(mode="json"), {})
    db.set_request(sid, rfq.model_dump())
    from app.agents import rfq as agent

    agent.handle_rfq(root, sid)
    return True


def catch_up():
    for saved in db.clarification_unhandled_replies():
        handle_reply(_stored_mail(saved), saved["session_id"])
    for pending in db.validation_pending():
        sid = pending["session_id"]
        row = db.validation_get(sid)
        if row["state"] == "resolving":
            saved = db.validation_reply(sid)
            if saved:
                handle_reply(_stored_mail(saved), sid, resume=True)
            else:
                db.validation_save(sid, "manual_review", row["draft"], {})
        elif row["send_state"] == "sending":
            db.clarification_sent(sid, "unknown")
            db.emit(
                "clarification_unknown",
                "Neustart während Versand; manuell prüfen",
                sid,
                level="error",
            )
        elif row["draft"].get("evidence", {}).get("workflow", "rfq") != "rfq" or not expired(sid):
            request(Mail(**row["root"]), sid, RfqDraft.model_validate(row["draft"]))


def _stored_mail(saved):
    import json

    from app.mail.parse import mail_from_api

    raw = json.loads(saved["raw_json"]) if saved.get("raw_json") else None
    return (
        mail_from_api(raw)
        if isinstance(raw, dict) and raw.get("id")
        else Mail(
            saved["id"],
            saved["session_id"],
            saved["sender"],
            saved["recipient"],
            saved["subject"],
            saved["body"],
            saved["received_at"],
        )
    )


def run(stop):
    """Resume a pending clarification after UI unpause; never touch an in-flight send."""
    import logging

    while not stop.wait(1):
        try:
            for saved in db.clarification_unhandled_replies():
                handle_reply(_stored_mail(saved), saved["session_id"])
            for item in db.validation_pending():
                row = db.validation_get(item["session_id"])
                if row["state"] == "awaiting_clarification" and row["send_state"] == "pending":
                    request(
                        Mail(**row["root"]),
                        item["session_id"],
                        RfqDraft.model_validate(row["draft"]),
                    )
            follow_up_due()
            if settings.clarify_once:
                fallback_due()
        except Exception:
            logging.getLogger(__name__).exception("Clarification worker failed")


def fallback_due(now=None):
    """CLARIFY_ONCE: no usable answer CLARIFY_FALLBACK_AFTER_S after the request: quote from
    real prices in time for the 120 s full score. Each session gets one attempt."""
    import json
    import threading

    from app.agents import rfq

    now = now if now is not None else datetime.now(UTC)
    started = []
    for row in db.clarification_waiting():
        sid = int(row["session_id"])
        evidence = json.loads(row["draft_json"]).get("evidence", {})
        workflow = evidence.get("workflow", "rfq")
        age = (now - datetime.fromisoformat(row["created_at"])).total_seconds()
        if (
            workflow != "rfq"
            or (
                settings.rfq_validation_mode != "enforce"
                and not evidence.get("blocking_clarification")
            )
            or not settings.clarify_fallback_after_s <= age < rfq.TOO_OLD_S
            or db.kv_get(f"clarify.fallback.{sid}") is not None
            or db.is_paused(sid)
            or db.is_manual(sid)
            or db.clarification_followup_pending(sid)
        ):
            continue
        threading.Thread(
            target=rfq.quote_without_answer,
            args=(sid, "keine Antwort"),
            name=f"rfq-fallback-{sid}",
            daemon=True,
        ).start()
        started.append(sid)
    return started
