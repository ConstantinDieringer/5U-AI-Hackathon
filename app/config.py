"""The only place that reads .env. Use `from app.config import settings`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    team_mailbox: str
    ratedesk_address: str
    tracking_address: str
    llm_model: str
    poll_interval_s: float
    db_path: str
    ui_port: int
    log_level: str
    inbox_dir: Path
    carrier_list: Path

    def __repr__(self) -> str:  # never leak the key into logs
        return f"Settings(base_url={self.base_url!r}, team_mailbox={self.team_mailbox!r})"


settings = Settings(
    api_key=_env("TEAM_API_KEY", _env("HACKLOOP_API_KEY")),  # old name still accepted
    base_url=_env("HACKLOOP_BASE_URL", "https://api.start-hack.5u.ai").rstrip("/"),
    team_mailbox=_env("TEAM_MAILBOX", "austrian-power@hackloop.mail").lower(),
    ratedesk_address=_env("RATEDESK_ADDRESS", "ratedesk@hackloop.mail").lower(),
    tracking_address=_env("TRACKING_ADDRESS", "tracking@hackloop.mail").lower(),
    llm_model=_env("LLM_MODEL"),
    poll_interval_s=float(_env("POLL_INTERVAL_S", "1.5")),
    db_path=_env("DB_PATH", "data/forwarder.db"),
    ui_port=int(_env("UI_PORT", "8000")),
    log_level=_env("LOG_LEVEL", "INFO"),
    inbox_dir=ROOT / "data" / "inbox",
    carrier_list=ROOT / "data" / "start-kit" / "carrier-list.csv",
)
