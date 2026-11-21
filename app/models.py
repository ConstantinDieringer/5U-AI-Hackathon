"""Shared data contracts between modules (TECH_SPEC section 6). Field names are binding.

Money is integer cents (`*_minor`, context/code-standards.md: never floats); weights stay float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel


@dataclass
class Mail:
    id: int
    thread_root: int  # = references[0] if present, else id (session mapping: db.ingest_mail)
    sender: str
    to: str
    subject: str
    body: str
    received_at: str  # ISO 8601 UTC
    attachments: list[dict] = field(default_factory=list)  # {id, filename, content_type, size}
    references: list[int] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class Category(str, Enum):
    RFQ = "rfq"
    CUSTOMER = "customer"
    TRACKING = "tracking"
    CARRIER_BID = "carrier_bid"
    PORTAL_REPLY = "portal_reply"
    TARIFF = "tariff"
    IGNORE = "ignore"


class Rfq(BaseModel):
    """What the LLM extracts from a customer RFQ. Pricing reads only these fields."""

    origin: str | None = None  # IATA; None when only a pickup ZIP is given
    pickup_zip: str | None = None  # set = door-to-port
    destination: str  # IATA
    weight_kg: float  # total gross weight
    volume_cbm: float = 0.0  # total volume
    pieces: int | None = None
    preference: Literal["cheapest", "fastest"] = "cheapest"
    ready_date: str | None = None  # ISO date
    language: str = "en"
    customer_name: str | None = None


Source = Literal["sheet", "portal", "bid"]


@dataclass
class RateOption:
    """One priced offer from a sheet, the portal or a carrier bid. Always the same shape."""

    airline: str
    origin: str  # IATA
    destination: str  # IATA
    transit_days: int
    total_minor: int  # all-in, in cents of `currency`
    currency: str  # ISO 4217
    total_eur_minor: int  # for comparing across currencies (rate in fx_eur_per_unit)
    source: Source
    rate_id: str | None = None  # portal only
    valid_until: str | None = None  # ISO 8601 UTC
    chargeable_kg: float = 0.0
    billed_kg: float = 0.0  # > chargeable_kg when billing at a higher weight break is cheaper
    fx_eur_per_unit: str | None = None  # rate used for total_eur_minor when currency != EUR
