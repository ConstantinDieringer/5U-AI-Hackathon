"""Second opinion on a booking reply the consent rules cannot decide safely.

The rules in booking.py are fast and free, but every new wording from 5U broke them
("we won't go ahead" booked in round 70; "Yes" / "Ja" / "OK" were unclear in round 60).
This module is asked only when the rules found no decision, or found a yes next to a doubt
word ("not", "hold off", "leider", "pas" ...). One small gateway call with a strict schema;
on any failure the answer is "unclear" and the existing clarification path runs.
An explicit decline found by the rules is never overruled (booking.py decides that).
Rollback: CONSENT_LLM_CHECK=false.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

Label = Literal["book", "decline", "question", "later", "change", "unclear"]
TIMEOUT_S = 12.0
MAX_TEXT_CHARS = 1500
PROMPT = (
    "A customer replies to our air freight quote. Classify their reply. "
    "book: they clearly and unconditionally tell us to book or accept the quoted offer now. "
    "decline: they reject the offer or say not to book. "
    "question: they ask something before deciding. "
    "later: they will decide or check later. "
    "change: they want a different option, weight, date or price. "
    "unclear: anything else or any doubt. "
    'Any language. Answer as JSON {"label": ...}.'
)
SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "consent",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "label": {
                    "type": "string",
                    "enum": ["book", "decline", "question", "later", "change", "unclear"],
                }
            },
            "required": ["label"],
        },
    },
}

# a yes next to one of these is checked before booking (cheap: only rare replies match)
DOUBT = re.compile(
    r"n't\b|\bnot\b|\bnever\b|\bno\b|\bnein\b|\bnicht\b|\bkein\w*|\bdoch\s+nicht\b|"
    r"\bnon\b|\bpas\b|\bplus\b|\bjamais\b|\bnada\b|\bnessun\w*|"
    r"\bhold\s+off\b|\bon\s+hold\b|\bpostpone\w*|\bunfortunately\b|\bafter\s+all\b|"
    r"\bwait\w*\b|\bleider\b|\bwarten\b|\bverschieb\w*|\bzurückstellen\b|\babwarten\b",
    re.IGNORECASE,
)
# The part of DOUBT that negates or postpones the yes; without the model it must not book.
HARD_DOUBT = re.compile(
    r"n't\b|\bnot\b|\bnever\b|\bno\b|\bnein\b|\bnicht\b|\bkein\w*|\bnon\b|\bpas\b|"
    r"\bjamais\b|\bnada\b|\bnessun\w*|\bhold\s+off\b|\bon\s+hold\b|\bpostpone\w*|"
    r"\bafter\s+all\b|\bwait\w*\b|\bwarten\b|\bverschieb\w*|\bzurückstellen\b|\babwarten\b",
    re.IGNORECASE,
)


class _Answer(BaseModel):
    label: Label


def parse(raw: str) -> Label:
    """Validated label; "unclear" for anything malformed. Pure (unit tested)."""
    try:
        return _Answer.model_validate(json.loads(raw)).label
    except (ValueError, ValidationError):
        return "unclear"


def classify(text: str, session_id: int) -> Label:
    from app import llm

    text = text.strip()[:MAX_TEXT_CHARS]
    if not text:
        return "unclear"
    try:
        raw = llm.complete(
            "consent_check",
            [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
            session_id,
            max_tokens=200,
            response_format=SCHEMA,
            timeout_s=TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - budget, timeout or gateway error
        log.warning("session %s: consent check failed: %s", session_id, exc)
        return "unclear"
    return parse(raw)
