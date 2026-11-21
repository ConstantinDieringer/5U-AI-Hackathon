"""One durable clarification per RFQ. A clarification is never a quote or acceptance."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime

from app import db, hackloop
from app.config import settings
from app.mail.content import current_message
from app.mail.language import detect
from app.mail.rfq_validation import numeric_facts, validate
from app.models import Mail, RfqDraft


def expired(sid):
    session = db.get_session(sid)
    return not session or datetime.fromisoformat(session["deadline_at"]) <= datetime.now(UTC)


def text_for(draft, lang):
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
        "dangerous_goods": "whether the goods are dangerous, their UN number/class, and which conflicting declaration is incorrect",
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
        "dangerous_goods": "ob es Gefahrgut ist, UN-Nummer/Klasse und welche widersprüchliche Angabe falsch ist",
        "shipment": "die fehlenden oder korrigierten Sendungsdaten",
    }
    labels = de if lang == "de" else en
    points = "\n".join("• " + labels[f] for f in fields)
    return (
        (
            "Guten Tag,\n\nfür ein vollständiges Angebot bestätigen oder korrigieren Sie bitte:\n"
            if lang == "de"
            else "Hello,\n\nBefore we can quote, please confirm or correct:\n"
        )
        + points
        + (
            "\n\nBitte antworten Sie in diesem Thread.\n\nYour austrian Power Team\n"
            if lang == "de"
            else "\n\nPlease reply in this thread with the labelled values.\n\nYour austrian Power Team\n"
        )
    )


def request(mail, sid, draft):
    db.validation_save(
        sid, "awaiting_clarification", draft.model_dump(mode="json"), dataclasses.asdict(mail)
    )
    db.emit(
        "validation_blocked",
        "Rückfrage erforderlich: " + ", ".join(sorted({i["field"] for i in draft.issues})),
        sid,
        level="warn",
    )
    if expired(sid):
        db.validation_save(sid, "expired", draft.model_dump(mode="json"), {})
        return
    if db.is_paused(sid) or db.is_manual(sid) or not db.clarification_claim(sid):
        return
    body = text_for(draft, detect(current_message(mail.body).text))
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


def handle_reply(mail, sid, *, resume=False):
    row = db.validation_get(sid)
    if not row or row["state"] not in ("awaiting_clarification", "resolving", "manual_review"):
        return False
    if settings.rfq_validation_mode != "enforce":
        db.validation_save(sid, "manual_review", row["draft"], {})
        return True
    resuming = resume and row["state"] == "resolving" and row["reply_mail_id"] == mail.id
    if not resuming and not db.clarification_reply_claim(sid, mail.id):
        return True
    draft = RfqDraft.model_validate(row["draft"])
    if expired(sid):
        db.validation_save(sid, "expired", draft.model_dump(mode="json"), {})
        return True
    if db.is_paused(sid) or db.is_manual(sid):
        db.validation_save(sid, "manual_review", draft.model_dump(mode="json"), {})
        return True
    own = current_message(mail.body).facts
    asked = {i["field"] for i in draft.issues}
    corrections = {k: float(v) for k, v in numeric_facts(own).items() if k in asked}
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
    checked.evidence["corrections"] = {**draft.evidence.get("corrections", {}), **corrections}
    checked.evidence["attachment_texts"] = old_texts
    if checked.issues or rfq is None:
        db.validation_save(sid, "manual_review", checked.model_dump(mode="json"), {})
        db.emit(
            "validation_blocked",
            "Rückfrage nicht vollständig geklärt; manuelle Prüfung nötig",
            sid,
            level="warn",
        )
        return True
    db.validation_save(sid, "ready", checked.model_dump(mode="json"), {})
    db.set_request(sid, rfq.model_dump())
    from app.agents import rfq as agent

    agent.handle_rfq(root, sid)
    return True


def catch_up():
    for pending in db.validation_pending():
        sid = pending["session_id"]
        row = db.validation_get(sid)
        if row["state"] == "resolving":
            saved = db.validation_reply(sid)
            if saved:
                import json

                from app.mail.parse import mail_from_api

                answer = (
                    mail_from_api(json.loads(saved["raw_json"]))
                    if saved.get("raw_json")
                    else Mail(
                        saved["id"],
                        sid,
                        saved["sender"],
                        saved["recipient"],
                        saved["subject"],
                        saved["body"],
                        saved["received_at"],
                    )
                )
                handle_reply(answer, sid, resume=True)
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
        elif not expired(sid):
            request(Mail(**row["root"]), sid, RfqDraft.model_validate(row["draft"]))


def run(stop):
    """Resume a pending clarification after UI unpause; never touch an in-flight send."""
    import logging

    while not stop.wait(1):
        try:
            for item in db.validation_pending():
                row = db.validation_get(item["session_id"])
                if row["state"] == "awaiting_clarification" and row["send_state"] == "pending":
                    request(
                        Mail(**row["root"]),
                        item["session_id"],
                        RfqDraft.model_validate(row["draft"]),
                    )
        except Exception:
            logging.getLogger(__name__).exception("Clarification worker failed")
