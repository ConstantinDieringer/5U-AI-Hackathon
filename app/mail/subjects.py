"""Customer subjects from verified shipment facts, never forwarded mail history."""

import re


def shipment_subject(
    origin: str | None, destination: str | None, language: str | None, fallback: str
) -> str:
    """Keep the existing subject until both structured airport codes are known."""
    route = [(code or "").strip().upper() for code in (origin, destination)]
    if not all(re.fullmatch(r"[A-Z]{3}", code) for code in route):
        return fallback
    if (language or "").lower().startswith("de"):
        return f"Luftfracht {route[0]} nach {route[1]}"
    language = (language or "en").lower().split("-")[0]
    if language == "fr":
        return f"Fret aérien {route[0]} vers {route[1]}"
    if language == "es":
        return f"Carga aérea {route[0]} a {route[1]}"
    if language == "it":
        return f"Trasporto aereo {route[0]} a {route[1]}"
    return f"Air freight {route[0]} to {route[1]}"
