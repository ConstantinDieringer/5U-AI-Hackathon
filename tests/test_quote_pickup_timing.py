"""Truck lead time is separate from flight transit and a confirmed collection slot."""

import json
from decimal import Decimal

import pytest

from app.agents import bids, rfq
from app.models import RateOption, Rfq


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("hours", [None, 11, 31])
def test_quote_discloses_pickup_slot_uncertainty(language, hours):
    request = Rfq(
        pickup_zip="53088", destination="SGN", weight_kg=1403, language=language
    )
    option = RateOption("Air", "CGN", "SGN", 2, 10000, "EUR", 10000, "sheet")
    carrier = bids.Carrier("Truck", "trucker", "truck@example.com", ("53",))
    truck = bids.Offer(
        carrier, Decimal(200), hours / 24 if hours is not None else None, 1
    )
    body = rfq.quote_text(request, option, 30000, truck, 0)
    # human wording: pickup time follows after booking, truck lead time in hours
    assert ("exact pickup time" if language == "en" else "genauen Abholtermin") in body
    if hours is not None:
        assert f"about {hours} hours" in body or f"etwa {hours} Stunden" in body
    assert "Carrier-reported" not in body
    assert body.count("€") == 1


def test_airport_delivery_does_not_invent_pickup():
    request = Rfq(origin="CGN", destination="SGN", weight_kg=1403)
    option = RateOption("Air", "CGN", "SGN", 2, 10000, "EUR", 10000, "sheet")
    body = rfq.quote_text(request, option, 10000, None, 0)
    assert "pickup time" not in body and "Abholtermin" not in body


@pytest.mark.parametrize(
    "text,kind",
    [
        (
            "We can pick up within 5 hours of the freight being ready.",
            "collection_lead",
        ),
        ("Transit time from pickup: 30 hours.", "road_transit"),
        ("Fahrzeit: 6 Stunden.", "road_transit"),
        ("Abholung innerhalb von 8 Stunden.", "collection_lead"),
        ("Estimated lead: 11 hours.", "unspecified"),
        ("Pickup within 5 hours. Transit time is 30 hours.", "collection_lead"),
    ],
)
def test_duration_meaning_follows_the_selected_duration(text, kind):
    assert bids.timing_kind(text) == kind


@pytest.mark.parametrize("kind", ["collection_lead", "unspecified", "road_transit"])
def test_composer_cannot_turn_collection_lead_into_road_duration(kind):
    request = Rfq(pickup_zip="93721", destination="PVG", weight_kg=540)
    option = RateOption("Air", "MUC", "PVG", 3, 10000, "EUR", 10000, "sheet")
    truck = bids.Offer(
        bids.Carrier("T", "trucker", "t@example.com", ("93",)),
        Decimal(100),
        5 / 24,
        1,
        timing_kind=kind,
    )
    body = (
        "Hello, Air MUC to PVG: 200,00 € all-in. "
        "Road collection to the airport takes five hours.\n\nYour austrian Power Team"
    )
    raw = json.dumps(
        {
            "body": body,
            "answered_questions": [],
            "unavailable_questions": [],
            "addressed_constraints": [],
            "unavailable_constraints": [],
        }
    )
    if kind == "road_transit":
        assert (
            rfq._validate_composed(raw, request, option, "200,00 €", truck=truck)
            == body
        )
    else:
        with pytest.raises(ValueError, match="not verified road duration"):
            rfq._validate_composed(raw, request, option, "200,00 €", truck=truck)
    safe = rfq.quote_text(request, option, 20000, truck, 1)
    assert "a lead time of about 5 hours" in safe
    assert "from your door to the airport" not in safe
