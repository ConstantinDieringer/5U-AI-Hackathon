"""RFQ watchdog (RFQ_WATCHDOG): no RFQ thread may run out without our quote while a real
price exists. An unanswered round scores zero; a quote at 200 s still earns about 95 of 100.

Every few seconds, for each RFQ session RFQ_WATCHDOG_AFTER_S after arrival and still inside
TOO_OLD_S, without a quote and without a live worker in this process:

  - skip it when a person paused or took it over (session or global pause), or when its
    root mail was blocked as phishing (it never becomes a candidate);
  - take the stored request: the open clarification's draft (what the mail itself states),
    else the stored extraction, else one more extraction if the LLM budget allows;
  - open dangerous goods: send nothing, raise the alert (the rule rejects the quote);
  - price once more from sheets, portal (inside the rate gate and the time left) and the
    bids already received, and send through the same one-quote claim (rfq._price_and_quote);
  - door-to-port without a trucker price follows ALLOW_INCOMPLETE_DOOR_QUOTE as before;
    customer exclusions stay applied; no real price at all: send nothing, raise the alert.

Each session gets one watchdog attempt (kv claim). A restart empties the worker registry,
so the first check after a start handles every session already past the watchdog time.
The alert is an error-level `watchdog_alert` event (attention list and #sos).

    python -m app.agents.watchdog report    # one line per unanswered board round, offline
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from app import db
from app.agents import bids, rfq
from app.config import settings
from app.mail import parse
from app.mail.rfq_validation import validate
from app.models import Mail, Rfq, RfqDraft
from app.pricing.pricing import resolve_origin

log = logging.getLogger(__name__)

CHECK_EVERY_S = 5.0
BLOCKING_ISSUES = {"dangerous_goods", "insurance", "insurance_allocation"}
MIN_LEFT_S = 25.0  # less time than this to the 360 s expiry: too late to price and send


def due(now: datetime | None = None) -> list[dict]:
    """Sessions the watchdog must act on now, each with `action`: "force" or a skip reason."""
    now = now or datetime.now(UTC)
    oldest = (now - timedelta(seconds=rfq.TOO_OLD_S)).isoformat()
    newest = (now - timedelta(seconds=settings.rfq_watchdog_after_s)).isoformat()
    out = []
    for row in db.watchdog_candidates(oldest, newest):
        sid = int(row["session_id"])
        if db.case_owns(sid):
            action = "case_worker"
        elif row["paused"] or db.is_global_paused():
            action = "paused"
        elif row["mode"] == "manual":
            action = "manual"
        elif rfq.is_working(sid):
            action = "worker_alive"  # the live flow is still pricing or waiting for bids
        elif db.kv_get(f"watchdog.{sid}") is not None:
            action = "done"
        elif row["first_reply_at"] and not db.release_unsent_quote_claim(sid):
            action = "claimed"  # the quote may be out: never a second one
        else:
            action = "force"
        out.append({**row, "action": action})
    return out


def check_once(now: datetime | None = None, background: bool = True) -> list[int]:
    """One pass. Starts (or with background=False runs) a forced quote per due session."""
    started = []
    for row in due(now):
        sid = int(row["session_id"])
        if row["action"] == "claimed" and db.kv_claim(f"watchdog.{sid}"):
            # the send started but no quote was recorded: it may be out, a person checks
            _alert(sid, "Versand unklar (Quote-Claim ohne gespeicherte Mail), bitte prüfen")
            continue
        if row["action"] != "force" or not db.kv_claim(f"watchdog.{sid}"):
            continue
        started.append(sid)
        if background:
            threading.Thread(
                target=force_quote, args=(sid,), name=f"rfq-watchdog-{sid}", daemon=True
            ).start()
        else:
            force_quote(sid)
    return started


def run(stop: threading.Event) -> None:
    """Background loop (run.py, agents only). Never raises."""
    log.info("RFQ watchdog on: forces a quote %.0f s after arrival", settings.rfq_watchdog_after_s)
    while True:
        try:
            check_once()
        except Exception:
            log.exception("RFQ watchdog check failed")
        if stop.wait(CHECK_EVERY_S):
            return


# ---------------------------------------------------------------- forcing


def _alert(sid: int, why: str, **payload: object) -> None:
    log.error("session %s: watchdog sent no quote: %s", sid, why)
    db.emit(
        "watchdog_alert", f"Watchdog: kein Angebot gesendet, {why}", sid, level="error", **payload
    )


def _root(sid: int) -> Mail | None:
    row = db.root_mail(sid)
    if row is None or row.get("category") == "phishing":
        return None
    if row.get("raw_json"):
        return parse.mail_from_api(json.loads(row["raw_json"]))
    return Mail(
        int(row["id"]),
        int(row["id"]),
        str(row["sender"]),
        str(row["recipient"] or settings.team_mailbox),
        str(row["subject"] or ""),
        str(row["body"] or ""),
        str(row["received_at"]),
    )


def _fields(issues: list[dict]) -> set[str]:
    return {str(i.get("field")) for i in issues}


def _open_issues(sid: int) -> tuple[list[dict], RfqDraft | None]:
    """Issues still open in the RFQ input check, and the draft holding what the mail states."""
    row = db.validation_get(sid)
    if row is not None:
        if row["draft"].get("evidence", {}).get("workflow", "rfq") != "rfq":
            return [], None
        if row["state"] == "ready":
            return [], None
        draft = RfqDraft.model_validate(row["draft"])
        return list(draft.issues), draft
    # shadow mode keeps no workflow row: the last input check event says what was open
    events = [
        e
        for e in (db.get_session(sid) or {}).get("events", [])
        if e["kind"] == "validation_result" and e.get("payload")
    ]
    if not events:
        return [], None
    return list((events[-1]["payload"].get("draft") or {}).get("issues") or []), None


def stated_request(facts: dict, issues: list[dict]) -> tuple[Rfq | None, str]:
    """What the mail itself states. A route field the input check left open is not trusted
    (round 15: AMS came from a forwarded subject), so it is dropped and only re-found where
    the request states it; a pickup without a named site has no trucking price."""
    open_fields = _fields(issues)
    data = dict(facts)
    for field in ("origin", "destination"):
        if field in open_fields:
            data[field] = None
    if "pickup_zip" in open_fields and not (data.get("pickup_zip") or data.get("pickup_city")):
        return None, "Abholort offen"
    try:
        return Rfq.model_validate(data), ""
    except ValidationError:
        return None, "Sendungsdaten unvollständig"


def _extract(mail: Mail, sid: int) -> tuple[Rfq | None, list[dict], str]:
    """No stored request: one more extraction, only inside the session's LLM budget."""
    if db.llm_calls_used(sid) >= settings.llm_max_calls_per_session:
        return None, [], "LLM-Budget der Session aufgebraucht"
    extracted = rfq.extract_rfq(mail, sid)
    if extracted is None:
        return None, [], "Extraktion unbrauchbar"
    cached = json.loads(db.kv_get(f"rfq.sources.{sid}", "{}") or "{}")
    checked, draft = validate(
        mail, extracted, cached.get("texts", {}), list(cached.get("problems", []))
    )
    if checked is not None and not draft.issues:
        return checked, [], ""
    request, why = stated_request(draft.facts, draft.issues)
    return request, list(draft.issues), why


def force_quote(sid: int, reason: str = "watchdog") -> str:
    """The best quote real prices allow, once. Returns "sent" or why nothing was sent."""
    with rfq.working(sid):
        try:
            return _force(sid, reason)
        except Exception as e:
            log.exception("session %s: watchdog failed", sid)
            _alert(sid, f"Fehler {type(e).__name__}: {e}")
            return "error"
        finally:
            bids.close(sid)


def _force(sid: int, reason: str) -> str:
    if db.case_owns(sid):
        return "case_worker"
    stored = db.root_mail(sid)
    if stored is not None and stored.get("category") == "phishing":
        return "phishing"  # never a reply to a blocked mail
    mail = _root(sid)
    session = db.get_session(sid)
    if mail is None or session is None:
        _alert(sid, "Anfrage-Mail fehlt")
        return "no_mail"
    if db.is_paused(sid) or db.is_manual(sid):
        return "paused"
    if session["quotes"] or session["first_reply_at"]:
        return "answered"  # the one-quote claim is taken: never a second quote
    t0 = min(rfq._received(mail), datetime.fromisoformat(session["created_at"]).timestamp())
    issues, draft = _open_issues(sid)
    open_fields = _fields(issues)
    if blocked := open_fields & BLOCKING_ISSUES:
        _alert(sid, "Gefahrgut offen", open=sorted(open_fields))
        return "blocked:" + ",".join(sorted(blocked))
    request: Rfq | None
    why = ""
    if draft is not None:  # an open clarification: what the mail itself states
        request, why = stated_request(draft.facts, issues)
    else:
        request = rfq._stored_request(sid)
        if request is None:
            request, issues, why = _extract(mail, sid)
            open_fields = _fields(issues)
            if blocked := open_fields & BLOCKING_ISSUES:
                _alert(sid, "Gefahrgut offen", open=sorted(open_fields))
                return "blocked:" + ",".join(sorted(blocked))
    if request is None:
        _alert(sid, why or "keine verwertbaren Sendungsdaten", open=sorted(open_fields))
        return "no_request"
    request = rfq.with_stated_origin(request, mail, sid)
    origin = resolve_origin(request)
    if origin is None or origin == request.destination:
        _alert(sid, "Abflughafen nicht genannt", open=sorted(open_fields))
        return "no_origin"
    age = time.time() - t0
    if t0 + rfq.RFQ_LIFETIME_S - time.time() < MIN_LEFT_S:
        _alert(sid, f"zu spät ({age:.0f} s)")
        return "too_late"
    db.emit(
        "watchdog_forced",
        f"Watchdog nach {age:.0f} s: Angebot aus echten Preisen ({reason})",
        sid,
        level="warn",
        open=sorted(open_fields),
    )
    rfq._price_and_quote(mail, sid, request, origin, t0, settings.rfq_validation_mode, forced=True)
    after = db.get_session(sid) or {}
    if after.get("quotes"):
        row = db.validation_get(sid)
        if draft is not None and row is not None:
            # a late answer must not reopen pricing: the thread has its quote
            draft.evidence["quoted_by_watchdog"] = reason
            db.validation_save(sid, "ready", draft.model_dump(mode="json"), row["root"])
        db.emit(
            "watchdog_sent",
            f"Watchdog-Angebot gesendet nach {time.time() - t0:.0f} s",
            sid,
            level="warn",
        )
        return "sent"
    last = next((e for e in reversed(after.get("events", [])) if e.get("level") == "error"), None)
    _alert(sid, (last or {}).get("message") or "kein Preis", open=sorted(open_fields))
    return "no_quote"


# ---------------------------------------------------------------- report


def report_lines() -> list[str]:
    """One line per board round answered "no answer", with the cause from our own records."""
    lines = []
    for r in db.unanswered_rounds():
        cause = r["cause"] or {}
        restarts = cause.get("restarts_s") or []
        line = f"round {r['round_id']:>3}  sent {r['sent_at'] or '?'}  " + (
            f"session {r['session_id']}  " if r["session_id"] else ""
        )
        line += str(cause.get("summary") or "unbekannt")
        if cause.get("last_event_at"):
            line += f"  (last event {cause['last_event_at']} {cause.get('last_event_kind')})"
        if restarts:
            line += "  restart at " + ", ".join(f"+{s} s" for s in restarts)
        lines.append(line)
    return lines


def _main(argv: list[str]) -> int:
    if argv[:1] != ["report"]:
        sys.stderr.write("usage: python -m app.agents.watchdog report\n")
        return 2
    db.init_db()
    sys.stdout.write("\n".join(report_lines() or ["no unanswered rounds on the board"]) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
