"""Air-service selection shared by the RFQ worker and the manual draft."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.models import RateOption, Rfq
from app.pricing.pricing import most_balanced, pick, within_arrival_deadline
from app.pricing.pricing import today as pricing_day


@dataclass(frozen=True)
class AirSelection:
    outcome: Literal["selected", "no_options", "deadline_unmet"]
    choice: RateOption | None
    eligible: list[RateOption]
    late: list[RateOption]
    preferred: bool
    has_tariff_or_portal: bool


def best_air(
    air: list[RateOption], bids: list[RateOption], preference: str, bid_balance: str | None = None
) -> RateOption | None:
    bid = most_balanced(bids, lambda o: o.total_eur_minor, lambda o: o.transit_days, bid_balance)
    return pick(air + ([bid] if bid else []), preference)


def select_air(
    request: Rfq,
    air: list[RateOption],
    bids: list[RateOption],
    preferred: set[str],
    bid_balance: str,
    today: date | None = None,
) -> AirSelection:
    """Filter hard constraints before ranking either the full or preferred candidate pool."""
    excluded = set(request.excluded_airlines)
    rates = [o for o in air if o.airline.lower() not in excluded]
    offers = [o for o in bids if o.airline.lower() not in excluded]
    today = today or pricing_day()
    feasible_rates = within_arrival_deadline(request, rates, today)
    feasible_offers = within_arrival_deadline(request, offers, today)
    eligible = feasible_rates + feasible_offers
    late = []
    if request.arrival_deadline:
        accepted = {id(o) for o in eligible}
        late = [o for o in rates + offers if id(o) not in accepted]
    if not eligible:
        return AirSelection(
            "deadline_unmet" if late else "no_options", None, [], late, False, False
        )
    favourite = (
        best_air(
            [o for o in feasible_rates if o.airline.lower() in preferred],
            [o for o in feasible_offers if o.airline.lower() in preferred],
            request.preference,
            bid_balance,
        )
        if preferred
        else None
    )
    choice = favourite or best_air(feasible_rates, feasible_offers, request.preference, bid_balance)
    return AirSelection(
        "selected", choice, eligible, late, favourite is not None, bool(feasible_rates)
    )
