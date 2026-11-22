"""Explicit quote selection. Scoring, current offers and bookings have different owners."""

from typing import Any


def first(session: dict[str, Any]) -> dict[str, Any] | None:
    return next(iter(session.get("quotes") or []), None)


def active(session: dict[str, Any]) -> dict[str, Any] | None:
    workflow = session.get("workflow")
    if workflow is None:
        return first(session)
    return next(
        (q for q in session.get("quotes", []) if q["id"] == workflow["active_quote_id"]), None
    )


def booked(session: dict[str, Any]) -> dict[str, Any] | None:
    bookings = session.get("bookings") or []
    quote_id = bookings[0].get("quote_id") if bookings else None
    if quote_id is not None:
        return next((q for q in session.get("quotes", []) if q["id"] == quote_id), None)
    return first(session)


def operational(session: dict[str, Any]) -> dict[str, Any] | None:
    return (
        booked(session)
        if any(b.get("status") in ("booked", "confirmed") for b in session.get("bookings", []))
        else active(session)
    )
