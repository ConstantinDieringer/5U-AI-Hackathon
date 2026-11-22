"""Explicit synthetic gateway evaluation. No production mailbox, transport or database."""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from app import db, hackloop, llm
from app.agents import rfq
from app.mail.rfq_validation import validate
from app.models import Mail, RateOption
from app.pricing.selection import select_air

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/arrival_eval.json"
READY = date(2027, 4, 12)
OPTIONS = [
    RateOption("Economy", "FRA", "MAD", 6, 30000, "EUR", 30000, "sheet"),
    RateOption("Standard", "FRA", "MAD", 3, 45000, "EUR", 45000, "sheet"),
    RateOption("Express", "FRA", "MAD", 1, 80000, "EUR", 80000, "sheet"),
]


def evaluate(case: dict, sid: int) -> dict:
    body = (
        "2 pieces, 100 kg gross, 1.50 cbm. Self-delivery to FRA on 2027-04-12. "
        "Please quote airport to airport FRA to MAD.\n" + case["text"]
    )
    mail = Mail(
        sid,
        sid,
        "sender@example.invalid",
        "desk@example.invalid",
        "FRA to MAD",
        body,
        "2027-04-10T08:00:00+00:00",
    )
    db.ingest_mail(
        mail_id=sid,
        sender=mail.sender,
        recipient=mail.to,
        subject=mail.subject,
        body=body,
        category="rfq",
        received_at=mail.received_at,
    )
    sources = {"manifest.txt": case["attachment"]} if "attachment" in case else {}
    start = time.monotonic()
    with patch.object(rfq, "read_sources", return_value=(sources, [], [])):
        raw = rfq.extract_rfq(mail, sid)
    if raw is None:
        raise RuntimeError("gateway or extraction failed; result is unavailable")
    checked, draft = validate(mail, raw, attachments=sources)
    issues = sorted({issue["field"] for issue in draft.issues})
    if issues or checked is None:
        action = "ask"
    else:
        result = select_air(checked, OPTIONS, [], set(), "ratio", READY)
        action = result.choice.airline if result.choice else "ask"
    deadline = checked.arrival_deadline if checked else None
    passed = case["action"] == action and (
        case.get("ask_field") in issues if action == "ask" else case["deadline"] == deadline
    )
    facts = raw.facts if hasattr(raw, "facts") else raw.model_dump()
    return {
        "case": case["case"],
        "pass": passed,
        "actual": action,
        "expected": case["action"],
        "deadline": deadline,
        "fields": issues,
        "extracted_deadline": facts.get("arrival_deadline"),
        "extracted_preference": facts.get("preference"),
        "seconds": round(time.monotonic() - start, 2),
    }


def main() -> None:
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))

    def refuse(request):
        raise AssertionError("Synthetic evaluation attempted non-gateway transport")

    transport = httpx.MockTransport(refuse)
    with (
        tempfile.TemporaryDirectory() as tmp,
        httpx.Client(transport=transport) as blocked,
        ExitStack() as cleanup,
    ):
        db.init_db(str(Path(tmp) / "synthetic.db"))
        cleanup.callback(db.close_thread_connection)
        with patch.object(hackloop, "_client", blocked):
            results = []
            for sid, case in enumerate(cases, 1):
                row = evaluate(case, sid)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                results.append(row)
    print(
        json.dumps(
            {
                "model": llm.model(),
                "passed": sum(r["pass"] for r in results),
                "total": len(results),
                "median_seconds": statistics.median(r["seconds"] for r in results),
            }
        )
    )


if __name__ == "__main__":
    main()
