"""One durable clarification per RFQ. A clarification is never a quote or acceptance."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app import db, hackloop, llm
from app.config import settings
from app.mail.content import current_message
from app.mail.language import detect
from app.mail.rfq_validation import (
    numeric_facts,
    requested_airport_corrections,
    validate,
)
from app.mail.router import address
from app.mail.style import SIGNATURE
from app.models import Mail, Rfq, RfqDraft
from app.pricing.reference import pickup_zones

log = logging.getLogger(__name__)

_RESCUE_FIELDS = (
    "origin",
    "pickup_zip",
    "destination",
    "weight_kg",
    "volume_cbm",
    "pieces",
    "dangerous_goods",
)
RESCUE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "clarification_rescue",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["corrections"],
            "properties": {
                "corrections": {
                    "type": "array",
                    "maxItems": 7,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["field", "value", "evidence"],
                        "properties": {
                            "field": {"type": "string", "enum": list(_RESCUE_FIELDS)},
                            "value": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                    },
                }
            },
        },
    },
}
RESCUE_PROMPT = (
    "You are the final recovery pass for an air-freight clarification. Read the complete "
    "customer thread. Resolve only fields listed as unresolved, using explicit customer facts "
    "from this shipment. Newer customer messages override the original request. Never use a "
    "signature as a pickup location, never guess, and never infer consent. Return only corrections "
    "that are unambiguous. Each evidence value must be an exact customer quote from the supplied "
    "thread. Airport values are IATA codes, weights are kg, volumes are cbm, and pickup_zip is five "
    "digits. Omit a field when the thread does not establish it."
)


def expired(sid):
    session = db.get_session(sid)
    return not session or datetime.fromisoformat(
        session["deadline_at"]
    ) <= datetime.now(UTC)


def text_for(draft, lang):
    if draft.evidence.get("question_en"):
        question = draft.evidence["question_de" if lang == "de" else "question_en"]
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
    items = [labels[f] for f in fields]
    joiner = " und " if lang == "de" else " and "
    wanted = items[0] if len(items) == 1 else ", ".join(items[:-1]) + joiner + items[-1]
    # one short human paragraph, no bullet list; the labels in brackets stay so the reply
    # can be matched to the field
    follow_up = int(draft.evidence.get("clarification_round", 0)) > 0
    example = ""
    if follow_up and len(fields) == 1:
        examples = {
            "origin": " Antworten Sie zum Beispiel mit „Origin: DUS“.",
            "destination": " Antworten Sie zum Beispiel mit „Destination: BOM“.",
            "pickup_zip": " Antworten Sie zum Beispiel mit „Pickup ZIP: 12345“.",
            "weight_kg": " Antworten Sie zum Beispiel mit „Gross weight: 1200 kg“.",
            "volume_cbm": " Antworten Sie zum Beispiel mit „Volume: 4,2 cbm“.",
        }
        examples_en = {
            "origin": ' For example, reply with "Origin: DUS".',
            "destination": ' For example, reply with "Destination: BOM".',
            "pickup_zip": ' For example, reply with "Pickup ZIP: 12345".',
            "weight_kg": ' For example, reply with "Gross weight: 1200 kg".',
            "volume_cbm": ' For example, reply with "Volume: 4.2 cbm".',
        }
        example = (examples if lang == "de" else examples_en).get(fields[0], "")
    if lang == "de":
        return (
            "Guten Tag,\n\ndanke für Ihre Anfrage. Damit wir Ihnen einen genauen Preis "
            f"nennen können, brauchen wir noch {wanted}. Schicken Sie uns das kurz hier im "
            f"Thread, dann melden wir uns gleich mit dem Angebot.{example}\n\n{SIGNATURE}\n"
        )
    return (
        "Hello,\n\nthanks for your request. To give you an exact price we just need "
        f"{wanted}. Could you send that over in this thread? We'll come back with the "
        f"quote right away.{example}\n\n{SIGNATURE}\n"
    )


def _normal(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _thread_transcript(mail: Mail, sid: int, root: Mail) -> str:
    rows = list(reversed(db.customer_mail_history(sid, mail.sender, mail.id)))
    rows.append({"id": mail.id, "subject": mail.subject, "body": mail.body})
    seen = set()
    pieces = []
    for row in rows:
        mid = int(row["id"])
        if mid in seen:
            continue
        seen.add(mid)
        current = current_message(
            str(row.get("body") or ""), str(row.get("subject") or "")
        )
        pieces.append(
            f"CUSTOMER MESSAGE {mid}\nSubject: {current.subject}\n{current.facts}".strip()
        )
    if root.id not in seen:
        current = current_message(root.body, root.subject)
        pieces.insert(
            0,
            f"CUSTOMER MESSAGE {root.id}\nSubject: {current.subject}\n{current.facts}".strip(),
        )
    return "\n\n".join(pieces)


def _rescue_value(field: str, raw: object) -> str | float | int | None:
    value = str(raw or "").strip()
    if field in ("origin", "destination"):
        code = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            return None
        if field == "origin" and code not in set(pickup_zones().values()):
            return None
        return code
    if field == "pickup_zip":
        match = re.fullmatch(r"(?:D-)?(\d{5})", value, re.IGNORECASE)
        return match[1] if match else None
    if field == "dangerous_goods":
        return value if value == "not_dangerous_document_corrected" else None
    try:
        number = Decimal(value.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    if number <= 0:
        return None
    if field == "pieces":
        return int(number) if number == number.to_integral_value() else None
    return float(number)


def rescue_with_model(
    mail: Mail, sid: int, root: Mail, draft: RfqDraft
) -> tuple[Rfq | None, RfqDraft]:
    """Use the stronger model once more, then accept only evidence-bound validated facts."""
    asked = {issue["field"] for issue in draft.issues}
    allowed = set(_RESCUE_FIELDS) & asked
    if "lots" in asked:
        allowed |= {"weight_kg", "volume_cbm"}
    if not allowed:
        return None, draft
    transcript = _thread_transcript(mail, sid, root)
    prompt = {
        "unresolved_fields": sorted(asked),
        "known_facts": draft.facts,
        "thread": transcript,
    }
    try:
        raw = llm.complete(
            "clarification_rescue",
            [
                {"role": "system", "content": RESCUE_PROMPT},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            sid,
            max_tokens=350,
            response_format=RESCUE_SCHEMA,
            model_name=settings.llm_rescue_model,
        )
        result = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - rescue failure must not stop the workflow
        log.warning("session %s: clarification rescue failed: %s", sid, exc)
        db.emit(
            "validation_rescue_failed",
            f"Stärkeres Modell konnte nicht auflösen: {type(exc).__name__}",
            sid,
            level="warn",
        )
        return None, draft
    corrections = dict(draft.evidence.get("corrections", {}))
    accepted = []
    normalized_thread = _normal(transcript)
    for item in result.get("corrections", []):
        field = item.get("field")
        evidence = str(item.get("evidence") or "").strip()
        if (
            field not in allowed
            or not evidence
            or _normal(evidence) not in normalized_thread
        ):
            continue
        value = _rescue_value(field, item.get("value"))
        if value is None:
            continue
        corrections[field] = value
        accepted.append({"field": field, "evidence": evidence[:160]})
    old_texts = draft.evidence.get("attachment_texts", {})
    remaining = [issue for issue in draft.issues if issue["field"] == "attachments"]
    rfq, checked = validate(root, draft, old_texts, remaining, corrections=corrections)
    checked.evidence.update(
        {
            "corrections": corrections,
            "attachment_texts": old_texts,
            "rescue_model": settings.llm_rescue_model,
            "rescue_evidence": accepted,
        }
    )
    for key in (
        "blocking_clarification",
        "clarification_round",
        "rescue_before_clarification",
    ):
        if key in draft.evidence:
            checked.evidence[key] = draft.evidence[key]
    if checked.issues or rfq is None:
        db.emit(
            "validation_rescue_unresolved",
            "Stärkeres Modell geprüft; Angaben weiterhin nicht eindeutig",
            sid,
            level="warn",
        )
        return None, checked
    db.emit(
        "validation_rescued",
        "Stärkeres Modell hat die Kundenangaben eindeutig aufgelöst",
        sid,
        model=settings.llm_rescue_model,
        fields=[item["field"] for item in accepted],
    )
    return rfq, checked


def recover_history(mail, sid, draft):
    """Only fill missing fields from this customer's same shipment thread, never conflicts."""
    history = db.customer_mail_history(sid, mail.sender, mail.id)
    db.emit(
        "history_checked", f"{len(history)} frühere Kundenmails im Thread geprüft", sid
    )
    missing = {
        i["field"]
        for i in draft.issues
        if i.get("code") in ("missing", "missing_or_unproven", "unproven")
    }
    observed = {field: set() for field in missing}
    for row in history:
        text = current_message(row["body"]).facts
        facts = {k: float(v) for k, v in numeric_facts(text).items()}
        facts.update(
            requested_airport_corrections(
                text,
                missing,
                origin=draft.facts.get("origin"),
                destination=draft.facts.get("destination"),
            )
        )
        if match := re.search(
            r"(?:pickup\s*(?:zip|postal code)|abhol.*?plz)\s*[:=]?\s*(\d{5})",
            text,
            re.IGNORECASE,
        ):
            facts["pickup_zip"] = match[1]
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
        if not draft.evidence.get("rescue_before_clarification"):
            draft.evidence["rescue_before_clarification"] = True
            previous = db.validation_get(sid)
            root = Mail(**previous["root"]) if previous else mail
            rescued, draft = rescue_with_model(mail, sid, root, draft)
            if rescued is not None:
                db.validation_save(
                    sid,
                    "ready",
                    draft.model_dump(mode="json"),
                    dataclasses.asdict(root),
                )
                db.set_request(sid, rescued.model_dump())
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
        "Rückfrage erforderlich: "
        + ", ".join(sorted({i["field"] for i in draft.issues})),
        sid,
        level="warn",
    )
    if draft.evidence.get("workflow", "rfq") == "rfq" and expired(sid):
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
    resuming = (
        resume and row["state"] == "resolving" and row["reply_mail_id"] == mail.id
    )
    if not resuming and not db.clarification_reply_claim(sid, mail.id):
        return True
    draft = RfqDraft.model_validate(row["draft"])
    if workflow == "rfq" and expired(sid):
        db.validation_save(sid, "expired", draft.model_dump(mode="json"), {})
        return True
    if db.is_paused(sid) or db.is_manual(sid):
        db.validation_save(sid, "manual_review", draft.model_dump(mode="json"), {})
        return True
    if workflow != "rfq":
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
    corrections.update(
        {k: float(v) for k, v in numeric_facts(own).items() if k in asked}
    )
    if "lots" in asked:
        from app.mail.lots import parse_lots

        totals = parse_lots(own)
        if totals:
            corrections.update(
                weight_kg=float(totals.weight_kg), volume_cbm=float(totals.volume_cbm)
            )
    if "pickup_zip" in asked and (m := re.search(r"\b(?:D-)?(\d{5})\b", own)):
        corrections["pickup_zip"] = m[1]
    corrections.update(
        requested_airport_corrections(
            own,
            asked,
            origin=draft.facts.get("origin"),
            destination=draft.facts.get("destination"),
        )
    )
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
    for key in (
        "blocking_clarification",
        "clarification_round",
        "rescue_before_clarification",
    ):
        if key in draft.evidence:
            checked.evidence[key] = draft.evidence[key]
    if checked.issues or rfq is None:
        rescued, checked = rescue_with_model(mail, sid, root, checked)
        if rescued is not None:
            db.validation_save(sid, "ready", checked.model_dump(mode="json"), {})
            db.set_request(sid, rescued.model_dump())
            from app.agents import rfq as agent

            agent.handle_rfq(root, sid)
            return True
        checked.evidence["clarification_round"] = (
            int(draft.evidence.get("clarification_round", 0)) + 1
        )
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
        elif row["draft"].get("evidence", {}).get(
            "workflow", "rfq"
        ) != "rfq" or not expired(sid):
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
                if (
                    row["state"] == "awaiting_clarification"
                    and row["send_state"] == "pending"
                ):
                    request(
                        Mail(**row["root"]),
                        item["session_id"],
                        RfqDraft.model_validate(row["draft"]),
                    )
        except Exception:
            logging.getLogger(__name__).exception("Clarification worker failed")
