"""Language of a mail from function words, never from names or places."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.mail.language import detect

# The real RFQs the LLM tagged "de" although they are English (mails 421, 1223, 1460).
SONNBERG = (
    "Dear Sir or Madam,\n\nCould you please quote the fastest air freight option to JFK "
    "(New York, US)? The shipment will be ready for pickup at our site in 61018 (Bad Homburg), "
    "Germany, on 2026-11-25. Please arrange trucking from our site to the departure airport."
    "\n\nKind regards\nAnna Sonnberg\nHead of Purchasing and Logistics\n"
    "Sonnberg Kaffeeroesterei GmbH"
)


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        (SONNBERG, "en"),
        (
            "Thank you for your offer. That works for us, please go ahead and book it.\n\n"
            "Cheers,\nKlaus Weidner\nWeidner Spielwaren GmbH, Nuremberg",
            "en",
        ),
        (
            "Sehr geehrte Damen und Herren,\n\nbitte senden Sie uns ein Angebot für die "
            "Luftfracht von 500 kg nach JFK. Die Sendung ist ab Montag abholbereit.\n\n"
            "Viele Grüße\nMax",
            "de",
        ),
        (
            "Bonjour,\n\npouvez-vous nous envoyer une offre pour le fret aérien ? Merci.",
            "fr",
        ),
        ("Passt, bitte buchen.", "de"),
        ("", "en"),
    ],
)
def test_detect(text: str, lang: str) -> None:
    assert detect(text) == lang


def test_quoted_text_is_ignored() -> None:
    assert (
        detect("Thanks, please book it.\n\n> Sehr geehrte Damen und Herren, bitte ...")
        == "en"
    )


def test_all_stored_rfqs_are_english() -> None:
    """Every RFQ so far was English; 3 of 6 were quoted in German before this fix."""
    path = Path("data/forwarder.db")
    if not path.exists():
        pytest.skip("no local DB")
    c = sqlite3.connect(path)
    bodies = [
        r[0]
        for r in c.execute(
            "SELECT m.body FROM sessions s JOIN mails m ON m.id = s.id WHERE s.kind = 'rfq'"
        )
    ]
    assert bodies and all(detect(b) == "en" for b in bodies)
