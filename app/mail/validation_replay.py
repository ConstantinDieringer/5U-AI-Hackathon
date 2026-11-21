"""Read-only replay against the configured CRM: python -m app.mail.validation_replay.

Uses stored extractions and local attachments only. Never calls a model, polls or sends.
"""

import json

from app import db
from app.mail.parse import mail_from_api
from app.mail.rfq_sources import read_sources
from app.mail.rfq_validation import validate
from app.models import Mail, RfqDraft


def replay():
    results = []
    for r in db.validation_replay_inputs():
        m = (
            mail_from_api(json.loads(r["raw_json"]))
            if r.get("raw_json")
            else Mail(
                r["id"],
                r["id"],
                r["sender"],
                r["recipient"],
                r["subject"],
                r["body"],
                r["received_at"],
            )
        )
        facts = json.loads(r["request_json"] or "{}")
        texts, media, problems = read_sources(m, local_only=True)
        if media:
            problems.append(
                {"field": "attachments", "code": "image_requires_extraction"}
            )
        _, draft = validate(m, RfqDraft(facts=facts), texts, problems)
        results.append(
            {
                "session_id": r["id"],
                "stored_extraction_available": bool(facts),
                "draft": draft.model_dump(mode="json"),
            }
        )
    return results


if __name__ == "__main__":
    print(json.dumps(replay(), ensure_ascii=False, indent=2))
