"""The only place that reads .env. Use `from app.config import settings`."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _flag(name: str) -> bool:
    """An on/off switch; only "true" switches on, anything else keeps today's behaviour."""
    return _env(name, "false").lower() == "true"


TRUCKER_BALANCES = ("minmax", "ratio")


def _trucker_balance() -> str:
    """How the 50/50 price/speed balance picks among carrier bids (truckers and airlines).
    Default `ratio` (board fit, see pricing.most_balanced); `minmax` is the old rule. An
    unknown value falls back to `ratio` with a warning: a typo must never stop the start."""
    mode = _env("TRUCKER_BALANCE", "ratio").lower()
    if mode not in TRUCKER_BALANCES:
        logging.getLogger(__name__).warning("TRUCKER_BALANCE=%r unknown, using ratio", mode)
        return "ratio"
    return mode


@dataclass(frozen=True)
class Settings:
    build_revision: str
    api_key: str
    base_url: str
    team_mailbox: str
    ratedesk_address: str
    tracking_address: str
    llm_model: str
    llm_retry_model: str
    llm_rescue_model: str
    llm_compose_model: str
    llm_review_model: str
    email_review_enabled: bool
    poll_interval_s: float
    db_path: str
    ui_port: int
    log_level: str
    inbox_dir: Path
    carrier_list: Path
    board_sync_every_s: float
    deadline_check_every_s: float
    ui_user: str
    ui_password: str
    agents_enabled: bool
    quote_target_s: float
    bid_max_wait_s: float
    quote_full_score_deadline_s: float
    quote_final_deadline_s: float
    min_bid_carriers: int
    max_bid_carriers: int
    max_trucker_bids: int
    price_adjust_pct: float
    carrier_observation_ttl_s: float
    allow_incomplete_door_quote: bool
    trucker_balance: str
    self_delivery_recovery: bool
    ask_pickup_zip: bool
    clarify_once: bool
    bid_next_break: bool
    conflict_source_answer: bool
    cutoff_question_notice: bool
    tariff_on_ready_date: bool
    unit_evidence: bool
    totals_same_line: bool
    thousands_evidence: bool
    revision_not_acceptance: bool
    all_excluded_notice: bool
    clarify_fallback_after_s: float
    llm_max_calls_per_session: int
    runtime_dir: Path
    usd_eur_rate: str
    usd_eur_valid_until: str
    use_breakpoints: bool
    served_lanes_ttl_s: float
    rfq_validation_mode: str
    webhook_secret: str
    webhook_secret_file: Path
    webhook_poll_interval_s: float
    price_change_tolerance_pct: float
    airline_confirm_wait_s: float
    followup_enabled: bool
    followup_after_s: float
    followup_max: int
    followup_window_s: float
    followup_check_every_s: float
    phishing_gate_mode: str
    phishing_gate_llm_mode: str
    phishing_gate_model: str

    def __repr__(self) -> str:  # never leak the key into logs
        return f"Settings(base_url={self.base_url!r}, team_mailbox={self.team_mailbox!r})"


settings = Settings(
    build_revision=_env("RAILWAY_GIT_COMMIT_SHA", _env("BUILD_REVISION", "unknown")),
    api_key=_env("TEAM_API_KEY", _env("HACKLOOP_API_KEY")),  # old name still accepted
    base_url=_env("HACKLOOP_BASE_URL", "https://api.start-hack.5u.ai").rstrip("/"),
    team_mailbox=_env("TEAM_MAILBOX", "austrian-power@hackloop.mail").lower(),
    ratedesk_address=_env("RATEDESK_ADDRESS", "ratedesk@hackloop.mail").lower(),
    tracking_address=_env("TRACKING_ADDRESS", "tracking@hackloop.mail").lower(),
    llm_model=_env("LLM_MODEL"),
    llm_retry_model=_env("LLM_RETRY_MODEL", "gpt-5"),  # stronger repair when extraction fails
    llm_rescue_model=_env("LLM_RESCUE_MODEL", "gpt-5"),
    llm_compose_model=_env("LLM_COMPOSE_MODEL", "gpt-5-mini"),
    llm_review_model=_env("LLM_REVIEW_MODEL", "gpt-5"),
    email_review_enabled=_env("EMAIL_REVIEW_ENABLED", "true").lower() == "true",
    poll_interval_s=float(_env("POLL_INTERVAL_S", "1.5")),
    db_path=_env("DB_PATH", "data/forwarder.db"),
    ui_port=int(_env("PORT", _env("UI_PORT", "8000"))),  # PORT is set by Railway/Render
    log_level=_env("LOG_LEVEL", "INFO"),
    inbox_dir=Path(_env("INBOX_DIR", str(ROOT / "data" / "inbox"))),
    carrier_list=ROOT / "data" / "start-kit" / "carrier-list.csv",
    board_sync_every_s=float(_env("BOARD_SYNC_EVERY_S", "15")),
    deadline_check_every_s=float(_env("DEADLINE_CHECK_EVERY_S", "10")),
    ui_user=_env("UI_USER", "team"),
    ui_password=_env("UI_PASSWORD"),
    # false = UI only: no polling, no sending, no catch-up (used while moving the DB online)
    agents_enabled=_env("AGENTS_ENABLED", "true").lower() not in ("0", "false", "no", "off"),
    quote_target_s=float(_env("QUOTE_TARGET_S", "110")),
    bid_max_wait_s=float(_env("BID_MAX_WAIT_S", "300")),
    quote_full_score_deadline_s=float(
        _env("QUOTE_FULL_SCORE_DEADLINE_S", _env("QUOTE_TARGET_S", "110"))
    ),
    quote_final_deadline_s=float(_env("QUOTE_FINAL_DEADLINE_S", _env("BID_MAX_WAIT_S", "300"))),
    min_bid_carriers=max(0, int(_env("MIN_BID_CARRIERS", "2"))),
    max_bid_carriers=max(1, int(_env("MAX_BID_CARRIERS", "5"))),  # airline bid requests
    # truckers get their own slots: in round 20 five truckers left no slot for airline bids
    max_trucker_bids=max(0, int(_env("MAX_TRUCKER_BIDS", "5"))),
    # % taken off the computed all-in total (0 = quote our cost). Experimental lever against
    # too_high; whether 5U also rejects prices far below its best price is not documented.
    price_adjust_pct=min(10.0, max(0.0, float(_env("PRICE_ADJUST_PCT", "0")))),
    carrier_observation_ttl_s=float(_env("CARRIER_OBSERVATION_TTL_S", "3600")),
    allow_incomplete_door_quote=(_env("ALLOW_INCOMPLETE_DOOR_QUOTE", "false").lower() == "true"),
    trucker_balance=_trucker_balance(),
    self_delivery_recovery=_flag("SELF_DELIVERY_RECOVERY"),
    ask_pickup_zip=_flag("ASK_PICKUP_ZIP"),
    clarify_once=_flag("CLARIFY_ONCE"),
    bid_next_break=_flag("BID_NEXT_BREAK"),
    conflict_source_answer=_flag("CONFLICT_SOURCE_ANSWER"),
    cutoff_question_notice=_flag("CUTOFF_QUESTION_NOTICE"),
    tariff_on_ready_date=_flag("TARIFF_ON_READY_DATE"),
    unit_evidence=_flag("UNIT_EVIDENCE"),
    totals_same_line=_flag("TOTALS_SAME_LINE"),
    thousands_evidence=_flag("THOUSANDS_EVIDENCE"),
    revision_not_acceptance=_flag("REVISION_NOT_ACCEPTANCE"),
    all_excluded_notice=_flag("ALL_EXCLUDED_NOTICE"),
    # CLARIFY_ONCE: without a usable answer by then, quote from real prices (before 120 s)
    clarify_fallback_after_s=float(_env("CLARIFY_FALLBACK_AFTER_S", "45")),
    llm_max_calls_per_session=max(1, int(_env("LLM_MAX_CALLS_PER_SESSION", "8"))),
    runtime_dir=Path(
        _env(
            "RUNTIME_DIR",
            str(Path(_env("DB_PATH", "data/forwarder.db")).resolve().parent / "runtime"),
        )
    ),
    usd_eur_rate=_env("USD_EUR_RATE"),
    usd_eur_valid_until=_env("USD_EUR_VALID_UNTIL"),
    use_breakpoints=_env("USE_BREAKPOINTS", "true").lower() == "true",
    served_lanes_ttl_s=float(_env("CARRIER_OBSERVATION_TTL_S", _env("SERVED_LANES_TTL_S", "3600"))),
    rfq_validation_mode=_env("RFQ_VALIDATION_MODE", "enforce"),
    webhook_secret=_env("MAILBOX_WEBHOOK_SECRET"),
    webhook_secret_file=Path(
        _env(
            "MAILBOX_WEBHOOK_SECRET_FILE",
            str(
                Path(_env("DB_PATH", "data/forwarder.db")).resolve().parent
                / "runtime"
                / "mailbox-webhook.secret"
            ),
        )
    ),
    webhook_poll_interval_s=max(1, float(_env("MAILBOX_WEBHOOK_POLL_INTERVAL_S", "30"))),
    # booking: an airline price this far (%) from the quoted air price counts as a change and
    # is put to the customer before we confirm (rounds 49/50: +10 % at confirmation, -22.5)
    price_change_tolerance_pct=max(0.0, float(_env("PRICE_CHANGE_TOLERANCE_PCT", "0.5"))),
    # wait this long for the airline's confirmation mail (with its price) before confirming
    # the booking to the customer anyway; 0 confirms right after booking as before
    airline_confirm_wait_s=max(0.0, float(_env("AIRLINE_CONFIRM_WAIT_S", "180"))),
    # customer silent after our quote -> nudge in the thread (app/agents/followup.py)
    followup_enabled=_env("FOLLOWUP_ENABLED", "true").lower() not in ("0", "false", "no", "off"),
    followup_after_s=float(_env("FOLLOWUP_AFTER_S", "30")),
    followup_max=max(0, int(_env("FOLLOWUP_MAX", "3"))),
    followup_window_s=float(_env("FOLLOWUP_WINDOW_S", "900")),  # rate_id validity
    followup_check_every_s=max(1.0, float(_env("FOLLOWUP_CHECK_EVERY_S", "2"))),
    # phishing gate: off | shadow (classify and log, block nothing) | enforce
    phishing_gate_mode=_env("PHISHING_GATE_MODE", "enforce").lower(),
    # what an LLM-only verdict may do; it never does more than PHISHING_GATE_MODE allows
    phishing_gate_llm_mode=_env("PHISHING_GATE_LLM_MODE", "shadow").lower(),
    phishing_gate_model=_env("PHISHING_GATE_MODEL", "gpt-5-nano"),
)

if settings.rfq_validation_mode not in ("shadow", "enforce"):
    raise ValueError("RFQ_VALIDATION_MODE must be shadow or enforce")
for _name, _mode in (
    ("PHISHING_GATE_MODE", settings.phishing_gate_mode),
    ("PHISHING_GATE_LLM_MODE", settings.phishing_gate_llm_mode),
):
    if _mode not in ("off", "shadow", "enforce"):
        raise ValueError(f"{_name} must be off, shadow or enforce")
