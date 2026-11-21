"""Agents that act on routed mail (RFQ, carrier bids)."""

import importlib

# every agent the mail path can reach; tests/test_imports.py keeps this list complete
AGENTS = (
    "bids",
    "booking",
    "carrier_prefs",
    "clarification",
    "email_review",
    "followup",
    "prebook",
    "rfq",
    "tariff",
    "tracking",
    "watchdog",
)


def check_imports() -> tuple[list[str], dict[str, str]]:
    """Import every agent: (loaded names, {name: error}). Never raises, so a broken tracking
    agent is reported at startup without stopping the RFQ agent."""
    loaded: list[str] = []
    failed: dict[str, str] = {}
    for name in AGENTS:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as e:  # noqa: BLE001 - reported by the caller, never fatal
            failed[name] = f"{type(e).__name__}: {e}"
        else:
            loaded.append(name)
    return loaded, failed
