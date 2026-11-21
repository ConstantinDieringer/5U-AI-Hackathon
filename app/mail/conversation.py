"""Bounded correspondence for personal wording, never a source of current price facts."""

import re
from typing import TypedDict

from app import db
from app.mail.content import current_message


class ConversationEntry(TypedDict):
    mail_id: int
    scope: str
    speaker: str
    date: str
    text: str


class ConversationContext(TypedDict):
    current_customer_message: str
    earlier_messages: list[ConversationEntry]


def quote_conversation(session_id: int) -> ConversationContext:
    session = db.get_session(session_id)
    if not session:
        return {"current_customer_message": "", "earlier_messages": []}
    root = next((m for m in session["mails"] if m["id"] == session_id), None)
    current = current_message(root["body"]).text[:3000] if root else ""
    earlier: list[ConversationEntry] = []
    for row in db.customer_correspondence(session_id):
        text = current_message(row["body"] or "").text[:900]
        # Older monetary amounts are unnecessary for relationship context.
        text = re.sub(
            r"(?:EUR|USD|€|\$)\s*[\d.,]+|[\d.,]+\s*(?:EUR|USD|€|\$)",
            "(previous price omitted)",
            text,
            flags=re.IGNORECASE,
        )
        if text:
            earlier.append(
                {
                    "mail_id": row["id"],
                    "scope": row["scope"],
                    "speaker": "customer" if row["direction"] == "in" else "our_team",
                    "date": row["received_at"],
                    "text": text,
                }
            )
    return {"current_customer_message": current, "earlier_messages": earlier}
