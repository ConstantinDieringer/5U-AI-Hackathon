"""Rule reader (app/mail/rules.py), shadow only: precision on the stored live rounds."""

import json
import re
from pathlib import Path

from app.mail import rules
from app.mail.content import current_message

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "replay" / "rfq_sessions.json").read_text("utf-8")
)["cases"]


def _read(body: str, subject: str = "Air freight quote") -> rules.Fields:
    return rules.read(subject, body, *rules.airports())


def test_rules_never_contradict_a_stored_extraction_except_known_model_errors() -> None:
    result = rules.compare(CASES)
    assert len(result["destination"]["same"]) == 31
    for field in rules.SHADOW_FIELDS:
        assert result[field]["differ"] == [], field
    # the rules name an airport the mail states and the model missed (rounds 23 and 28)
    assert result["origin"]["extra"] == [5844, 7237]
    # the model gave a ZIP the mail does not contain (round 9: pickup "from our warehouse")
    assert result["pickup_zip"]["silent"] == [4363]


def test_model_origins_the_rules_do_not_confirm_are_not_in_the_mail() -> None:
    """Every origin only the model gave was a guess: an ICAO code, the destination itself, an
    airport from an old forwarded subject, or one derived from the pickup ZIP (code does that)."""
    by_id = {c["session_id"]: c for c in CASES}
    for sid in rules.compare(CASES)["origin"]["silent"]:
        root = by_id[sid]["mails"][0]
        own = current_message(root["body"], root["subject"]).text
        stored = by_id[sid]["llm_extraction"]
        named = re.search(rf"\b{stored['origin']}\b", own)
        assert not named or stored["origin"] == stored["destination"], sid


def test_two_candidates_give_no_value() -> None:
    got = _read("Please quote to ORD (Chicago, US) or alternatively to JFK (New York, US).")
    assert got.destination is None
    assert _read("Cheapest please, but also tell us the fastest.").preference is None
    assert _read("Ready on 2026-11-25, delivery by 2026-11-30.").ready_date is None


def test_quantities_are_not_pickup_zips() -> None:
    got = _read("Pickup from our site, 12000 kg in total, ZIP follows tomorrow.")
    assert got.pickup_zip is None
