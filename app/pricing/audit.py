"""Read-only pricing evidence report: python -m app.pricing.audit."""

import json

from app import db


def report() -> list[dict]:
    results = []
    for row in db.pricing_evidence():
        payload = json.loads(row["payload_json"] or "{}")
        selected = payload.get("selected", {})
        plain, best = (
            selected.get("plain_total_minor"),
            selected.get("breakpoint_total_minor"),
        )
        results.append(
            {
                "session_id": row["session_id"],
                "verdict": row["verdict"],
                "reason": row["reason"],
                "points": json.loads(row["points_json"] or "null"),
                "plain_total_minor": plain,
                "breakpoint_total_minor": best,
                "tariff_version": selected.get("tariff_version"),
                "breakpoint_relevant": plain is not None
                and best is not None
                and plain != best,
                "conclusion": "requires scorer best-price evidence; verdict alone is inconclusive",
            }
        )
    return results


if __name__ == "__main__":
    print(json.dumps(report(), indent=2))
