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


def _text_model(role: str) -> str:
    """The user's unified model choice supersedes older Railway role overrides.

    An explicitly empty LLM_TEXT_MODEL restores legacy per-role configuration.
    Audio recognition has its own compatible models and does not use this policy.
    """
    unified = os.getenv("LLM_TEXT_MODEL", "gpt-5.6-luna").strip()
    return unified or _env(role, _env("LLM_MODEL", "gpt-5.6-luna"))


_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})


def _flag(name: str, default: bool = False) -> bool:
    """An on/off switch, the one parser for every boolean setting: 1/true/yes/on and
    0/false/no/off (any case) win, an empty or unknown value keeps the default."""
    value = _env(name).lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return default


def _domains(name: str, default: str) -> frozenset[str]:
    """A comma-separated domain list, lower case, without leading dots."""
    raw = _env(name, default)
    return frozenset(d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip())


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


def _speech_model() -> str:
    model = _env("SPEECH_MODEL", "base")
    if model.endswith(".en"):
        logging.getLogger(__name__).warning(
            "Using multilingual %s instead of English-only %s", model[:-3], model
        )
        return model[:-3]
    return model


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
    thread_rescue_enabled: bool
    thread_rescue_max_calls: int
    thread_rescue_after_s: float
    thread_rescue_max_age_s: float
    thread_rescue_max_parallel: int
    thread_rescue_max_questions: int
    thread_rescue_max_mails: int
    thread_rescue_body_chars: int
    case_worker_enabled: bool
    case_worker_max_steps: int
    case_worker_workers: int
    case_worker_max_calls: int
    llm_compose_model: str
    compose_repair: bool
    quote_template_first: bool
    llm_review_model: str
    email_review_enabled: bool
    reply_in_customer_language: bool
    quote_pdf: bool
    quote_pdf_on_request: bool
    poll_interval_s: float
    db_path: str
    ui_port: int
    log_level: str
    inbox_dir: Path
    board_sync_every_s: float
    deadline_check_every_s: float
    ui_user: str
    ui_password: str
    review_token: str
    agents_enabled: bool
    quote_full_score_deadline_s: float
    quote_final_deadline_s: float
    min_bid_carriers: int
    bid_quorum: int
    bid_grace_s: float
    max_bid_carriers: int
    max_trucker_bids: int
    price_adjust_pct: float
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
    clarify_cap_resolve: bool
    revision_not_acceptance: bool
    bid_llm_fallback: bool
    estimate_missing_truck: bool
    cargo_insurance: bool
    consent_llm_check: bool
    all_excluded_notice: bool
    clarify_fallback_after_s: float
    rfq_watchdog: bool
    rfq_watchdog_after_s: float
    rfq_interval_s: float
    deploy_gate_margin_s: float
    llm_max_calls_per_session: int
    runtime_dir: Path
    usd_eur_rate: str
    usd_eur_valid_until: str
    fx_eur_rates: str
    fx_valid_until: str
    use_breakpoints: bool
    portal_429_email_fallback: bool
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
    tracking_notice_check_every_s: float
    reply_workflow_mode: str
    reply_workers: int
    prebook_answer: bool
    phishing_gate_mode: str
    phishing_gate_llm_mode: str
    phishing_gate_model: str
    simulation_static_contacts: bool
    mail_guard_enabled: bool
    inbound_flood_max: int
    inbound_flood_s: float
    outbound_burst_max: int
    outbound_burst_s: float
    outbound_session_max: int
    outbound_duplicate_s: float
    speech_model: str
    speech_model_dir: str
    llm_audio_model: str
    llm_vision_model: str
    attachment_transcription: bool
    groq_api_key: str
    groq_audio_model: str
    audio_timeout_s: float
    local_audio_enabled: bool
    local_audio_model: str
    link_fetch: bool
    link_allowed_domains: frozenset[str]
    link_file_hosts: frozenset[str]

    def __repr__(self) -> str:  # never leak the key into logs
        return f"Settings(base_url={self.base_url!r}, team_mailbox={self.team_mailbox!r})"


settings = Settings(
    build_revision=_env("RAILWAY_GIT_COMMIT_SHA", _env("BUILD_REVISION", "unknown")),
    api_key=_env("TEAM_API_KEY", _env("HACKLOOP_API_KEY")),  # old name still accepted
    base_url=_env("HACKLOOP_BASE_URL", "https://api.start-hack.5u.ai").rstrip("/"),
    team_mailbox=_env("TEAM_MAILBOX", "austrian-power@hackloop.mail").lower(),
    ratedesk_address=_env("RATEDESK_ADDRESS", "ratedesk@hackloop.mail").lower(),
    tracking_address=_env("TRACKING_ADDRESS", "tracking@hackloop.mail").lower(),
    llm_model=_text_model("LLM_MODEL"),
    llm_retry_model=_text_model("LLM_RETRY_MODEL"),
    llm_rescue_model=_text_model("LLM_RESCUE_MODEL"),
    thread_rescue_enabled=_flag("THREAD_RESCUE_ENABLED", True),
    thread_rescue_max_calls=max(1, int(_env("THREAD_RESCUE_MAX_CALLS", "12"))),
    thread_rescue_after_s=max(10.0, float(_env("THREAD_RESCUE_AFTER_S", "45"))),
    # only fresh customer input: older threads are history, not a live workflow (a restart
    # woke ~40 hours-old threads at once, sent mails into them and hit gateway 429s)
    thread_rescue_max_age_s=max(60.0, float(_env("THREAD_RESCUE_MAX_AGE_S", "600"))),
    thread_rescue_max_parallel=max(1, int(_env("THREAD_RESCUE_MAX_PARALLEL", "2"))),
    # questions to one customer before the first quote; then a human takes over (session
    # 38891 asked for the Augsburg ZIP eleven times until the deadline passed)
    thread_rescue_max_questions=max(1, int(_env("THREAD_RESCUE_MAX_QUESTIONS", "3"))),
    # Keep enough conversation to solve unfamiliar requests and accumulated corrections.
    thread_rescue_max_mails=max(2, int(_env("THREAD_RESCUE_MAX_MAILS", "24"))),
    thread_rescue_body_chars=max(500, int(_env("THREAD_RESCUE_BODY_CHARS", "8000"))),
    case_worker_enabled=_flag("CASE_WORKER_ENABLED", False),
    case_worker_max_steps=max(1, min(24, int(_env("CASE_WORKER_MAX_STEPS", "16")))),
    case_worker_workers=max(1, min(4, int(_env("CASE_WORKER_WORKERS", "2")))),
    case_worker_max_calls=max(1, min(32, int(_env("CASE_WORKER_MAX_CALLS", "20")))),
    llm_compose_model=_text_model("LLM_COMPOSE_MODEL"),
    # a second composer call after a failed check doubles the quote's biggest cost; off =
    # the checked deterministic quote copy goes out instead
    compose_repair=_flag("COMPOSE_REPAIR", False),
    # true = the checked template carries the facts and one small call adds a personal
    # opening and answers (cheaper than the full composer); false = full composer
    quote_template_first=_flag("QUOTE_TEMPLATE_FIRST", False),
    llm_review_model=_text_model("LLM_REVIEW_MODEL"),
    email_review_enabled=_flag("EMAIL_REVIEW_ENABLED"),
    reply_in_customer_language=_flag("REPLY_IN_CUSTOMER_LANGUAGE", True),
    # 5U wants the quote as a PDF attachment: built by code from the priced option, no LLM
    quote_pdf=_flag("QUOTE_PDF", True),
    # team decision 22.11.: the PDF goes out only when the customer asks for it
    quote_pdf_on_request=_flag("QUOTE_PDF_ON_REQUEST", True),
    poll_interval_s=float(_env("POLL_INTERVAL_S", "1.5")),
    db_path=_env("DB_PATH", "data/forwarder.db"),
    ui_port=int(_env("PORT", _env("UI_PORT", "8000"))),  # PORT is set by Railway/Render
    log_level=_env("LOG_LEVEL", "INFO"),
    inbox_dir=Path(_env("INBOX_DIR", str(ROOT / "data" / "inbox"))),
    board_sync_every_s=float(_env("BOARD_SYNC_EVERY_S", "15")),
    deadline_check_every_s=float(_env("DEADLINE_CHECK_EVERY_S", "10")),
    ui_user=_env("UI_USER", "team"),
    ui_password=_env("UI_PASSWORD"),
    # read-only bearer token for review agents (GET only), so they never need the team key
    review_token=_env("REVIEW_TOKEN"),
    # false = UI only: no polling, no sending, no catch-up (used while moving the DB online)
    agents_enabled=_flag("AGENTS_ENABLED", True),
    quote_full_score_deadline_s=float(
        _env("QUOTE_FULL_SCORE_DEADLINE_S", _env("QUOTE_TARGET_S", "110"))
    ),
    quote_final_deadline_s=float(_env("QUOTE_FINAL_DEADLINE_S", _env("BID_MAX_WAIT_S", "300"))),
    min_bid_carriers=max(0, int(_env("MIN_BID_CARRIERS", "2"))),
    # bids.wait stops early once BID_QUORUM carriers answered and BID_GRACE_S passed without a
    # further reply; 0 = wait for every carrier as before
    bid_quorum=max(1, int(_env("BID_QUORUM", "2"))),
    bid_grace_s=max(0.0, float(_env("BID_GRACE_S", "0"))),
    max_bid_carriers=max(1, int(_env("MAX_BID_CARRIERS", "5"))),  # airline bid requests
    # truckers get their own slots: in round 20 five truckers left no slot for airline bids
    max_trucker_bids=max(0, int(_env("MAX_TRUCKER_BIDS", "5"))),
    # % taken off the computed all-in total (0 = quote our cost). Experimental lever against
    # too_high; whether 5U also rejects prices far below its best price is not documented.
    price_adjust_pct=min(10.0, max(0.0, float(_env("PRICE_ADJUST_PCT", "0")))),
    allow_incomplete_door_quote=_flag("ALLOW_INCOMPLETE_DOOR_QUOTE"),
    trucker_balance=_trucker_balance(),
    # on by default since 21.11 (fehler-log): rounds 28/35, revision/all-excluded/December edge
    # cases; each still switches off with NAME=false in Railway
    self_delivery_recovery=_flag("SELF_DELIVERY_RECOVERY", True),
    ask_pickup_zip=_flag("ASK_PICKUP_ZIP"),
    clarify_once=_flag("CLARIFY_ONCE"),
    bid_next_break=_flag("BID_NEXT_BREAK"),
    conflict_source_answer=_flag("CONFLICT_SOURCE_ANSWER", True),
    cutoff_question_notice=_flag("CUTOFF_QUESTION_NOTICE", True),
    tariff_on_ready_date=_flag("TARIFF_ON_READY_DATE", True),
    unit_evidence=_flag("UNIT_EVIDENCE", True),
    totals_same_line=_flag("TOTALS_SAME_LINE"),
    thousands_evidence=_flag("THOUSANDS_EVIDENCE", True),
    clarify_cap_resolve=_flag("CLARIFY_CAP_RESOLVE"),
    revision_not_acceptance=_flag("REVISION_NOT_ACCEPTANCE", True),
    # round 63-65: a bid the rules cannot read gets one cheap checked extraction call
    bid_llm_fallback=_flag("BID_LLM_FALLBACK", True),
    # no trucker bid: estimate from earlier bids for the same pickup zone instead of silence
    estimate_missing_truck=_flag("ESTIMATE_MISSING_TRUCK", True),
    # cargo insurance the customer asks for is added to the all-in total (value x cover x rate)
    cargo_insurance=_flag("CARGO_INSURANCE", True),
    # round 60/70: a reply the consent rules cannot decide (or a yes next to a doubt word)
    # is checked by one cheap gateway call; the rules' explicit decline always wins
    consent_llm_check=_flag("CONSENT_LLM_CHECK", True),
    all_excluded_notice=_flag("ALL_EXCLUDED_NOTICE", True),
    # CLARIFY_ONCE: without a usable answer by then, quote from real prices (before 120 s)
    clarify_fallback_after_s=float(_env("CLARIFY_FALLBACK_AFTER_S", "45")),
    # RFQ_WATCHDOG: an RFQ still without our quote this long after it arrived gets the best
    # quote real prices allow (app/agents/watchdog.py); never a second quote, never paused
    rfq_watchdog=_flag("RFQ_WATCHDOG"),
    rfq_watchdog_after_s=max(30.0, float(_env("RFQ_WATCHDOG_AFTER_S", "200"))),
    # deploy gate: RFQs arrive about every 6 min 4 s; the phase is read from real arrivals
    rfq_interval_s=max(60.0, float(_env("RFQ_INTERVAL_S", "364"))),
    deploy_gate_margin_s=max(0.0, float(_env("DEPLOY_GATE_MARGIN_S", "90"))),
    llm_max_calls_per_session=max(1, int(_env("LLM_MAX_CALLS_PER_SESSION", "8"))),
    runtime_dir=Path(
        _env(
            "RUNTIME_DIR",
            str(Path(_env("DB_PATH", "data/forwarder.db")).resolve().parent / "runtime"),
        )
    ),
    usd_eur_rate=_env("USD_EUR_RATE"),
    usd_eur_valid_until=_env("USD_EUR_VALID_UNTIL"),
    # EUR per unit for every foreign currency we accept, e.g. "USD=0.92,JPY=0.0061,GBP=1.17"
    # (app/pricing/fx.py); one shared expiry. Missing = that currency is not converted.
    fx_eur_rates=_env("FX_EUR_RATES"),
    fx_valid_until=_env("FX_VALID_UNTIL"),
    use_breakpoints=_flag("USE_BREAKPOINTS", True),
    portal_429_email_fallback=_flag("PORTAL_429_EMAIL_FALLBACK", True),
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
    followup_enabled=_flag("FOLLOWUP_ENABLED", True),
    followup_after_s=float(_env("FOLLOWUP_AFTER_S", "30")),
    followup_max=max(0, int(_env("FOLLOWUP_MAX", "3"))),
    followup_window_s=float(_env("FOLLOWUP_WINDOW_S", "900")),  # rate_id validity
    followup_check_every_s=max(1.0, float(_env("FOLLOWUP_CHECK_EVERY_S", "2"))),
    tracking_notice_check_every_s=max(5.0, float(_env("TRACKING_NOTICE_CHECK_EVERY_S", "30"))),
    # customer answers our quote with questions: answer them from the stored quote and ask
    # for the go-ahead (app/agents/prebook.py); false = today's generic clarification
    reply_workflow_mode=_env("REPLY_WORKFLOW_MODE", "shadow").lower(),
    reply_workers=max(1, min(16, int(_env("REPLY_WORKERS", "4")))),
    prebook_answer=_flag("PREBOOK_ANSWER"),
    # phishing gate: off | shadow (classify and log, block nothing) | enforce
    phishing_gate_mode=_env("PHISHING_GATE_MODE", "enforce").lower(),
    # what an LLM-only verdict may do; it never does more than PHISHING_GATE_MODE allows.
    # off = no model call: in shadow the verdict can never block, so the call only costs
    phishing_gate_llm_mode=_env("PHISHING_GATE_LLM_MODE", "off").lower(),
    phishing_gate_model=_text_model("PHISHING_GATE_MODEL"),
    # Challenge policy: genuine simulated parties never lose/change their mail identity.
    simulation_static_contacts=_flag("SIMULATION_STATIC_CONTACTS", True),
    # mail guard (app/mail/guard.py): empty/automatic mail, RFQ floods, outgoing mail loops
    mail_guard_enabled=_flag("MAIL_GUARD_ENABLED", True),
    inbound_flood_max=max(1, int(_env("INBOUND_FLOOD_MAX", "3"))),  # new RFQs per sender
    inbound_flood_s=float(_env("INBOUND_FLOOD_S", "600")),  # 5U sends one RFQ per 6 min
    outbound_burst_max=max(1, int(_env("OUTBOUND_BURST_MAX", "6"))),  # mails to one customer
    outbound_burst_s=float(_env("OUTBOUND_BURST_S", "120")),
    outbound_session_max=max(1, int(_env("OUTBOUND_SESSION_MAX", "15"))),  # per thread
    outbound_duplicate_s=float(_env("OUTBOUND_DUPLICATE_S", "1800")),  # same text again
    # Groq first, gateway only if a compatible model is allowed, local Whisper last.
    # Empty audio model auto-selects a gateway audio model; never guess access.
    llm_audio_model=_env("LLM_AUDIO_MODEL"),
    speech_model=_speech_model(),  # multilingual offline fallback
    speech_model_dir=_env("SPEECH_MODEL_DIR"),  # Docker bakes the model into /opt/whisper
    llm_vision_model=_text_model("LLM_VISION_MODEL"),
    # false = no recognition of audio/scans (deterministic text readers stay enabled)
    attachment_transcription=_flag("ATTACHMENT_TRANSCRIPTION", True),
    groq_api_key=_env("GROQ_API_KEY"),
    groq_audio_model=_env("GROQ_AUDIO_MODEL", "whisper-large-v3"),
    audio_timeout_s=max(1.0, float(_env("AUDIO_TIMEOUT_S", "60"))),
    local_audio_enabled=_flag("LOCAL_AUDIO_ENABLED", True),
    local_audio_model=_env("LOCAL_AUDIO_MODEL", _speech_model()).removesuffix(".en"),
    # links in mail (app/mail/links.py): safety-checked, then downloaded like an attachment.
    # false = never open a link. Allowed domains empty = every domain that passes the check.
    link_fetch=_flag("LINK_FETCH", True),
    link_allowed_domains=_domains("LINK_ALLOWED_DOMAINS", ""),
    # links without a file extension are only opened on these file hosts
    link_file_hosts=_domains(
        "LINK_FILE_HOSTS",
        "drive.google.com,docs.google.com,dropbox.com,dropboxusercontent.com,"
        "sharepoint.com,onedrive.live.com,box.com,hackloop.mail,5u.ai",
    ),
)

if settings.rfq_validation_mode not in ("shadow", "enforce"):
    raise ValueError("RFQ_VALIDATION_MODE must be shadow or enforce")
for _name, _mode in (
    ("PHISHING_GATE_MODE", settings.phishing_gate_mode),
    ("PHISHING_GATE_LLM_MODE", settings.phishing_gate_llm_mode),
):
    if _mode not in ("off", "shadow", "enforce"):
        raise ValueError(f"{_name} must be off, shadow or enforce")

if settings.reply_workflow_mode not in ("off", "shadow", "enforce"):
    raise ValueError("REPLY_WORKFLOW_MODE must be off, shadow or enforce")
