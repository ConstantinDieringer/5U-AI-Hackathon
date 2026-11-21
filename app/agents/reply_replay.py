"""Offline replay of customer replies AFTER our quote: python -m app.agents.reply_replay.

Each corpus round first runs through the RFQ replay (app.agents.rfq_replay) to its quote, then
customer replies arrive as the poller hands them over (open clarification first, else the
booking agent). Two kinds of replies:

- stored: the real replies the corpus holds after the production quote;
- injected: the real round 54, 55 and 56 replies (their own threads are not in the corpus),
  put into every quoted round. Round 54 verbatim names DEL and 22842, so elsewhere it shows
  the mismatch guard; "round54-own" asks the same questions with the round's own lane, pickup
  and ready date. Both are followed by "Yes, please book.".

Runs with PREBOOK_ANSWER off and on and lists every round whose outcome changes. Nothing leaves
the machine: sends and bookings are captured, every other 5U or gateway call raises.

    python -m app.agents.reply_replay [--out results.json]
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from app import db, hackloop, llm
from app.agents import booking, clarification, prebook, rfq_replay
from app.models import Mail

ROUND_54 = (
    "Before we book, could you confirm whether this is the fastest option to DEL with pickup "
    "from 22842 on 28 November? What's the earliest arrival with this option?"
)
ROUND_55 = "I'll look at it and get back to you."
ROUND_56 = "I'll check whether we can proceed with the booking and get back to you."
YES = "Yes, please book."
INJECTED: dict[str, tuple[str, ...]] = {
    "round54+yes": (ROUND_54, YES),
    "round55": (ROUND_55,),
    "round56": (ROUND_56,),
}


def _round_54_own(request: dict[str, Any]) -> str:
    """Round 54's questions with this round's destination, pickup and ready date."""
    pickup = ""
    if request.get("pickup_zip") and request.get("ready_date"):
        ready = date.fromisoformat(str(request["ready_date"])[:10])
        pickup = f" with pickup from {request['pickup_zip']} on {ready.day} {ready:%B}"
    return (
        f"Before we book, could you confirm whether this is the fastest option to "
        f"{request.get('destination')}{pickup}? What's the earliest arrival with this option?"
    )


@dataclasses.dataclass
class _Capture:
    next_id: int = 950_000_000
    sent: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    booked: list[str] = dataclasses.field(default_factory=list)


@contextlib.contextmanager
def _offline(cap: _Capture, flag: bool) -> Iterator[None]:
    def blocked(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("reply replay: network disabled")

    def send(to: str, subject: str, body: str, in_reply_to: int | None = None, **_k: Any):
        cap.next_id += 1
        mail = {"id": cap.next_id, "to": to, "subject": subject, "body": body,
                "in_reply_to": in_reply_to, "references": [],
                "received_at": datetime.now(UTC).isoformat()}  # fmt: skip
        cap.sent.append(mail)
        return mail

    def book(rate_id: str, team_reference: str) -> dict[str, Any]:
        cap.booked.append(rate_id)
        return {"booking_reference": f"BK-REPLAY{len(cap.booked):03d}", "rate_id": rate_id}

    def no_llm(*_a: Any, **_k: Any) -> str:
        raise ValueError("reply replay: no LLM")

    settings = dataclasses.replace(booking.settings, prebook_answer=flag, airline_confirm_wait_s=0)
    with contextlib.ExitStack() as stack:
        for target, name, value in (
            (hackloop, "_request", blocked),
            (hackloop, "send", send),
            (hackloop, "book", book),
            (llm, "complete", no_llm),
            (booking, "settings", settings),
            (prebook, "MIN_GAP_S", 0.0),
        ):
            stack.enter_context(mock.patch.object(target, name, value))
        yield


def _stored_replies(case: dict[str, Any], sid: int) -> tuple[str, ...]:
    """The round's real customer replies the RFQ replay did not consume as clarification
    answers: what arrived after our quote (round 13's stored quote time is later than its
    acceptance, so timestamps cannot tell)."""
    consumed = {
        str(m["body"])
        for m in (db.get_session(sid) or {}).get("mails", [])
        if m["direction"] == "in"
    }
    return tuple(
        str(m["body"])
        for m in case["mails"]
        if m.get("category") == "customer" and str(m["body"]) not in consumed
    )


def _deliver(sid: int, bodies: tuple[str, ...], cap: _Capture) -> dict[str, Any]:
    session = db.get_session(sid) or {}
    customer = str(session.get("customer") or "")
    kinds = []
    for body in bodies:
        cap.next_id += 1
        now = datetime.now(UTC).isoformat()
        db.ingest_mail(mail_id=cap.next_id, sender=customer, recipient="austrian-power@hackloop.mail",
                       subject=f"Re: {session.get('subject')}", body=body, category="customer",
                       received_at=now, references=[sid])  # fmt: skip
        mail = Mail(cap.next_id, sid, customer, "austrian-power@hackloop.mail",
                    f"Re: {session.get('subject')}", body, now, references=[sid])  # fmt: skip
        kinds.append(booking.classify_reply(body))
        if not clarification.handle_reply(mail, sid):
            booking.handle_reply(mail, sid)
    session = db.get_session(sid) or {}
    quote_mail = int((session.get("quotes") or [{}])[0].get("mail_id") or 0)
    return {
        "kinds": kinds,
        "booked": bool(session.get("bookings")),
        "after_quote": [
            m["category"]
            for m in session.get("mails", [])
            if m["direction"] == "out" and int(m["id"]) > quote_mail
        ],
    }


def replay(flag: bool) -> list[dict[str, Any]]:
    corpus = json.loads(rfq_replay.CORPUS.read_text(encoding="utf-8"))
    seen = corpus.get("lane_observations", [])
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="reply-replay-") as tmp:
        try:
            for case in corpus["cases"]:
                if case.get("llm_extraction") is None:
                    continue
                # None: built from the replayed session (its leftover replies, its request)
                scenarios: dict[str, tuple[str, ...] | None] = {"stored": None, **INJECTED}
                scenarios["round54-own+yes"] = None
                for name, bodies in scenarios.items():
                    workdir = Path(tmp) / f"{case['session_id']}-{name}"
                    workdir.mkdir()
                    quoted = rfq_replay.replay_case(case, workdir, seen)
                    if quoted["outcome"] != "quoted":
                        break
                    sid = int(case["session_id"])
                    if name == "stored":
                        bodies = _stored_replies(case, sid)
                        if not bodies:
                            continue
                    elif bodies is None:
                        request = (db.get_session(sid) or {}).get("request")
                        bodies = (_round_54_own(request or {}), YES)
                    cap = _Capture()
                    with _offline(cap, flag):
                        outcome = _deliver(sid, bodies, cap)
                    results.append(
                        {
                            "round_no": case.get("round_no"),
                            "session_id": case["session_id"],
                            "scenario": name,
                            **outcome,
                        }
                    )
        finally:
            db.close_thread_connection()
    return results


def compare(off: list[dict[str, Any]], on: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(changes, regressions). A regression: booked with the flag off, not booked with it on."""
    before = {(r["session_id"], r["scenario"]): r for r in off}
    changes, worse = [], []
    for r in on:
        prev = before.get((r["session_id"], r["scenario"]))
        if prev is None or (prev["booked"], prev["after_quote"]) == (r["booked"], r["after_quote"]):
            continue
        line = (
            f"round {r['round_no']} {r['scenario']}: {prev['after_quote']} booked={prev['booked']}"
            f" -> {r['after_quote']} booked={r['booked']}"
        )
        changes.append(line)
        if prev["booked"] and not r["booked"]:
            worse.append(line)
    return changes, worse


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.agents.reply_replay")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    off, on = replay(False), replay(True)
    if args.out:
        args.out.write_text(json.dumps({"off": off, "on": on}, indent=1) + "\n", encoding="utf-8")
    changes, worse = compare(off, on)
    stored = [r for r in on if r["scenario"] == "stored"]
    sys.stdout.write(
        f"{len(on)} replies replayed ({len(stored)} stored, {len(on) - len(stored)} injected)\n"
    )
    sys.stdout.write("\n".join(changes or ["no changes"]) + "\n")
    sys.stdout.write(f"worse with PREBOOK_ANSWER on: {len(worse)}\n")
    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
