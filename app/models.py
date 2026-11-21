"""Shared data contracts between modules (TECH_SPEC section 6). Field names are binding.

Money is integer cents (`*_minor`, context/code-standards.md: never floats); weights stay float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ICAO -> IATA for the six German origin airports of the pickup zones. US ICAO codes are
# "K" + IATA (KJFK -> JFK) and are handled in _to_iata.
_ICAO_DE = {
    "EDDF": "FRA",
    "EDDM": "MUC",
    "EDDL": "DUS",
    "EDDH": "HAM",
    "EDDS": "STR",
    "EDDK": "CGN",
}


def _to_iata(v: object) -> str | None:
    """A 3-letter IATA code from an IATA or a known ICAO code, else None."""
    code = str(v).strip().upper()
    if len(code) == 3 and code.isalpha():
        return code
    if code in _ICAO_DE:
        return _ICAO_DE[code]
    if len(code) == 4 and code.isalpha() and code.startswith("K"):
        return code[1:]
    return None


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
    """What the LLM extracts from a customer RFQ. Pricing reads only these fields.

    Validated here because it is LLM output (code-standards: validate at the boundary).
    Limits mirror the portal: weight 0 < kg <= 100000, volume 0 <= cbm <= 1000.
    """

    origin: str | None = None  # IATA; None when only a pickup ZIP is given
    pickup_zip: str | None = None  # set = door-to-port
    destination: str  # IATA
    weight_kg: float = Field(gt=0, le=100_000)  # total gross weight
    volume_cbm: float = Field(default=0.0, ge=0, le=1_000)  # total volume
    pieces: int | None = Field(default=None, ge=1)
    preference: Literal["cheapest", "fastest"] = "cheapest"
    ready_date: str | None = None  # ISO date
    language: str = "en"
    customer_name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _origin_fallback(cls, data: object) -> object:
        """LLMs sometimes give an ICAO code (EDDL) for the origin. With a pickup ZIP the origin
        follows from the ZIP anyway, so an unusable origin is dropped instead of failing."""
        if isinstance(data, dict) and data.get("pickup_zip"):
            o = data.get("origin")
            if o is not None and _to_iata(o) is None:
                data = {**data, "origin": None}
        return data

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def _iata(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        code = _to_iata(v)
        if code is None:
            raise ValueError(f"not an IATA airport code: {v!r}")
        return code

    @field_validator("pickup_zip", mode="before")
    @classmethod
    def _zip(cls, v: object) -> object:
        return str(v).strip() or None if v is not None else None


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
    fx_source: str | None = None
    fx_valid_until: str | None = None
    observed_at: str | None = None
    tariff_version: str | None = None
    plain_total_minor: int | None = None
    breakpoint_total_minor: int | None = None
