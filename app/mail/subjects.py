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
    if language == "nl":
        return f"Luchtvracht {route[0]} naar {route[1]}"
    if language == "pl":
        return f"Fracht lotniczy {route[0]} do {route[1]}"
    if language == "tr":
        return f"Hava kargo {route[0]} çıkışlı {route[1]} varışlı"
    return f"Air freight {route[0]} to {route[1]}"
