"""Load the bundled customer writing profile shared with the repository skill."""

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def customer_voice() -> str:
    """Trusted local instructions only; no customer input or runtime network lookup."""
    return Path(__file__).with_name("customer_voice.md").read_text(encoding="utf-8").strip()
