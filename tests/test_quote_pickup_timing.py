"""Truck lead time is separate from flight transit and a confirmed collection slot."""

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
