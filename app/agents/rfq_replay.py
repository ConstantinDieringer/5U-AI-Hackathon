"""Offline replay of real RFQ sessions through the live RFQ flow: python -m app.agents.rfq_replay.

Every stored round runs through `rfq.handle_rfq` (and `clarification.handle_reply` for stored
customer answers) on a throwaway SQLite file. Nothing leaves the machine:

- the LLM returns the extraction stored for that session; the quote composer is refused, so the
  deterministic quote text is used (mail text diffs show template changes only);
- the portal returns the options stored at quote time, only for the same lane, weight and
  volume (otherwise none: a changed request needs fresh rates and shows up as a diff);
- carrier replies stored before our quote are delivered for the carriers the code asks now;
- sends are captured; every other 5U call raises and is counted as `network_blocked`.

Clock: the round and its stored rate/FX expiries move forward by whole days to just after now,
so clock times keep their original value; tariff validity and quoted dates use the original day. Rate sheets and portal lanes come from
the tracked seed files, not from RUNTIME_DIR.

    python -m app.agents.rfq_replay corpus <dir of /api/sessions/{id} JSON> [--board board_me.json]
    python -m app.agents.rfq_replay run [--out results.json] [--compare baseline.json]
    python -m app.agents.rfq_replay diff <before.json> <after.json>
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import math
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from app import db, hackloop, llm
from app.agents import bids, clarification, rfq
from app.models import Mail, RateOption, Rfq
from app.pricing import portal_sync, pricing, served_lanes
from app.pricing.portal_sync import PortalResult

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "replay"
CORPUS = FIXTURES / "rfq_sessions.json"
RECONSTRUCTED = FIXTURES / "reconstructed_extractions.json"
BASELINE = FIXTURES / "baseline.json"
_RATE_FIELDS = {f.name for f in dataclasses.fields(RateOption)}
_EXPIRY_FIELDS = ("valid_until", "fx_valid_until", "observed_at")


# ---------------------------------------------------------------- corpus


def _extraction(detail: dict[str, Any]) -> dict[str, Any] | None:
    """The first `extracted` event is the parsed LLM answer (before code post-processing)."""
    for event in detail["events"]:
        if event["kind"] == "extracted" and event.get("payload"):
            return dict(event["payload"])
    return None


def _portal(detail: dict[str, Any]) -> dict[str, Any] | None:
    """Portal options and errors as the RFQ flow saw them (a 503 becomes an email bid)."""
    errors = [
        str(err)
        for e in detail["events"]
        if e["kind"] == "portal_warning" and e.get("payload")
        for err in e["payload"].get("errors", [])
    ]
    audits = [
        e for e in detail["events"] if e["kind"] == "pricing_audit" and e.get("payload")
    ]
    if audits:
        payload = audits[-1]["payload"]
        request, alternatives = payload.get("request"), payload.get("options", [])
    elif detail["quotes"]:
        request, alternatives = (
            detail.get("request"),
            detail["quotes"][0].get("alternatives"),
        )
    else:
        return {"request": detail.get("request"), "options": [], "errors": errors}
    options = [o for o in alternatives or [] if o.get("source") == "portal"]
    return {"request": request, "options": options, "errors": errors}


def corpus_case(
    detail: dict[str, Any],
    board: dict[int, dict[str, Any]],
    reconstructed: dict[str, Any],
) -> dict[str, Any]:
    """One replay case from a stored session detail (GET /api/sessions/{id})."""
    sid = int(detail["id"])
    quote = detail["quotes"][0] if detail["quotes"] else None
    quote_mail = next(
        (m for m in detail["mails"] if quote and m["id"] == quote.get("mail_id")), None
    )
    cutoff = quote["sent_at"] if quote else detail.get("deadline_at")
    keep = []
    for m in detail["mails"]:
        inbound = m["direction"] == "in"
        before_quote = cutoff is None or m["received_at"] <= cutoff
        if inbound and (m["id"] == sid or before_quote):
            keep.append(
                {
                    k: m.get(k)
                    for k in (
                        "id",
                        "sender",
                        "recipient",
                        "subject",
                        "body",
                        "category",
                        "received_at",
                        "in_reply_to",
                    )
                }
            )
    round_no = next((int(b["round_id"]) for b in detail.get("board") or []), None)
    points = board.get(round_no or -1, {})
    extraction = _extraction(detail)
    source = "stored"
    if extraction is None and str(sid) in reconstructed:
        extraction = reconstructed[str(sid)]["extraction"]
        source = "reconstructed: " + reconstructed[str(sid)]["note"]
    return {
        "session_id": sid,
        "round_no": round_no,
        "board": {
            "verdict": points.get("verdict"),
            "reason": points.get("reason"),
            "accuracy": (points.get("points") or {}).get("accuracy"),
        },
        "llm_extraction": extraction,
        "llm_extraction_source": source if extraction is not None else None,
        "portal": _portal(detail),
        "mails": keep,
        "production_quote": (
            {
                "airline": quote["airline"],
                "source": quote["source"],
                "total_minor": quote["total_minor"],
                "transit_days": quote["transit_days"],
                "sent_at": quote["sent_at"],
                "body": quote_mail["body"] if quote_mail else None,
            }
            if quote
            else None
        ),
    }


def build_corpus(session_dir: Path, board_path: Path | None) -> dict[str, Any]:
    board: dict[int, dict[str, Any]] = {}
    if board_path:
        for r in json.loads(board_path.read_text(encoding="utf-8"))["requests"]:
            board[int(r["round_no"])] = r
    reconstructed = (
        json.loads(RECONSTRUCTED.read_text(encoding="utf-8"))
        if RECONSTRUCTED.exists()
        else {}
    )
    cases, observations = [], []
    for path in sorted(session_dir.glob("*.json")):
        detail = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(detail, dict) or not detail.get("mails"):
            continue
        for m in detail["mails"]:
            if m["direction"] == "in" and served_lanes.parse_refusal(
                m.get("body") or ""
            ):
                observations.append(
                    {k: m.get(k) for k in ("id", "sender", "received_at", "body")}
                )
        if detail.get("kind") == "rfq":
            cases.append(corpus_case(detail, board, reconstructed))
    cases.sort(
        key=lambda c: (c["round_no"] is None, c["round_no"] or 0, c["session_id"])
    )
    observations.sort(key=lambda o: (o["received_at"], o["id"]))
    # airlines' "we do not serve this lane" replies: what production knew before each round
    return {"cases": cases, "lane_observations": observations}


# ---------------------------------------------------------------- sandbox


def _shift(stamp: str | None, delta: timedelta) -> str | None:
    if not stamp:
        return stamp
    try:
        return (datetime.fromisoformat(stamp) + delta).isoformat()
    except ValueError:
        return stamp


def _stored_origin(stored: dict[str, Any]) -> str | None:
    try:
        return rfq.resolve_origin(Rfq.model_validate(stored))
    except ValueError:
        return stored.get("origin")


def _same_request(stored: dict[str, Any] | None, now: Rfq) -> bool:
    """Stored portal prices hold only for the same airport pair, weight and volume."""
    if not stored:
        return False
    return (
        _stored_origin(stored) == rfq.resolve_origin(now)
        and stored.get("destination") == now.destination
        and abs(float(stored.get("weight_kg") or 0) - now.weight_kg) < 0.01
        and abs(float(stored.get("volume_cbm") or 0) - now.volume_cbm) < 0.0001
    )


def _rate_option(raw: dict[str, Any], now: Rfq, delta: timedelta) -> RateOption:
    """A stored option; rounds before the pricing audit stored only airline, total, transit."""
    data = {k: v for k, v in raw.items() if k in _RATE_FIELDS}
    data.setdefault("origin", rfq.resolve_origin(now) or "")
    data.setdefault("destination", now.destination)
    data.setdefault("currency", "EUR")
    data.setdefault("total_minor", data["total_eur_minor"])
    weight = float(pricing.chargeable_kg(now.weight_kg, now.volume_cbm))
    data.setdefault("chargeable_kg", weight)
    data.setdefault("billed_kg", data["chargeable_kg"])
    for key in _EXPIRY_FIELDS:
        if key in data:
            data[key] = _shift(data[key], delta)
    return RateOption(**data)


@dataclasses.dataclass
class _Run:
    case: dict[str, Any]
    delta: timedelta
    day: date
    sent: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    notes: set[str] = dataclasses.field(default_factory=set)
    network_blocked: int = 0
    next_id: int = 900_000_000


@contextlib.contextmanager
def _sandbox(run: _Run) -> Iterator[None]:
    stored_portal = run.case.get("portal") or {}
    extraction = run.case.get("llm_extraction")
    replies = [m for m in run.case["mails"] if m.get("category") == "carrier_bid"]

    def blocked(*_a: Any, **_k: Any) -> Any:
        run.network_blocked += 1
        raise RuntimeError("replay: network disabled")

    def send(
        to: str, subject: str, body: str, in_reply_to: int | None = None, **_k: Any
    ) -> dict[str, Any]:
        run.next_id += 1
        sent = {
            "id": run.next_id,
            "from": "austrian-power@hackloop.mail",
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
            "references": [],
            "received_at": datetime.now(UTC).isoformat(),
        }
        run.sent.append(sent)
        return sent

    def complete(purpose: str, *_a: Any, **_k: Any) -> str:
        if purpose.startswith("extract") and extraction is not None:
            return json.dumps(extraction)
        # no stored answer (composer, repairs): ValueError takes the deterministic fallback
        raise ValueError(f"replay: no stored LLM answer for {purpose}")

    def portal(request: Rfq, **_k: Any) -> PortalResult:
        errors = list(stored_portal.get("errors") or [])
        if not _same_request(stored_portal.get("request"), request):
            if stored_portal.get("options") or errors:
                run.notes.add("portal_request_changed")
            return PortalResult()
        options = [
            _rate_option(o, request, run.delta)
            for o in stored_portal.get("options", [])
        ]
        fallbacks = [e.split(":", 1)[0] for e in errors if "-> 503" in e]
        return PortalResult(options, fallbacks, errors)

    original_request = bids.request

    def request(sid: int, rfq_: Rfq, origin: str, carriers: list[bids.Carrier]) -> int:
        n = original_request(sid, rfq_, origin, carriers)
        asked = {c.email for c in carriers}
        answered = {m["sender"] for m in replies}
        if asked - answered:
            run.notes.add("carrier_without_stored_reply")
        for m in replies:
            if m["sender"] in asked:
                bids.on_bid_mail(_mail(m, run.delta), None)
        return n

    original_sheet = pricing.sheet_options
    original_schedule = rfq.schedule

    def sheet(
        request: Rfq, use_breakpoints: bool | None = None, on: date | None = None
    ) -> list[RateOption]:
        return original_sheet(request, use_breakpoints, on or run.day)

    def schedule(request: Rfq, transit_days: int, today: date | None = None) -> Any:
        return original_schedule(request, transit_days, today or run.day)

    seed = json.loads(portal_sync.SEED_PORTAL_LANES.read_text(encoding="utf-8"))
    patches: list[tuple[object, str, Callable[..., Any] | Path]] = [
        (hackloop, "_request", blocked),
        (hackloop, "send", send),
        (llm, "complete", complete),
        (rfq, "portal_result", portal),
        (rfq, "sheet_options", sheet),
        (rfq, "schedule", schedule),
        (bids, "request", request),
        (bids, "wait", lambda *_a, **_k: []),
        (pricing, "RUNTIME_TARIFFS", Path("/nonexistent/replay/tariffs.json")),
        (portal_sync, "load_portal_lanes", lambda *_a, **_k: seed),
    ]
    with contextlib.ExitStack() as stack:
        for target, name, value in patches:
            stack.enter_context(mock.patch.object(target, name, value))
        pricing.reload_lanes()
        try:
            yield
        finally:
            pricing.reload_lanes()


def _mail(
    m: dict[str, Any], delta: timedelta, references: list[int] | None = None
) -> Mail:
    return Mail(
        int(m["id"]),
        int(references[0]) if references else int(m["id"]),
        m["sender"],
        m["recipient"],
        m["subject"],
        m["body"],
        _shift(m["received_at"], delta) or "",
        references=list(references or []),
    )


# ---------------------------------------------------------------- replay


def replay_case(
    case: dict[str, Any],
    workdir: Path,
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sid = int(case["session_id"])
    root = next(m for m in case["mails"] if int(m["id"]) == sid)
    received = datetime.fromisoformat(root["received_at"])
    # whole days, so clock times in the quote ("bookable until 18:04 UTC") stay as they were
    days = math.ceil((datetime.now(UTC) - received) / timedelta(days=1))
    run = _Run(case, timedelta(days=days), received.date())
    db.init_db(str(workdir / f"replay-{sid}.db"))
    for seen in observations or []:
        refusal = served_lanes.parse_refusal(seen["body"] or "")
        if refusal is not None and seen["received_at"] < root["received_at"]:
            served_lanes.remember(
                seen["sender"], refusal, at=_shift(seen["received_at"], run.delta)
            )
    with _sandbox(run):
        mail = _mail(root, run.delta)
        db.ingest_mail(
            mail_id=sid,
            sender=mail.sender,
            recipient=mail.to,
            subject=mail.subject,
            body=mail.body,
            category="rfq",
            received_at=mail.received_at,
        )
        rfq.handle_rfq(mail, sid)
        for answer in (m for m in case["mails"] if m.get("category") == "customer"):
            row = db.validation_get(sid)
            if not row or row["state"] not in ("awaiting_clarification", "resolving"):
                break
            reply = _mail(answer, run.delta, references=[sid])
            db.ingest_mail(
                mail_id=reply.id,
                sender=reply.sender,
                recipient=reply.to,
                subject=reply.subject,
                body=reply.body,
                category="customer",
                received_at=reply.received_at,
                references=[sid],
                in_reply_to=answer.get("in_reply_to"),
            )
            clarification.handle_reply(reply, sid)
        return _result(sid, run)


def _result(sid: int, run: _Run) -> dict[str, Any]:
    session = db.get_session(sid) or {}
    quote = session["quotes"][0] if session.get("quotes") else None
    body = next(
        (
            m["body"]
            for m in session.get("mails", [])
            if quote and m["id"] == quote.get("mail_id")
        ),
        None,
    )
    asked = sorted({m["to"] for m in run.sent if "[AP-" in m["subject"]})
    validation = db.validation_get(sid) or {}
    issues = (validation.get("draft") or {}).get("issues") or []
    clarifications = [
        m for m in session.get("mails", []) if m.get("category") == "clarification"
    ]
    warnings = sorted(
        {
            e["kind"]
            for e in session.get("events", [])
            if e.get("level") in ("warn", "error")
        }
    )
    request = None
    if session.get("request"):
        stored = session["request"]
        request = {
            "airport": _stored_origin(stored),
            "destination": stored.get("destination"),
            "pickup_zip": stored.get("pickup_zip"),
            "weight_kg": stored.get("weight_kg"),
            "volume_cbm": stored.get("volume_cbm"),
            "preference": stored.get("preference"),
        }
    return {
        "session_id": sid,
        "round_no": run.case.get("round_no"),
        "outcome": "quoted"
        if quote
        else ("clarification" if clarifications else "no_quote"),
        "status": session.get("status"),
        "request": request,
        "airline": quote["airline"] if quote else None,
        "source": quote["source"] if quote else None,
        "route": f"{quote['origin']}-{quote['destination']}" if quote else None,
        "transit_days": quote["transit_days"] if quote else None,
        "total_minor": quote["total_minor"] if quote else None,
        "carriers_asked": asked,
        "clarifications_sent": len(clarifications),
        "open_issues": sorted({i.get("field", "") for i in issues}),
        "warnings": warnings,
        "notes": sorted(run.notes),
        "network_blocked": run.network_blocked,
        "quote_body": body,
    }


def replay_all(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="rfq-replay-") as tmp:
        seen = corpus.get("lane_observations", [])
        try:
            return [
                replay_case(c, Path(tmp), seen)
                for c in corpus["cases"]
                if c.get("llm_extraction") is not None
            ]
        finally:
            # Windows cannot unlink the final replay DB while SQLite still owns it.
            db.close_thread_connection()


# ---------------------------------------------------------------- diff


_COMPARED = (
    "outcome",
    "request",
    "airline",
    "source",
    "route",
    "transit_days",
    "total_minor",
    "carriers_asked",
    "clarifications_sent",
    "open_issues",
    "quote_body",
)


def diff(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    corpus: dict[str, Any] | None = None,
) -> list[str]:
    """One line per changed field, with the board accuracy as price evidence."""
    board = {
        c["session_id"]: c.get("board") or {}
        for c in (corpus or {"cases": []})["cases"]
    }
    old = {r["session_id"]: r for r in before}
    lines = []
    for new in after:
        sid = new["session_id"]
        prev = old.get(sid)
        if prev is None:
            lines.append(f"round {new['round_no']} ({sid}): new case")
            continue
        for key in _COMPARED:
            if prev.get(key) == new.get(key):
                continue
            if key == "quote_body":
                lines.append(f"round {new['round_no']} ({sid}): quote text changed")
                continue
            line = f"round {new['round_no']} ({sid}): {key} {prev.get(key)!r} -> {new.get(key)!r}"
            if key == "total_minor":
                b = board.get(sid, {})
                line += f"  [board: {b.get('verdict')}, accuracy {b.get('accuracy')}]"
                if b.get("accuracy") == 45.0:
                    line += "  WAS AT BEST PRICE"
            lines.append(line)
    for sid in old.keys() - {r["session_id"] for r in after}:
        lines.append(f"session {sid}: missing from the new run")
    return lines


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.agents.rfq_replay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("corpus")
    c.add_argument("session_dir", type=Path)
    c.add_argument("--board", type=Path)
    r = sub.add_parser("run")
    r.add_argument("--corpus", type=Path, default=CORPUS)
    r.add_argument("--out", type=Path)
    r.add_argument("--compare", type=Path)
    d = sub.add_parser("diff")
    d.add_argument("before", type=Path)
    d.add_argument("after", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "corpus":
        corpus = build_corpus(args.session_dir, args.board)
        json.dump(corpus, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "run":
        corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
        results = replay_all(corpus)
        text = json.dumps(results, ensure_ascii=False, indent=1) + "\n"
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        for res in results:
            sys.stderr.write(
                f"round {res['round_no']} ({res['session_id']}): {res['outcome']} "
                f"{res['airline'] or ''} {res['total_minor'] or ''}\n"
            )
        cases = {c["session_id"]: c for c in corpus["cases"]}
        differ = [
            str(res["round_no"])
            for res in results
            if (res["airline"], res["total_minor"])
            != tuple(
                (cases[res["session_id"]].get("production_quote") or {}).get(k)
                for k in ("airline", "total_minor")
            )
        ]
        sys.stderr.write(
            f"same airline and total as production: {len(results) - len(differ)}/{len(results)}"
            f" (differs: rounds {', '.join(differ) or '-'})\n"
        )
        if args.compare:
            before = json.loads(args.compare.read_text(encoding="utf-8"))
            changes = diff(before, results, corpus)
            sys.stdout.write("\n".join(changes or ["no changes"]) + "\n")
            return 1 if changes else 0
        return 0
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    changes = diff(before, after, json.loads(CORPUS.read_text(encoding="utf-8")))
    sys.stdout.write("\n".join(changes or ["no changes"]) + "\n")
    return 1 if changes else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
