"""Customer carrier exclusions and preferences (round 10820: excluded_airline)."""

import pytest

from app import db
from app.agents import carrier_prefs


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "p.db"))


def test_parse_exclusion_from_round_10820():
    text = (
        "Please provide an all-in price in EUR.\n\nPlease do not offer Zephyr Steppe Cargo (ZS)"
        " for this shipment or for any of our future inquiries."
    )
    assert carrier_prefs.parse(text) == [("Zephyr Steppe Cargo", "exclude")]


def test_parse_preference_and_german():
    assert carrier_prefs.parse("We would prefer Lagoon Isles Airways if possible.") == [
        ("Lagoon Isles Airways", "prefer")
    ]
    assert carrier_prefs.parse("Bitte nicht mit Zephyr Steppe Cargo fliegen.") == [
        ("Zephyr Steppe Cargo", "exclude")
    ]
    assert carrier_prefs.parse("Zephyr Steppe Cargo is not acceptable for us.") == [
        ("Zephyr Steppe Cargo", "exclude")
    ]


def test_remembered_per_customer_for_future_requests():
    cust = "meyer-industrie@hackloop.mail"
    carrier_prefs.remember(cust, "Please do not offer Zephyr Steppe Cargo (ZS).", 10820)
    carrier_prefs.remember(cust, "We would prefer Zephyr Steppe Cargo.", 11000)  # no downgrade
    assert carrier_prefs.excluded(cust) == {"zephyr steppe cargo"}
    assert carrier_prefs.preferred(cust) == set()
    assert carrier_prefs.excluded("Meyer-Industrie@hackloop.mail ") == {"zephyr steppe cargo"}
    assert carrier_prefs.excluded("other@hackloop.mail") == set()


def test_backfill_reads_stored_customer_mails():
    db.ingest_mail(
        mail_id=10820,
        sender="meyer-industrie@hackloop.mail",
        recipient="t",
        subject="RFQ ULN",
        category="rfq",
        body="Please do not offer Zephyr Steppe Cargo (ZS) for any future inquiries.",
    )
    assert carrier_prefs.backfill() == 1
    assert carrier_prefs.excluded("meyer-industrie@hackloop.mail") == {"zephyr steppe cargo"}
    assert carrier_prefs.backfill() == 0


Z, L, P = "Zephyr Steppe Cargo", "Lagoon Isles Airways", "Pampero Air Cargo"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # the carrier by its code or without its generic last word
        ("Please do not offer ZS for this shipment.", [(Z, "exclude")]),
        ("Please do not offer Zephyr Steppe for this shipment.", [(Z, "exclude")]),
        # a negation refuses only inside its own clause
        (
            "We do not mind which airline you use, but we prefer Lagoon Isles Airways.",
            [(L, "prefer")],
        ),
        (
            "We do not want Zephyr Steppe Cargo, but we prefer Lagoon Isles Airways.",
            [(L, "prefer"), (Z, "exclude")],
        ),
        ("Our goods are not dangerous, please quote with Fjordline Air Cargo if possible.", []),
        # ... but a list of carriers is one clause
        (
            "Please do not use Zephyr Steppe Cargo, Pampero Air Cargo or Lagoon Isles Airways.",
            [(L, "exclude"), (P, "exclude"), (Z, "exclude")],
        ),
        # a negation that refuses nothing
        ("We have no objection to Zephyr Steppe Cargo.", []),
        # two capitals that are not a carrier in a refusing sentence
        ("Please do not quote via another airport than MIA (Miami, US).", []),
        ("Please do not delay, the goods are for our ZS department.", []),
    ],
)
def test_parse_codes_short_names_and_clause_scope(text, want):
    assert carrier_prefs.parse(text) == want


def test_country_code_in_brackets_is_never_a_carrier(monkeypatch):
    monkeypatch.setattr(carrier_prefs, "_codes", lambda: {"MN": Z})
    assert carrier_prefs.parse("Please do not ship via Beijing to ULN (Ulaanbaatar, MN).") == []
