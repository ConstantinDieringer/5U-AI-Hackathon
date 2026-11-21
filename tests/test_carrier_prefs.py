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
    carrier_prefs.remember(
        cust, "We would prefer Zephyr Steppe Cargo.", 11000
    )  # no downgrade
    assert carrier_prefs.excluded(cust) == {"zephyr steppe cargo"}
    assert carrier_prefs.preferred(cust) == set()
    assert carrier_prefs.excluded("Meyer-Industrie@hackloop.mail ") == {
        "zephyr steppe cargo"
    }
    assert carrier_prefs.excluded("other@hackloop.mail") == set()
