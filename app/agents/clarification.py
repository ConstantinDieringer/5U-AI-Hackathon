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
from app.mail import multilingual
from app.mail.content import current_message
from app.mail.rfq_validation import (
    _SIGNATURE,
    explicit_delivery_origin,
    free_text_answer,
    numeric_facts,
    validate,
    weight_reading,
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
    return not session or datetime.fromisoformat(session["deadline_at"]) <= datetime.now(UTC)


def text_for(draft, lang):
    if draft.evidence.get("question_en"):
        question = draft.evidence["question_de" if lang == "de" else "question_en"]
        return f"{'Guten Tag' if lang == 'de' else 'Hello'},\n\n{question}\n\n{SIGNATURE}\n"
    fields = sorted({i["field"] for i in draft.issues})
    en = {
        "pickup_zip": "the postal code of the collection address",
        "origin": "the departure airport code",
        "destination": "the destination airport code",
        "weight_kg": "the total gross weight in kg",
        "volume_cbm": "the total volume in cbm",
        "pieces": "the total number of pieces",
        "lots": "complete gross weights and dimensions for each lot",
        "attachments": "a readable, corrected packing specification",
        "dangerous_goods": "the applicable dangerous goods declaration and safety data sheet, the UN number and class, and clarification of any conflicting declarations",
        "shipment": "the missing or corrected shipment details",
        "insurance": "the goods value and currency, insured coverage percentage and agreed insurance premium rate",
        "insurance_allocation": "the goods value, currency, coverage percentage and premium rate for each shipment separately",
    }
    de = {
        "pickup_zip": "die Postleitzahl der Abholadresse",
        "origin": "den Abflughafen",
        "destination": "den Zielflughafen",
        "weight_kg": "das gesamte Bruttogewicht in kg",
        "volume_cbm": "das gesamte Volumen in cbm",
        "pieces": "die gesamte Stückzahl",
        "lots": "vollständige Bruttogewichte und Maße für jede Position",
        "attachments": "eine lesbare, korrigierte Packliste",
        "dangerous_goods": "die zutreffende Gefahrguterklärung und das Sicherheitsdatenblatt, UN-Nummer und Klasse und eine Klärung widersprüchlicher Angaben",
        "shipment": "die fehlenden oder korrigierten Sendungsdaten",
        "insurance": "den Warenwert mit Währung, die Deckung in Prozent und den vereinbarten Prämiensatz der Transportversicherung",
        "insurance_allocation": "den Warenwert mit Währung, die Deckung und den Prämiensatz für jede Sendung einzeln",
    }
    labels = de if lang == "de" else en
    if "dangerous_goods" in fields:
        # one merged text (Constantin ba6dfa0 + round 10132): name the declared entry, ask
        # for DGD and SDS, no promise to quote before the documents are reviewed
        dg = draft.evidence.get("dangerous_goods") or {}
        what = dg.get("declared") or ", ".join(dg.get("un_numbers") or []) or None
        rest = [labels[f] for f in fields if f != "dangerous_goods"]
        if lang == "de":
            found = (
                f"Laut Ihren Unterlagen ist die Sendung Gefahrgut. Angegeben ist {what}. "
                if what
                else "Ihre Sendung enthält möglicherweise Gefahrgut, bitte nennen Sie uns UN-Nummer und Klasse. "
            )
            more = f" Außerdem brauchen wir noch {' und '.join(rest)}." if rest else ""
            return (
                f"Guten Tag,\n\nvielen Dank für Ihre Anfrage. {found}\n\nBitte schicken Sie uns die "
                "Versendererklärung für Gefahrgut, kurz DGD, und das Sicherheitsdatenblatt, "
                "kurz SDS.\n\nWir prüfen die Unterlagen und die Eignung des Transports, bevor wir ein Angebot "
                f"abgeben oder buchen.{more}\n\n{SIGNATURE}\n"
            )
        found = (
            f"Your documents show the shipment is dangerous goods. The declared entry is {what}. "
            if what
            else "Your shipment may contain dangerous goods, so please also confirm the UN number and class. "
        )
        more = f" We also need {' and '.join(rest)}." if rest else ""
        return (
            f"Hello,\n\nthank you for your request. {found}\n\nCould you send us the shipper's "
            "declaration for dangerous goods, known as DGD, and the safety data sheet, known as SDS?\n\nWe "
            "need to review them and the transport suitability before we can quote or book "
            f"this shipment.{more}\n\n{SIGNATURE}\n"
        )
    items = [labels[f] for f in fields]
    joiner = " und " if lang == "de" else " and "
    wanted = items[0] if len(items) == 1 else ", ".join(items[:-1]) + joiner + items[-1]
    # Ask in normal prose; reply extraction does not depend on exposing field labels.
    if lang == "de":
        return (
            "Guten Tag,\n\ndanke für Ihre Anfrage. Damit wir Ihnen einen genauen Preis "
            f"nennen können, brauchen wir noch {wanted}.\n\nSchicken Sie uns das gern hier im "
            f"Thread, dann melden wir uns gleich mit dem Angebot.\n\n{SIGNATURE}\n"
        )
    return (
        "Hello,\n\nthanks for your request. To give you an exact price we just need "
        f"{wanted}.\n\nCould you send that over in this thread? We'll come back with the "
        f"quote right away.\n\n{SIGNATURE}\n"
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
        current = current_message(str(row.get("body") or ""), str(row.get("subject") or ""))
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


def known_weights(draft: RfqDraft, sid: int, mail: Mail) -> tuple[Decimal, ...]:
    """Weights this shipment already states (THOUSANDS_EVIDENCE): the draft and its accepted
    corrections, every mail/attachment observation, and the current words of the customer's
    earlier mails in this thread. The mail being read is not its own evidence."""
    observations = (draft.evidence.get("weight_kg") or {}).get("observations") or []
    found: list[object] = [
        draft.facts.get("weight_kg"),
        draft.evidence.get("corrections", {}).get("weight_kg"),
        *(value for _, value in observations),
    ]
    for row in db.customer_mail_history(sid, mail.sender, mail.id):
        facts = numeric_facts(current_message(str(row.get("body") or "")).facts)
        found.append(facts.get("weight_kg"))
    return tuple(Decimal(str(v)) for v in found if v not in (None, ""))


def _rescue_value(
    field: str, raw: object, known: tuple[Decimal, ...] = (), facts: dict | None = None
) -> str | float | int | None:
    value = str(raw or "").strip()
    if field == "weight_kg" and settings.thousands_evidence:
        # the model copies "1,674" from the mail: 1674 when the shipment's figures say so
        number = value.replace(" ", "")
        if not re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?", number):
            return None
        reading = weight_reading(number, known=known, facts=facts)
        return float(reading) if reading is not None and reading > 0 else None
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
        return (
            value
            if value in ("not_dangerous_document_corrected", "dg_documents_received")
            else None
        )
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
    """Use the stronger model, then accept only evidence-bound deterministically valid facts."""
    if settings.thread_rescue_enabled:
        from app.agents import rescue

        return rescue.recover_request(mail, sid, root, draft)
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
    known = known_weights(draft, sid, mail) if settings.thousands_evidence else ()
    for item in result.get("corrections", []):
        field = item.get("field")
        evidence = str(item.get("evidence") or "").strip()
        if field not in allowed or not evidence or _normal(evidence) not in normalized_thread:
            continue
        value = _rescue_value(field, item.get("value"), known, draft.facts)
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
    for key in ("blocking_clarification", "rescue_before_clarification"):
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
                    sid, "ready", draft.model_dump(mode="json"), dataclasses.asdict(root)
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
        "Rückfrage erforderlich: " + ", ".join(sorted({i["field"] for i in draft.issues})),
        sid,
        level="warn",
    )
    if draft.evidence.get("workflow", "rfq") == "rfq" and expired(sid):
        db.validation_save(sid, "expired", draft.model_dump(mode="json"), {})
        return
    if db.is_paused(sid) or db.is_manual(sid):
        return
    workflow = draft.evidence.get("workflow", "rfq")
    asked = _questions_sent(sid, workflow)
    rescue_question = draft.evidence.get("rescue_question_mail_id") == mail.id
    if settings.clarify_once and asked and workflow == "rfq" and not rescue_question:
        # CLARIFY_ONCE: never a second question (round 28 asked four times). The answer did not
        # resolve it: quote from real prices if they allow, else stop for a person.
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
    # hard cap: at most two clarifications per workflow in a thread (one follow-up for a partial
    # answer). More is a loop (round 7237 sent four identical ones); alert a human instead.
    # Counted per workflow: an RFQ question must not use up the booking agent's questions.
    if asked >= 2 and not rescue_question:
        if workflow == "rfq":
            db.emit(
                "clarification_loop",
                "Weitere Rückfrage unterdrückt: Session braucht einen Menschen",
                sid,
                level="error",
            )
        else:
            # booking/tracking: the customer is still deciding, no round is lost; the
            # follow-up agent and the attention list cover it, no #sos alarm
            db.emit(
                "clarification_loop",
                f"Weitere Rückfrage ({workflow}) unterdrückt: bitte manuell prüfen",
                sid,
                level="warn",
            )
        # No send follows, for every workflow. Left at "pending", the 1 s worker called this
        # again every second (session 24377: 422 events in two minutes, until rescue).
        db.clarification_sent(sid, "capped")
        if settings.clarify_cap_resolve and workflow == "rfq":
            resolve_at_cap(mail, sid, draft)
        return
    if not db.clarification_claim(sid):
        return
    lang = multilingual.mail_language(mail, draft.facts.get("language") or "en")
    body = text_for(draft, lang)
    try:
        sent = hackloop.send_customer(
            mail.sender,
            mail.subject,
            body,
            in_reply_to=mail.id,
            session_id=sid,
            language_code=lang,
        )
        body = sent["body"]
    except hackloop.CustomerSendBlocked:
        db.clarification_sent(sid, "pending")
        return
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
        subject=sent["subject"],
        body=body,
        category="clarification",
        session_id=sid,
        in_reply_to=mail.id,
        references=[*mail.references, mail.id],
        raw=sent,
    )
    if workflow != "rfq":
        try:
            _count_other_workflow(sid, workflow)
        except Exception:  # bookkeeping after a completed send must not fail it
            log.exception("session %s: could not count the %s question", sid, workflow)
    if workflow == "rfq":
        try:  # which items our questions covered (CLARIFY_CAP_RESOLVE: was DG asked?)
            db.kv_set(
                f"clarify.asked.{sid}",
                json.dumps(sorted(asked_fields(sid) | {i["field"] for i in draft.issues})),
            )
        except Exception:  # bookkeeping after a completed send must not fail it
            log.exception("session %s: could not record the asked fields", sid)


def asked_fields(sid: int) -> set[str]:
    """Items any of our sent clarification questions in this thread asked about."""
    try:
        return set(json.loads(db.kv_get(f"clarify.asked.{sid}") or "[]"))
    except ValueError:
        return set()


_FIGURES = {"weight_kg": r"kgs?|kilos?|kilograms?", "volume_cbm": r"cbm|m3|m³"}
_OPEN_LABELS = {  # attention list wording for the open items
    "weight_kg": "Gewicht",
    "volume_cbm": "Volumen",
    "pieces": "Stückzahl",
    "lots": "Positionen",
    "pickup_zip": "Abhol-PLZ",
    "origin": "Abflughafen",
    "destination": "Zielflughafen",
    "dangerous_goods": "Gefahrgut-Unterlagen",
    "attachments": "Anhang",
    "shipment": "Sendungsdaten",
}


def thread_figure(field: str, draft: RfqDraft, sid: int, mail: Mail) -> float | None:
    """CLARIFY_CAP_RESOLVE: the one plausible value of `field` across this thread, else None.

    Candidates: every mail/attachment observation and every "N kg" ("N cbm") figure in the
    current words of each customer mail of the thread, the root included. A "1,674" figure
    adds both readings. Weights the shipment's pieces, pallets or volume rule out are dropped
    (Kessler: 1.674 kg, or the 11 a table header produced, for 11 pieces in 4.36 cbm). More
    candidates only make a unique value less likely, never a guess more likely."""
    from app.mail.rfq_validation import _NUM, implausible_weight

    values: set[Decimal] = set()
    for _, value in (draft.evidence.get(field) or {}).get("observations") or []:
        values.add(Decimal(str(value)))
    previous = db.validation_get(sid)
    texts = [current_message(Mail(**previous["root"]).body).facts] if previous else []
    texts += [
        current_message(str(row.get("body") or "")).facts
        for row in db.customer_mail_history(sid, mail.sender, mail.id)
    ]
    texts.append(current_message(mail.body).facts)
    for text in texts:
        body = _SIGNATURE.split(text, 1)[0]
        for raw in re.findall(rf"({_NUM})\s*(?:{_FIGURES[field]})\b", body, re.IGNORECASE):
            values.add(Decimal(raw.replace(",", ".")))
            if re.fullmatch(r"\d{1,3}[.,]\d{3}", raw):
                values.add(Decimal(re.sub(r"[.,]", "", raw)))
    values = {v for v in values if v > 0}
    if field == "weight_kg":
        context = "\n".join(texts)
        values = {v for v in values if not implausible_weight(v, context, draft.facts)}
    return float(values.pop()) if len(values) == 1 else None


def resolve_at_cap(mail: Mail, sid: int, draft: RfqDraft) -> None:
    """CLARIFY_CAP_RESOLVE: both questions are spent and something is still open. Never end
    silently:
    1. an open weight/volume with exactly one plausible value across the thread: use it;
       nothing else open means the normal flow prices and quotes;
    2. otherwise the best quote from what the mail states (quote_without_answer), unless a
       rule rejects it outright: DG never asked for, a pickup without a site, no usable
       weight/volume; customer carrier exclusions stay applied in pricing;
    3. otherwise nothing is sent: an error event and the top of the attention list name the
       open items, and the session waits in manual review."""
    from app.agents import rfq as agent

    previous = db.validation_get(sid)
    root = Mail(**previous["root"]) if previous else mail
    corrections = dict(draft.evidence.get("corrections", {}))
    found = {
        field: value
        for field in sorted({i["field"] for i in draft.issues} & _FIGURES.keys())
        if (value := thread_figure(field, draft, sid, mail)) is not None
    }
    if found:
        corrections.update(found)
        old_texts = draft.evidence.get("attachment_texts", {})
        remaining = [i for i in draft.issues if i["field"] == "attachments"]
        request, checked = validate(root, draft, old_texts, remaining, corrections=corrections)
        checked.evidence.update(corrections=corrections, attachment_texts=old_texts)
        for key in ("blocking_clarification", "rescue_before_clarification"):
            if key in draft.evidence:
                checked.evidence[key] = draft.evidence[key]
        db.emit(
            "clarification_cap_figures",
            "Rückfrage-Limit: eindeutiger Wert aus dem Thread übernommen: "
            + ", ".join(f"{k} {v:g}" for k, v in found.items()),
            sid,
            level="warn",
            found=found,
        )
        draft = checked
        if request is not None and not checked.issues:
            db.validation_save(sid, "ready", checked.model_dump(mode="json"), {})
            db.set_request(sid, request.model_dump())
            agent.handle_rfq(root, sid)
            return
        db.validation_save(sid, "awaiting_clarification", checked.model_dump(mode="json"), {})
    if agent.quote_without_answer(
        sid, "Rückfrage-Limit erreicht", dg_asked="dangerous_goods" in asked_fields(sid)
    ):
        return
    open_items = sorted({i["field"] for i in draft.issues})
    db.emit(
        "clarification_cap_open",
        "Rückfrage-Limit erreicht, kein Angebot gesendet. Offen: "
        + ", ".join(_OPEN_LABELS.get(f, f) for f in open_items),
        sid,
        level="error",
        open=open_items,
    )
    db.validation_save(sid, "manual_review", draft.model_dump(mode="json"), {})


def _other_workflow_counts(sid: int) -> dict[str, int]:
    """Questions sent per non-RFQ workflow (booking, tracking) in this thread."""
    try:
        raw = json.loads(db.kv_get(f"clarify.sent.{sid}") or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, int) and v > 0}


def _count_other_workflow(sid: int, workflow: str) -> None:
    counts = _other_workflow_counts(sid)
    counts[workflow] = counts.get(workflow, 0) + 1
    db.kv_set(f"clarify.sent.{sid}", json.dumps(counts, sort_keys=True))


def _questions_sent(sid: int, workflow: str = "rfq") -> int:
    """Clarification questions this workflow already sent in the thread.

    All workflows store their questions as category 'clarification'. Booking and tracking
    questions are counted separately, so the RFQ count is the rest. Without this split one
    RFQ question plus one booking question hit the cap of two and every further hesitant
    customer reply raised `clarification_loop`."""
    others = _other_workflow_counts(sid)
    if workflow != "rfq":
        return others.get(workflow, 0)
    total = (
        db.conn()
        .execute(
            "SELECT COUNT(*) FROM mails WHERE session_id = ? AND direction = 'out'"
            " AND category = 'clarification'",
            (sid,),
        )
        .fetchone()[0]
    )
    return max(0, total - sum(others.values()))


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


def answer_draft(mail, sid, root, draft):
    """Apply the shared deterministic customer-answer rules without dispatch or sending."""
    mail = multilingual.prepare(mail, sid, draft.facts.get("language") or "und")
    root = multilingual.prepare(root, sid, draft.facts.get("language") or "und")
    own = current_message(mail.body).facts
    asked = {i["field"] for i in draft.issues}
    corrections = dict(draft.evidence.get("corrections", {}))
    known = known_weights(draft, sid, mail) if settings.thousands_evidence else ()
    if "insurance" in asked:
        from app.models import CargoInsurance
        from app.pricing import insurance

        previous = (
            CargoInsurance.model_validate(draft.facts["insurance"])
            if draft.facts.get("insurance")
            else None
        )
        policy, problems = insurance.read([own], previous, requested=True)
        # Preserve partial answers too; the next question only follows new customer evidence.
        if not any(p.startswith("conflicting") for p in problems):
            corrections["insurance"] = policy.model_dump() if policy else None
    corrections.update(
        {k: float(v) for k, v in numeric_facts(own, known=known).items() if k in asked}
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
    for field, label in (
        ("origin", "origin|departure|abflug"),
        ("destination", "destination|ziel"),
    ):
        if field in asked and (
            m := re.search(rf"(?:{label})\s*[:=]?\s*([A-Z]{{3}})\b", own, re.IGNORECASE)
        ):
            corrections[field] = m[1].upper()
    conflicts = {
        i["field"]
        for i in draft.issues
        if i.get("code") == "conflict" and i["field"] in ("weight_kg", "volume_cbm", "pieces")
    } - corrections.keys()
    if settings.conflict_source_answer and conflicts:
        # round 35 (8859): "the attached packing specification is correct" names no number
        from app.mail.rfq_validation import confirmed_source

        corrections.update(confirmed_source(own, draft, conflicts))
    if settings.clarify_once:
        # free text: "CGN", "Cologne", a ZIP, "gross weight is 85 kg" (round 28's four answers)
        found = free_text_answer(
            own, asked, draft.facts.get("destination"), known=known, facts=draft.facts
        )
        corrections.update({k: v for k, v in found.items() if k not in corrections})
    if settings.thousands_evidence and "weight_kg" in asked and "weight_kg" not in corrections:
        # A direct answer to our weight question counts without CLARIFY_ONCE, as origin
        # answers do: "The total gross weight for our shipment to BOM is 1,674 kg." (Kessler)
        corrections.update(
            free_text_answer(
                own, {"weight_kg"}, draft.facts.get("destination"), known=known, facts=draft.facts
            )
        )
    if "origin" in asked and "origin" not in corrections:
        # Direct answers work independently of optional recovery/one-question modes.
        found = free_text_answer(own, {"origin"}, draft.facts.get("destination"))
        corrections.update(found)
    if "origin" in asked and "origin" not in corrections and settings.self_delivery_recovery:
        # "we'll drop it off at CGN ourselves", "quote it from CGN to DFW" (round 28)
        stated = explicit_delivery_origin(mail, draft.facts.get("destination"))
        if stated:
            corrections["origin"] = stated
    # the customer sends (or says they attached) the DG documents
    if "dangerous_goods" in asked and (
        mail.attachments
        or re.search(
            r"declaration|\bDGD\b|\bM?SDS\b|safety data sheet|dangerous goods (?:documents|docs)"
            r"|attached|anbei|im anhang|sicherheitsdatenblatt|gefahrgutdokument",
            own,
            re.IGNORECASE,
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

        texts, media, problems = read_sources(mail, session_id=sid)
        if not problems and not media:
            draft.evidence["attachment_texts"] = texts
            draft.issues = [i for i in draft.issues if i["field"] != "attachments"]
    old_texts = draft.evidence.get("attachment_texts", {})
    # Revalidate the original shipment with only explicit answers to the requested fields.
    remaining = [i for i in draft.issues if i["field"] == "attachments"]
    rfq, checked = validate(root, draft, old_texts, remaining, corrections=corrections)
    checked.evidence["corrections"] = {
        **draft.evidence.get("corrections", {}),
        **corrections,
    }
    checked.evidence["attachment_texts"] = old_texts
    for key in ("blocking_clarification", "rescue_before_clarification"):
        if key in draft.evidence:
            checked.evidence[key] = draft.evidence[key]
    return rfq, checked


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
    if workflow != "rfq":
        db.validation_save(sid, "ready", draft.model_dump(mode="json"), {})
        if workflow == "booking":
            from app.agents import booking

            booking.handle_reply(mail, sid)
        else:
            from app.agents import tracking

            tracking.handle(mail, sid)
        return True
    root = Mail(**row["root"])
    rfq, checked = answer_draft(mail, sid, root, draft)
    if checked.issues or rfq is None:
        rescued, checked = rescue_with_model(mail, sid, root, checked)
        if rescued is not None:
            db.validation_save(sid, "ready", checked.model_dump(mode="json"), {})
            db.set_request(sid, rescued.model_dump())
            from app.agents import rfq as agent

            agent.handle_rfq(root, sid)
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
    from app.agents import rescue

    for saved in db.clarification_unhandled_replies():
        if rescue.is_owner(saved["session_id"]):
            rescue.take_over(_stored_mail(saved), saved["session_id"])
        else:
            handle_reply(_stored_mail(saved), saved["session_id"])
    for pending in db.validation_pending():
        sid = pending["session_id"]
        row = db.validation_get(sid)
        if row["state"] == "resolving":
            saved = db.validation_reply(sid)
            if saved:
                if rescue.is_owner(sid):
                    rescue.take_over(_stored_mail(saved), sid)
                else:
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

    from app.agents import rescue

    while not stop.wait(1):
        try:
            tick(rescue)
        except Exception:
            logging.getLogger(__name__).exception("Clarification worker failed")


def tick(rescue) -> None:
    """One pass of the worker. A paused or taken-over session keeps its pending question
    untouched until it is released (calling request() for it re-emitted
    `validation_blocked` every second)."""
    for saved in db.clarification_unhandled_replies():
        if rescue.is_owner(saved["session_id"]):
            rescue.take_over(_stored_mail(saved), saved["session_id"])
        else:
            handle_reply(_stored_mail(saved), saved["session_id"])
    for item in db.validation_pending():
        sid = item["session_id"]
        if db.is_paused(sid) or db.is_manual(sid):
            continue
        row = db.validation_get(sid)
        if row["state"] == "awaiting_clarification" and row["send_state"] == "pending":
            request(Mail(**row["root"]), sid, RfqDraft.model_validate(row["draft"]))
    if settings.clarify_once:
        fallback_due()


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
        workflow = json.loads(row["draft_json"]).get("evidence", {}).get("workflow", "rfq")
        age = (now - datetime.fromisoformat(row["created_at"])).total_seconds()
        if (
            workflow != "rfq"
            or not settings.clarify_fallback_after_s <= age < rfq.TOO_OLD_S
            or db.kv_get(f"clarify.fallback.{sid}") is not None
            or db.is_paused(sid)
            or db.is_manual(sid)
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
