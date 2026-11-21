"""Evaluate the phishing gate's Layer A on the labelled real-mail set. No network, no LLM.

    python -m app.mail.screening_eval        # table + every non-genuine verdict

Layer B is not run here (it needs the live gateway); mails it would see are listed as unsure.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app.mail import screening
from app.models import Mail

FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "screening" / "labelled_mails.json"
)


def load(path: Path = FIXTURE) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["mails"])


def _mail(row: dict[str, Any]) -> Mail:
    mid = row["id"] if isinstance(row["id"], int) else 10**9
    refs = [int(x) for x in row.get("references") or []]
    return Mail(
        id=mid,
        thread_root=refs[0] if refs else mid,
        sender=row["sender"],
        to="austrian-power@hackloop.mail",
        subject=row["subject"],
        body=row["body"],
        received_at="",
        attachments=list(row.get("attachments") or []),
        references=refs,
        raw={"in_reply_to": row.get("in_reply_to")},
    )


def evaluate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each mail with what production would have known when it arrived."""
    customers: list[tuple[int, str]] = sorted(
        {
            (r["id"], screening.address(r["session_customer"]))
            for r in rows
            if isinstance(r["id"], int) and r.get("session_customer") and r["label"] == "genuine"
        }
    )
    out = []
    for row in rows:
        mid = row["id"] if isinstance(row["id"], int) else 10**9
        sender = screening.address(row["sender"])
        seen = frozenset(c for i, c in customers if i < mid)
        ctx = screening.Context(
            known_party=sender in screening.fixed_addresses(),
            thread_known=bool(row["thread_known"]),
            known_customer=sender in seen,
            thread_party=bool(row["thread_known"])
            and sender == screening.address(row.get("session_customer") or ""),
            known_addresses=screening.fixed_addresses() | seen,
        )
        start = time.perf_counter()
        result = screening.rules(_mail(row), ctx)
        out.append({**row, "result": result, "us": (time.perf_counter() - start) * 1e6})
    return out


def report(results: list[dict[str, Any]]) -> str:
    table = Counter((r["label"], r["result"].verdict.value) for r in results)
    lines = ["label     | genuine | unsure | phishing"]
    for label in ("genuine", "phishing"):
        lines.append(
            f"{label:<9} | {table[(label, 'genuine')]:>7} | {table[(label, 'unsure')]:>6} "
            f"| {table[(label, 'phishing')]:>8}"
        )
    times = sorted(r["us"] for r in results)
    lines.append(
        f"Layer A time per mail: median {times[len(times) // 2]:.0f} µs, max {times[-1]:.0f} µs"
    )
    for r in results:
        res = r["result"]
        if res.verdict.value != "genuine" or r["label"] != "genuine":
            lines.append(f"- [{r['label']}] {r['id']} {r['sender']}: {screening.summary(res)}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(evaluate(load())))
