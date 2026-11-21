import pytest

from app.mail.subjects import shipment_subject


@pytest.mark.parametrize(
    "origin,destination,lang,expected",
    [
        ("CGN", "SCL", "en", "Air freight CGN to SCL"),
        ("fra", "nrt", "de-DE", "Luftfracht FRA nach NRT"),
        ("LHR", "JFK", None, "Air freight LHR to JFK"),
    ],
)
def test_verified_route_replaces_historical_subject(origin, destination, lang, expected):
    assert shipment_subject(origin, destination, lang, "FW: AMS TLV 104 crates") == expected


@pytest.mark.parametrize(
    "origin,destination",
    [
        (None, "SCL"),
        ("CGN", None),
        ("", "SCL"),
        ("Germany", "SCL"),
        ("CGN\nBcc", "SCL"),
    ],
)
def test_incomplete_or_invalid_route_is_not_inferred_from_history(origin, destination):
    assert shipment_subject(origin, destination, "en", "FW: AMS TLV") == "FW: AMS TLV"
