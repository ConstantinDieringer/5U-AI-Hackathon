"""CRM storage: the only module that talks SQL.

Design: context/docs/design/aufgabe-c.md (sections 3-5).

Rules
- One connection per thread (threading.local), autocommit mode (isolation_level=None).
- Every write runs inside tx(), which starts with BEGIN IMMEDIATE. A transaction that
  reads and then writes without it fails at once with "database is locked".
- The event for a state change is written in the same transaction as the change.
- No network, mail or LLM calls inside a transaction.
- Money is stored as integer minor units (*_minor), LLM cost as micro-USD (*_micro_usd).
- All timestamps are ISO-8601 UTC strings generated in Python.

Public functions take plain keyword arguments, so callers do not depend on the
mailbox field names (still to be confirmed in context/docs/mail-format.md).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

REPLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_revisions (
 session_id INTEGER NOT NULL REFERENCES sessions(id), version INTEGER NOT NULL,
 source_mail_id INTEGER NOT NULL, request_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(session_id,version), UNIQUE(session_id,source_mail_id)
);
CREATE TABLE IF NOT EXISTS thread_workflow (
 session_id INTEGER PRIMARY KEY REFERENCES sessions(id), request_version INTEGER NOT NULL,
 active_quote_id INTEGER, phase TEXT NOT NULL, last_mail_id INTEGER NOT NULL,
 open_json TEXT NOT NULL DEFAULT '[]', approval_json TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_decisions (
 session_id INTEGER NOT NULL, mail_id INTEGER NOT NULL, request_version INTEGER NOT NULL,
 decision_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(session_id,mail_id)
);
CREATE TABLE IF NOT EXISTS workflow_actions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id),
 mail_id INTEGER NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'pending', payload_json TEXT, result_json TEXT,
 lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL,
 UNIQUE(session_id,mail_id,kind,version)
);
CREATE INDEX IF NOT EXISTS ix_workflow_pending ON workflow_actions(status,id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_workflow_thread_owner ON workflow_actions(session_id)
 WHERE kind='reply' AND status='processing';
"""

CASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS case_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id),
 mail_id INTEGER NOT NULL UNIQUE, version INTEGER NOT NULL, status TEXT NOT NULL,
 workspace_json TEXT NOT NULL DEFAULT '{}', lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
 steps INTEGER NOT NULL DEFAULT 0, llm_calls INTEGER NOT NULL DEFAULT 0,
 wake_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_case_pending ON case_jobs(status,id);
CREATE TABLE IF NOT EXISTS case_steps (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES case_jobs(id),
 signature TEXT NOT NULL, tool TEXT NOT NULL, arguments_json TEXT NOT NULL,
 status TEXT NOT NULL, result_json TEXT, updated_at TEXT NOT NULL,
 UNIQUE(job_id,signature)
);
"""


def case_latest(sid: int) -> dict[str, Any] | None:
    row = _one(
        conn().execute(
            "SELECT * FROM case_jobs WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)
        )
    )
    if row:
        row["workspace"] = json.loads(row.pop("workspace_json"))
        row.pop("lease_owner", None)
    return row


def case_owns(sid: int) -> bool:
    # Enrollment is durable: changing the rollout flag cannot resume a legacy sender
    # around an unresolved requirement or an uncertain external operation.
    return case_latest(sid) is not None


def case_enqueue(sid: int, mid: int) -> None:
    with tx() as c:
        previous = _one(
            c.execute("SELECT * FROM case_jobs WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,))
        )
        if c.execute("SELECT 1 FROM case_jobs WHERE mail_id=?", (mid,)).fetchone():
            return
        workspace = json.loads(previous["workspace_json"]) if previous else {}
        # Every new customer input invalidates calculated prices and prepared documents.
        # Keep evidence and outstanding obligations; the model must explicitly supersede
        # an obsolete obligation with a citation from the new customer message.
        for key in ("shipments", "prices", "artifacts", "last_audit"):
            workspace[key] = {}
        c.execute(
            "UPDATE case_jobs SET status='superseded' WHERE session_id=? "
            "AND status IN ('pending','waiting','running')",
            (sid,),
        )
        c.execute(
            "INSERT INTO case_jobs(session_id,mail_id,version,status,workspace_json,created_at,updated_at) "
            "VALUES (?,?,?,'pending',?,?,?)",
            (
                sid,
                mid,
                (previous["version"] + 1) if previous else 1,
                _json(workspace),
                now(),
                now(),
            ),
        )
        _emit(c, "case_queued", "Fallbearbeitung eingeplant", sid, payload={"mail_id": mid})


def case_wake(sid: int) -> None:
    with tx() as c:
        c.execute(
            "UPDATE case_jobs SET status=CASE WHEN status='waiting' THEN 'pending' ELSE status END,"
            "wake_requested=1,updated_at=? WHERE id="
            "(SELECT MAX(id) FROM case_jobs WHERE session_id=?) AND status IN ('waiting','running')",
            (now(), sid),
        )


def case_wake_mail(mid: int) -> None:
    row = _one(conn().execute("SELECT session_id FROM mails WHERE id=?", (mid,)))
    if row and row["session_id"] is not None:
        case_wake(int(row["session_id"]))


def case_claim(owner: str, at: float) -> dict[str, Any] | None:
    with tx() as c:
        expired = _rows(
            c.execute(
                "SELECT id,session_id FROM case_jobs WHERE status IN ('running','superseded') "
                "AND lease_until<? AND (status='running' OR EXISTS "
                "(SELECT 1 FROM case_steps e WHERE e.job_id=case_jobs.id AND e.status='dispatching'))",
                (at,),
            )
        )
        for row in expired:
            uncertain = c.execute(
                "SELECT 1 FROM case_steps WHERE job_id=? AND status='dispatching'", (row["id"],)
            ).fetchone()
            state = "unknown" if uncertain else "pending"
            c.execute(
                "UPDATE case_jobs SET status=?,lease_owner=NULL WHERE id=?", (state, row["id"])
            )
            if uncertain:
                _emit(
                    c,
                    "case_unknown",
                    "Externer Ausgang unklar, keine Wiederholung",
                    row["session_id"],
                    level="error",
                )
        row = _one(
            c.execute(
                "SELECT j.* FROM case_jobs j JOIN sessions s ON s.id=j.session_id "
                "WHERE j.status='pending' AND s.paused=0 AND s.mode='auto' "
                "AND NOT EXISTS (SELECT 1 FROM case_jobs b WHERE b.session_id=j.session_id "
                "AND (b.status='unknown' OR (b.status='running' AND b.lease_until>?))) "
                "AND NOT EXISTS (SELECT 1 FROM case_steps e JOIN case_jobs b ON b.id=e.job_id "
                "WHERE b.session_id=j.session_id AND e.status='dispatching') "
                "ORDER BY j.id LIMIT 1",
                (at,),
            )
        )
        if row and not is_global_paused():
            c.execute(
                "UPDATE case_jobs SET status='running',lease_owner=?,lease_until=? WHERE id=?",
                (owner, at + 90, row["id"]),
            )
            row["workspace"] = json.loads(row.pop("workspace_json"))
            return row
    return None


def case_current(job: int, owner: str, at: float) -> bool:
    row = _one(
        conn().execute(
            "SELECT j.session_id,j.mail_id FROM case_jobs j JOIN sessions s ON s.id=j.session_id "
            "WHERE j.id=? AND j.status='running' AND j.lease_owner=? AND j.lease_until>? "
            "AND s.paused=0 AND s.mode='auto' "
            "AND NOT EXISTS (SELECT 1 FROM mails m WHERE m.session_id=j.session_id "
            "AND m.direction='in' AND (lower(m.sender)=lower(s.customer) OR m.category IN ('rfq','customer','tracking')) "
            "AND m.id>j.mail_id) "
            "AND NOT EXISTS (SELECT 1 FROM case_jobs n WHERE n.session_id=j.session_id AND n.id>j.id)",
            (job, owner, at),
        )
    )
    return bool(row) and not is_global_paused()


def case_heartbeat(job: int, owner: str, at: float) -> bool:
    with tx() as c:
        return (
            c.execute(
                "UPDATE case_jobs SET lease_until=? WHERE id=? AND lease_owner=? AND status='running'",
                (at + 90, job, owner),
            ).rowcount
            == 1
        )


def case_save(job: int, owner: str, workspace: dict, status: str = "running") -> bool:
    with tx() as c:
        return (
            c.execute(
                "UPDATE case_jobs SET workspace_json=?,status=CASE WHEN ?='waiting' AND wake_requested=1 "
                "THEN 'pending' ELSE ? END,wake_requested=CASE WHEN ?='waiting' THEN 0 ELSE wake_requested END,"
                "updated_at=? WHERE id=? AND lease_owner=? AND status='running'",
                (_json(workspace), status, status, status, now(), job, owner),
            ).rowcount
            == 1
        )


def case_spend_step(job: int, owner: str, maximum: int) -> bool:
    with tx() as c:
        return (
            c.execute(
                "UPDATE case_jobs SET steps=steps+1 WHERE id=? AND lease_owner=? "
                "AND status='running' AND steps<?",
                (job, owner, maximum),
            ).rowcount
            == 1
        )


def reserve_case_call(sid: int, job: int, maximum: int) -> bool:
    with tx() as c:
        return (
            c.execute(
                "UPDATE case_jobs SET llm_calls=llm_calls+1 WHERE id=? AND session_id=? "
                "AND status='running' AND llm_calls<?",
                (job, sid, maximum),
            ).rowcount
            == 1
        )


def case_step(job: int, signature: str, tool: str, arguments: str) -> dict:
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO case_steps(job_id,signature,tool,arguments_json,status,updated_at) "
            "VALUES (?,?,?,?,'prepared',?)",
            (job, signature, tool, arguments, now()),
        )
        row = _one(
            c.execute("SELECT * FROM case_steps WHERE job_id=? AND signature=?", (job, signature))
        )
        assert row is not None
        row["result"] = json.loads(row.pop("result_json") or "null")
        return row


def case_step_state(step: int, state: str, result: dict | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE case_steps SET status=?,result_json=?,updated_at=? WHERE id=?",
            (state, _json(result), now(), step),
        )


def case_steps(job: int) -> list[dict]:
    rows = _rows(conn().execute("SELECT * FROM case_steps WHERE job_id=? ORDER BY id", (job,)))
    for row in rows:
        row["result"] = json.loads(row.pop("result_json") or "null")
    return rows


def case_set_context(job: int, owner: str, step: int | None = None) -> None:
    _local.case_context = (job, owner, step)


def case_clear_context() -> None:
    _local.case_context = None


def case_transport_guard() -> None:
    """Fail closed immediately before transport, including delegated business tools."""
    import time

    context = getattr(_local, "case_context", None)
    if context is None:
        return
    job, owner, step = context
    if not case_current(job, owner, time.time()):
        raise RuntimeError("Case input, ownership or human control changed before transport")
    if step is not None:
        case_step_state(step, "dispatching")


DEADLINE = timedelta(minutes=6)
ATTENTION_WINDOW_S = 90

# Categories that open a customer session when no known mail is referenced.
SESSION_CATEGORIES = {"rfq": "rfq", "customer": "rfq", "tracking": "tracking"}

SESSION_STATUSES = (
    "new",
    "pricing",
    "quoted",
    "accepted",
    "booked",
    "confirmed",
    "answered",
    "lost",
    "expired",
    "error",
)
OPEN_STATUSES = ("new", "pricing")


# Event kinds (fixed catalogue, see design section 4)
class Ev:
    MAIL_IN = "mail_in"
    MAIL_OUT = "mail_out"
    ROUTED = "routed"
    EXTRACTED = "extracted"
    PRICED = "priced"
    BID_REQUESTED = "bid_requested"
    BID_RECEIVED = "bid_received"
    LLM_CALL = "llm_call"
    QUOTED = "quoted"
    BOOKED = "booked"
    STATUS = "status"
    HUMAN_ACTION = "human_action"
    BOARD_VERDICT = "board_verdict"
    WARN = "warn"
    ERROR = "error"


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sessions (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL DEFAULT 'rfq' CHECK (kind IN ('rfq','tracking')),
  customer       TEXT,
  subject        TEXT,
  status         TEXT NOT NULL DEFAULT 'new' CHECK (status IN {SESSION_STATUSES!r}),
  paused         INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  mode           TEXT NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto','manual')),
  request_json   TEXT CHECK (request_json IS NULL OR json_valid(request_json)),
  deadline_at    TEXT,
  first_reply_at TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS ix_sessions_created ON sessions(created_at);

CREATE TABLE IF NOT EXISTS mails (
  id           INTEGER PRIMARY KEY,
  session_id   INTEGER REFERENCES sessions(id),
  thread_root  INTEGER NOT NULL,
  in_reply_to  INTEGER,
  direction    TEXT NOT NULL CHECK (direction IN ('in','out')),
  sender       TEXT,
  recipient    TEXT,
  subject      TEXT,
  body         TEXT,
  category     TEXT,
  received_at  TEXT NOT NULL,
  raw_json     TEXT CHECK (raw_json IS NULL OR json_valid(raw_json))
);
CREATE INDEX IF NOT EXISTS ix_mails_session ON mails(session_id, received_at);
-- category first: background loops filter by category (+direction, +session); an index led
-- by direction alone would pull direction-only queries off the cheaper table scan
CREATE INDEX IF NOT EXISTS ix_mails_category ON mails(category, direction, session_id);
CREATE INDEX IF NOT EXISTS ix_mails_reply ON mails(in_reply_to);

CREATE TABLE IF NOT EXISTS quotes (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id        INTEGER NOT NULL REFERENCES sessions(id),
  airline           TEXT,
  origin            TEXT,
  destination       TEXT,
  total_minor       INTEGER NOT NULL,
  currency          TEXT NOT NULL,
  air_total_minor   INTEGER,
  air_currency      TEXT,
  insurance_minor   INTEGER NOT NULL DEFAULT 0 CHECK (insurance_minor >= 0),
  transit_days      INTEGER,
  source            TEXT CHECK (source IN ('sheet','portal','bid','manual')),
  rate_id           TEXT,
  valid_until       TEXT,
  alternatives_json TEXT CHECK (alternatives_json IS NULL OR json_valid(alternatives_json)),
  mail_id           INTEGER,
  sent_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quotes_session ON quotes(session_id);

CREATE TABLE IF NOT EXISTS bookings (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id        INTEGER NOT NULL REFERENCES sessions(id),
  team_reference    TEXT NOT NULL UNIQUE,
  booking_reference TEXT,
  rate_id           TEXT,
  airline           TEXT,
  price_minor       INTEGER,
  currency          TEXT,
  status            TEXT NOT NULL CHECK (status IN ('booked','confirmed','failed')),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id        INTEGER,
  purpose           TEXT,
  model             TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  cost_micro_usd    INTEGER NOT NULL DEFAULT 0,
  latency_ms        INTEGER,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_session ON llm_calls(session_id);

CREATE TABLE IF NOT EXISTS events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id     INTEGER,
  kind           TEXT NOT NULL,
  level          TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info','warn','error')),
  message        TEXT,
  duration_ms    INTEGER,
  cost_micro_usd INTEGER,
  payload_json   TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, id);
CREATE INDEX IF NOT EXISTS ix_events_level ON events(level, id);

CREATE TABLE IF NOT EXISTS board_results (
  round_id    TEXT PRIMARY KEY,
  session_id  INTEGER,
  verdict     TEXT,
  reason      TEXT,
  points_json TEXT,
  raw_json    TEXT,
  fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customer_carrier_prefs (
  customer   TEXT NOT NULL,
  carrier    TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('exclude','prefer')),
  session_id INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY (customer, carrier)
);
CREATE TABLE IF NOT EXISTS mail_delivery (
 mail_id INTEGER PRIMARY KEY, raw_json TEXT NOT NULL, source TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mail_delivery_pending ON mail_delivery(status, retry_at, mail_id);
CREATE TABLE IF NOT EXISTS rfq_validation (
 session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
 state TEXT NOT NULL,
 draft_json TEXT NOT NULL,
 root_json TEXT NOT NULL,
 send_state TEXT NOT NULL DEFAULT 'pending',
 sent_mail_id INTEGER,
 reply_mail_id INTEGER,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clarification_answers (
 session_id INTEGER NOT NULL REFERENCES sessions(id),
 mail_id INTEGER NOT NULL,
 PRIMARY KEY(session_id, mail_id)
);
CREATE TABLE IF NOT EXISTS thread_rescue_runs (
 session_id INTEGER NOT NULL REFERENCES sessions(id),
 input_key TEXT NOT NULL,
 state TEXT NOT NULL,
 decision_json TEXT,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(session_id,input_key)
);
CREATE TABLE IF NOT EXISTS tariff_jobs (
  mail_id INTEGER PRIMARY KEY,
  raw_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT
);
CREATE TABLE IF NOT EXISTS mail_screening (
  mail_id      INTEGER PRIMARY KEY,
  sender       TEXT NOT NULL,
  thread_root  INTEGER NOT NULL,
  verdict      TEXT NOT NULL CHECK (verdict IN ('genuine','phishing','unsure')),
  layer        TEXT NOT NULL,
  score        REAL NOT NULL,
  blocked      INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0,1)),
  routed_as    TEXT NOT NULL,
  reasons_json TEXT NOT NULL CHECK (json_valid(reasons_json)),
  detail_json  TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
  released_at  TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mail_screening_thread ON mail_screening(thread_root, sender);
CREATE TABLE IF NOT EXISTS address_changes (  -- a verified party announced its new address
  new_address  TEXT PRIMARY KEY,
  old_address  TEXT NOT NULL,   -- the original verified address (carrier list, desk, customer)
  mail_id      INTEGER NOT NULL,
  created_at   TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- connection

_local = threading.local()
_db_path: str = settings.db_path  # from .env via app/config.py (default data/forwarder.db)
_generation = 0  # bumped by init_db so threads reopen after a path change (tests)
_initialised: str | None = None  # path init_db last completed for in this process


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def conn() -> sqlite3.Connection:
    c: sqlite3.Connection | None = getattr(_local, "c", None)
    if c is None or getattr(_local, "gen", -1) != _generation:
        if c is not None:
            c.close()
        c = sqlite3.connect(_db_path, timeout=5, isolation_level=None, check_same_thread=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c, _local.gen = c, _generation
    return c


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException as e:
        c.execute("ROLLBACK")
        if isinstance(e, sqlite3.OperationalError):
            log.error("sqlite error %s: %s", getattr(e, "sqlite_errorname", "?"), e)
        raise


def close_thread_connection() -> None:
    """Release this thread's SQLite handle before removing a temporary database."""
    connection = getattr(_local, "c", None)
    if connection is not None:
        connection.close()
        _local.c = None


def init_db(path: str | None = None) -> None:
    """Create the database file and schema. Call once at startup (and per test).

    Startup reaches this from several entry points (run.py, the poller's store, the UI
    lifespan). A repeat call without a path is a no-op once this process has initialised
    the current file; an explicit path always initialises in full (tests, import)."""
    global _db_path, _generation, _initialised
    if path is None and _initialised == _db_path and os.path.exists(_db_path):
        return
    path_changed = path is not None
    if path is not None:
        _db_path = path
        _generation += 1
    d = os.path.dirname(_db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    c = conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.executescript(REPLY_SCHEMA)
    c.executescript(CASE_SCHEMA)
    with tx() as migration:
        columns = {row["name"] for row in migration.execute("PRAGMA table_info(llm_calls)")}
        if "accounting_json" not in columns:
            migration.execute("ALTER TABLE llm_calls ADD COLUMN accounting_json TEXT")
        quote_columns = {row["name"] for row in migration.execute("PRAGMA table_info(quotes)")}
        if "air_total_minor" not in quote_columns:
            migration.execute("ALTER TABLE quotes ADD COLUMN air_total_minor INTEGER")
        if "air_currency" not in quote_columns:
            migration.execute("ALTER TABLE quotes ADD COLUMN air_currency TEXT")
        for column, kind in (
            ("request_version", "INTEGER"),
            ("request_snapshot_json", "TEXT"),
            ("pricing_snapshot_json", "TEXT"),
            ("supersedes_id", "INTEGER"),
        ):
            if column not in quote_columns:
                migration.execute(f"ALTER TABLE quotes ADD COLUMN {column} {kind}")
        if "insurance_minor" not in quote_columns:
            migration.execute(
                "ALTER TABLE quotes ADD COLUMN insurance_minor INTEGER NOT NULL DEFAULT 0"
            )
        booking_columns = {row["name"] for row in migration.execute("PRAGMA table_info(bookings)")}
        if "quote_id" not in booking_columns:
            migration.execute("ALTER TABLE bookings ADD COLUMN quote_id INTEGER")
        # price the airline actually confirmed, and where the customer side of the booking
        # stands (see booking.py: awaiting_airline, confirmed_sent, price_change_asked, ...)
        for column, kind in (
            ("airline_price_minor", "INTEGER"),
            ("airline_currency", "TEXT"),
            ("customer_state", "TEXT"),
            ("customer_total_minor", "INTEGER"),
        ):
            if column not in booking_columns:
                migration.execute(f"ALTER TABLE bookings ADD COLUMN {column} {kind}")
        board_columns = {
            row["name"] for row in migration.execute("PRAGMA table_info(board_results)")
        }
        if "cause_json" not in board_columns:
            # why a round got no answer, frozen when the board first reports it
            migration.execute("ALTER TABLE board_results ADD COLUMN cause_json TEXT")
    remap_unassigned()
    backfill_bookings_from_confirmations()
    _initialised = _db_path
    if path_changed:
        # A database path can later be replaced by the UI import endpoint, which runs in a
        # different thread. Do not leave the initializing thread holding its WAL on Windows.
        c.close()
        _local.c = None


def import_database(data: bytes) -> dict[str, Any]:
    """Swap in an uploaded SQLite file as the database (see /api/admin/import-db)."""
    tmp = f"{_db_path}.upload"
    with open(tmp, "wb") as f:
        f.write(data)
    check = sqlite3.connect(tmp)
    try:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sessions", "mails", "events"} <= tables:
            raise ValueError("uploaded DB has no forwarder schema")
        counts = {
            t: check.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("sessions", "mails", "quotes", "bookings")
        }
        check.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        check.close()
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
        _local.c = None
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(_db_path + suffix)
        except FileNotFoundError:
            pass
    os.replace(tmp, _db_path)
    init_db(_db_path)  # new generation: every thread reopens; runs migrations
    since = kv_get("mail.next_since_id")
    log.warning("database imported: %s, since_id=%s", counts, since)
    return {"ok": True, "counts": counts, "since_id": since}


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _one(cur: sqlite3.Cursor) -> dict[str, Any] | None:
    r = cur.fetchone()
    cur.fetchall()
    return dict(r) if r else None


def _json(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- events


def _emit(
    c: sqlite3.Connection,
    kind: str,
    message: str,
    session_id: int | None = None,
    level: str = "info",
    duration_ms: int | None = None,
    cost_micro_usd: int | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    cur = c.execute(
        "INSERT INTO events(session_id, kind, level, message, duration_ms, cost_micro_usd,"
        " payload_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            session_id,
            kind,
            level,
            message,
            duration_ms,
            cost_micro_usd,
            _json(payload),
            now(),
        ),
    )
    return int(cur.lastrowid or 0)


def emit(
    kind: str,
    message: str,
    session_id: int | None = None,
    level: str = "info",
    duration_ms: int | None = None,
    cost_micro_usd: int | None = None,
    **payload: Any,
) -> int | None:
    """Write a timeline event. Never raises: events must not stop the business flow."""
    try:
        with tx() as c:
            return _emit(
                c,
                kind,
                message,
                session_id,
                level,
                duration_ms,
                cost_micro_usd,
                payload or None,
            )
    except Exception:
        log.exception("emit failed (%s)", kind)
        return None


def _event_row(r: dict[str, Any]) -> dict[str, Any]:
    p = r.pop("payload_json", None)
    r["payload"] = json.loads(p) if p else None
    return r


def events_after(
    last_id: int, limit: int = 200, session_id: int | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events WHERE id > ?"
    args: list[Any] = [last_id]
    if session_id is not None:
        sql += " AND session_id = ?"
        args.append(session_id)
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    return [_event_row(r) for r in _rows(conn().execute(sql, args))]


# ---------------------------------------------------------------- kv


def add_carrier_pref(customer: str, carrier: str, kind: str, session_id: int | None) -> bool:
    """Store that a customer refuses ('exclude') or asks for ('prefer') a carrier. A refusal
    is never downgraded to a preference. Returns True if something changed."""
    cust = (customer or "").strip().lower()
    with tx() as c:
        old = _one(
            c.execute(
                "SELECT kind FROM customer_carrier_prefs WHERE customer = ? AND carrier = ?",
                (cust, carrier),
            )
        )
        if old and (old["kind"] == kind or old["kind"] == "exclude"):
            return False
        c.execute(
            "INSERT INTO customer_carrier_prefs(customer, carrier, kind, session_id, created_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(customer, carrier) DO UPDATE SET kind = excluded.kind,"
            " session_id = excluded.session_id, created_at = excluded.created_at",
            (cust, carrier, kind, session_id, now()),
        )
        return True


def carrier_prefs(customer: str) -> list[tuple[str, str]]:
    rows = _rows(
        conn().execute(
            "SELECT carrier, kind FROM customer_carrier_prefs WHERE customer = ? ORDER BY carrier",
            ((customer or "").strip().lower(),),
        )
    )
    return [(r["carrier"], r["kind"]) for r in rows]


def kv_get(key: str, default: str | None = None) -> str | None:
    r = _one(conn().execute("SELECT value FROM kv WHERE key = ?", (key,)))
    return r["value"] if r else default


def kv_values(prefix: str) -> list[str]:
    """Read cached values in a literal key namespace, without treating wildcards as SQL."""
    return [
        r["value"]
        for r in conn().execute(
            "SELECT value FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
        )
    ]


def delivery_enqueue(mail_id: int, raw: dict, source: str) -> bool:
    """Ack only after commit. A short lock timeout fits the webhook's 5s window."""
    c = conn()
    c.execute("PRAGMA busy_timeout=1000")
    try:
        with tx() as c:
            return (
                c.execute(
                    "INSERT OR IGNORE INTO mail_delivery(mail_id,raw_json,source,updated_at) "
                    "VALUES(?,?,?,?)",
                    (mail_id, _json(raw), source, now()),
                ).rowcount
                == 1
            )
    finally:
        c.execute("PRAGMA busy_timeout=5000")


def delivery_claim(at: float) -> dict | None:
    # the worker polls about once a second: an empty queue costs one indexed read, no write
    # lock. The claim itself re-reads inside the transaction, so it stays atomic.
    if not _one(
        conn().execute(
            "SELECT 1 FROM mail_delivery WHERE status='pending' AND retry_at<=? LIMIT 1", (at,)
        )
    ):
        return None
    with tx() as c:
        row = _one(
            c.execute(
                "SELECT * FROM mail_delivery WHERE status='pending' AND retry_at<=? "
                "ORDER BY mail_id LIMIT 1",
                (at,),
            )
        )
        if row:
            c.execute(
                "UPDATE mail_delivery SET status='processing',attempts=attempts+1,updated_at=? WHERE mail_id=?",
                (now(), row["mail_id"]),
            )
            row["attempts"] += 1
        return row


def delivery_get(mid: int) -> dict | None:
    return _one(conn().execute("SELECT * FROM mail_delivery WHERE mail_id=?", (mid,)))


def delivery_state(mid: int, state: str, error: str | None = None, retry_at: float = 0) -> None:
    with tx() as c:
        c.execute(
            "UPDATE mail_delivery SET status=?,error=?,retry_at=?,updated_at=? WHERE mail_id=?",
            (state, error, retry_at, now(), mid),
        )


def delivery_recover() -> None:
    with tx() as c:
        c.execute(
            "UPDATE mail_delivery SET status='pending' WHERE status IN ('processing','stored')"
        )
        ids = _rows(c.execute("SELECT mail_id FROM mail_delivery WHERE status='dispatching'"))
        c.execute(
            "UPDATE mail_delivery SET status='unknown',error='Restart during dispatch; inspect agent state' WHERE status='dispatching'"
        )
        for row in ids:
            _emit(
                c,
                Ev.WARN,
                f"Mail {row['mail_id']}: Verarbeitung bei Neustart unklar, manuell prüfen",
                level="warn",
            )


def validation_get(sid: int) -> dict[str, Any] | None:
    r = _one(conn().execute("SELECT * FROM rfq_validation WHERE session_id=?", (sid,)))
    if r:
        r["draft"] = json.loads(r.pop("draft_json"))
        r["root"] = json.loads(r.pop("root_json"))
    return r


def validation_save(sid: int, state: str, draft: dict, root: dict) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO rfq_validation(session_id,state,draft_json,root_json,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,"
            "draft_json=excluded.draft_json,updated_at=excluded.updated_at",
            (sid, state, _json(draft), _json(root), now()),
        )


def clarification_claim(sid: int) -> bool:
    with tx() as c:
        return (
            c.execute(
                "UPDATE rfq_validation SET send_state='sending',updated_at=? "
                "WHERE session_id=? AND send_state='pending' AND EXISTS "
                "(SELECT 1 FROM sessions WHERE id=? AND paused=0 AND mode='auto') "
                "AND NOT EXISTS(SELECT 1 FROM kv WHERE key='global_pause' AND value='1')",
                (now(), sid, sid),
            ).rowcount
            == 1
        )


def clarification_sent(sid: int, state: str, mail_id: int | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE rfq_validation SET send_state=?,sent_mail_id=?,updated_at=? WHERE session_id=?",
            (state, mail_id, now(), sid),
        )


def clarification_reply_claim(sid: int, mid: int) -> bool:
    with tx() as c:
        if c.execute(
            "SELECT 1 FROM clarification_answers WHERE session_id=? AND mail_id=?", (sid, mid)
        ).fetchone():
            return False
        row = _one(
            c.execute(
                "SELECT state,send_state,sent_mail_id FROM rfq_validation WHERE session_id=?",
                (sid,),
            )
        )
        if (
            not row
            or row["state"] != "awaiting_clarification"
            or row["send_state"] not in ("sent", "unknown")
        ):
            return False
        if row["sent_mail_id"] is not None and mid <= row["sent_mail_id"]:
            return False
        c.execute("INSERT INTO clarification_answers VALUES (?,?)", (sid, mid))
        return (
            c.execute(
                "UPDATE rfq_validation SET reply_mail_id=?,state='resolving',updated_at=? "
                "WHERE session_id=? AND state='awaiting_clarification' AND send_state IN ('sent','unknown') "
                "AND reply_mail_id IS NULL",
                (mid, now(), sid),
            ).rowcount
            == 1
        )


def clarification_reopen(sid: int, draft: dict, root: dict | None = None) -> None:
    """Only called after consuming a new reply, or opening a different post-quote workflow."""
    with tx() as c:
        c.execute(
            "UPDATE rfq_validation SET state='awaiting_clarification',send_state='pending',"
            "sent_mail_id=NULL,reply_mail_id=NULL,draft_json=?,root_json=COALESCE(?,root_json),"
            "updated_at=? WHERE session_id=?",
            (_json(draft), _json(root) if root else None, now(), sid),
        )


def clarification_open_workflow(sid: int, draft: dict, root: dict) -> bool:
    with tx() as c:
        previous = _one(c.execute("SELECT state FROM rfq_validation WHERE session_id=?", (sid,)))
        if previous and previous["state"] in (
            "awaiting_clarification",
            "resolving",
            "manual_review",
        ):
            return False
        if _one(
            c.execute(
                "SELECT 1 FROM mails WHERE session_id=? AND direction='out'"
                " AND category='clarification' AND in_reply_to=?",
                (sid, root["id"]),
            )
        ):
            return False
        c.execute(
            "INSERT INTO rfq_validation(session_id,state,draft_json,root_json,updated_at)"
            " VALUES (?,'awaiting_clarification',?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
            " state='awaiting_clarification',draft_json=excluded.draft_json,root_json=excluded.root_json,"
            " send_state='pending',sent_mail_id=NULL,reply_mail_id=NULL,updated_at=excluded.updated_at",
            (sid, _json(draft), _json(root), now()),
        )
        return True


def customer_mail_history(sid: int, sender: str, before_mail: int) -> list[dict[str, Any]]:
    from email.utils import parseaddr

    rows = _rows(
        conn().execute(
            "SELECT id,sender,subject,body FROM mails WHERE session_id=?"
            " AND direction='in' AND id<? AND category IS NOT 'phishing'"
            " ORDER BY id DESC LIMIT 20",
            (sid, before_mail),
        )
    )
    owner = parseaddr(sender)[1].lower()
    return [r for r in rows if parseaddr(r["sender"] or "")[1].lower() == owner]


def customer_correspondence(sid: int) -> list[dict[str, Any]]:
    """Small customer-only conversation window, with current thread taking priority."""
    from email.utils import parseaddr

    root = _one(
        conn().execute(
            "SELECT s.customer,m.received_at FROM sessions s JOIN mails m ON m.id=s.id WHERE s.id=?",
            (sid,),
        )
    )
    if not root:
        return []
    owner = parseaddr(root["customer"] or "")[1].lower()
    if not owner:
        return []
    rows = _rows(
        conn().execute(
            "SELECT m.id,m.session_id,m.direction,m.sender,m.recipient,m.subject,m.body,"
            " m.category,m.received_at,s.customer AS session_customer FROM mails m"
            " JOIN sessions s ON s.id=m.session_id WHERE m.id<>? AND"
            " (m.session_id=? OR (s.id<? AND julianday(m.received_at)<julianday(?)))"
            " AND (LOWER(s.customer)=? OR LOWER(s.customer) LIKE ?)"
            " ORDER BY (m.session_id=?) DESC,julianday(m.received_at) DESC,m.id DESC LIMIT 80",
            (sid, sid, sid, root["received_at"], owner, f"%<{owner}>", sid),
        )
    )
    selected = []
    per_scope = {"current_thread": 0, "previous_conversation": 0}
    for row in rows:
        if parseaddr(row.pop("session_customer") or "")[1].lower() != owner:
            continue
        peer = row["sender"] if row["direction"] == "in" else row["recipient"]
        if parseaddr(peer or "")[1].lower() != owner:
            continue
        if row["category"] not in {
            "rfq",
            "customer",
            "quote",
            "clarification",
            "booking_confirmation",
        }:
            continue
        scope = "current_thread" if row["session_id"] == sid else "previous_conversation"
        if per_scope[scope] >= 4:
            continue
        per_scope[scope] += 1
        selected.append({**row, "scope": scope})
    return list(reversed(selected))


def validation_pending() -> list[dict[str, Any]]:
    return _rows(
        conn().execute(
            "SELECT session_id FROM rfq_validation WHERE "
            "(send_state IN ('pending','sending') AND state='awaiting_clarification') OR state='resolving'"
        )
    )


def kv_set(key: str, value: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def kv_claim(key: str) -> bool:
    """Atomic once-only marker: True for the first caller, False for every later one."""
    with tx() as c:
        return (
            c.execute("INSERT OR IGNORE INTO kv(key, value) VALUES (?, ?)", (key, now())).rowcount
            == 1
        )


def tracking_notice_candidates() -> list[int]:
    """Active customer workflows whose already-sent mail may need a missing notice."""
    return [
        int(r["id"])
        for r in _rows(
            conn().execute(
                "SELECT s.id FROM sessions s WHERE s.kind='rfq' AND s.paused=0 AND s.mode='auto'"
                " AND (s.status IN ('quoted','booked','confirmed') OR EXISTS(SELECT 1 FROM bookings b WHERE b.session_id=s.id AND b.status IN ('booked','confirmed')))"
                " AND (EXISTS(SELECT 1 FROM quotes q WHERE q.session_id=s.id)"
                " OR EXISTS(SELECT 1 FROM bookings b WHERE b.session_id=s.id)) ORDER BY s.id DESC"
            )
        )
    ]


def claim_tracking_mail(key: str, grace_s: float) -> tuple[str, bool] | None:
    """One initial tracking send and, by user policy, one final send after uncertainty.

    Uses kv rather than a new table. A crashed reservation ages into uncertainty. A known
    receipt always wins. The final reservation is never recycled after another ambiguity.
    """
    import time

    stamp = time.time()
    with tx() as c:
        row = _one(c.execute("SELECT value FROM kv WHERE key=?", (key,)))
        chosen, final = key, False
        if row:
            if row["value"] not in ("reserved", "unknown"):
                return None
            previous = _one(c.execute("SELECT value FROM kv WHERE key=?", (key + ".at",)))
            if previous is None:
                c.execute(
                    "INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)", (key + ".at", str(stamp))
                )
                return None
            if stamp - float(previous["value"]) < grace_s:
                return None
            chosen, final = key + ".final", True
        if (
            c.execute(
                "INSERT OR IGNORE INTO kv(key,value) VALUES (?, 'reserved')", (chosen,)
            ).rowcount
            != 1
        ):
            return None
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES (?,?)", (chosen + ".at", str(stamp)))
        return chosen, final


def tracking_mail_result(
    key: str, attempt: str, mail_id: int | None = None, *, uncertain: bool = False
) -> None:
    with tx() as c:
        if mail_id is not None:
            for target in {key, attempt}:
                c.execute(
                    "INSERT OR REPLACE INTO kv(key,value) VALUES (?,?)", (target, f"sent:{mail_id}")
                )
        elif uncertain:
            c.execute("UPDATE kv SET value='unknown' WHERE key=? AND value='reserved'", (attempt,))
        else:
            c.execute("DELETE FROM kv WHERE key=? AND value='reserved'", (attempt,))


def tracking_unanswered(since: str, before: str, limit: int = 30) -> list[dict[str, Any]]:
    """Recent unanswered tracking inputs, including customer replies in booked threads."""
    return _rows(
        conn().execute(
            "SELECT m.* FROM mails m JOIN sessions s ON s.id=m.session_id "
            "WHERE m.direction='in' AND m.category IN ('tracking','customer') "
            "AND m.received_at>=? AND m.received_at<=? AND s.paused=0 AND s.mode='auto' "
            "AND (s.kind='tracking' OR m.category='tracking' OR EXISTS(SELECT 1 FROM bookings b WHERE b.session_id=s.id AND b.status IN ('booked','confirmed') AND (b.customer_state IS NULL OR b.customer_state IN ('confirmed_sent','price_accepted')))) "
            "AND NOT EXISTS(SELECT 1 FROM mails sent WHERE sent.direction='out' AND sent.in_reply_to=m.id) "
            "AND NOT EXISTS(SELECT 1 FROM mail_screening q WHERE q.mail_id IN (m.id,s.id) AND q.blocked=1 AND q.released_at IS NULL) "
            "AND NOT EXISTS(SELECT 1 FROM kv k WHERE k.key='tracking.reply.' || m.id AND k.value LIKE 'sent:%') "
            "ORDER BY m.id DESC LIMIT ?",
            (since, before, limit),
        )
    )


def clarification_waiting() -> list[dict[str, Any]]:
    """Sessions whose question went out and whose answer has not resolved it yet."""
    return _rows(
        conn().execute(
            "SELECT v.session_id, s.created_at, v.draft_json FROM rfq_validation v"
            " JOIN sessions s ON s.id = v.session_id"
            " WHERE v.state = 'awaiting_clarification' AND v.send_state IN ('sent','unknown')"
        )
    )


def validation_reply(sid: int) -> dict[str, Any] | None:
    return _one(
        conn().execute(
            "SELECT m.* FROM mails m JOIN rfq_validation v ON m.id=v.reply_mail_id "
            "WHERE v.session_id=?",
            (sid,),
        )
    )


def clarification_unhandled_replies() -> list[dict[str, Any]]:
    return _rows(
        conn().execute(
            "SELECT m.* FROM mails m JOIN rfq_validation v ON v.session_id=m.session_id "
            "WHERE v.state='awaiting_clarification' AND v.send_state IN ('sent','unknown') "
            "AND m.direction='in' AND m.id>COALESCE(v.sent_mail_id,v.session_id) "
            "AND m.category IN ('customer','tracking') AND NOT EXISTS "
            "(SELECT 1 FROM clarification_answers a WHERE a.session_id=m.session_id AND a.mail_id=m.id) "
            "ORDER BY m.id"
        )
    )


def validation_replay_inputs() -> list[dict[str, Any]]:
    return _rows(
        conn().execute(
            "SELECT s.id,s.request_json,m.raw_json,m.body,m.subject,m.sender,m.recipient,m.received_at "
            "FROM sessions s JOIN mails m ON m.id=s.id WHERE s.kind='rfq' ORDER BY s.id"
        )
    )


# ---------------------------------------------------------------- mails & sessions


def _resolve_session(
    c: sqlite3.Connection, in_reply_to: int | None, references: Sequence[int]
) -> int | None:
    """Session of the nearest known ancestor mail (parent first, then back to the root)."""
    candidates = ([in_reply_to] if in_reply_to else []) + list(reversed(references))
    for mid in candidates:
        r = _one(c.execute("SELECT session_id FROM mails WHERE id = ?", (mid,)))
        if r and r["session_id"] is not None:
            return int(r["session_id"])
    return None


_QUOTED_SUBJECT = re.compile(r'your (?:mail|message|request) "([^"]+)"', re.IGNORECASE)
_RE_PREFIX = re.compile(r"^\s*((re|aw|fwd?|wg)\s*:\s*)+", re.IGNORECASE)
SUBJECT_MATCH_WINDOW = timedelta(hours=2)


def _session_by_subject(c: sqlite3.Connection, subject: str, body: str) -> int | None:
    """Fallback when a reply carries no in_reply_to/references (seen on ratedesk replies):
    match the quoted subject in the body, or the subject without Re:, against a mail we
    sent recently. Newest match wins."""
    candidates = [m.group(1) for m in _QUOTED_SUBJECT.finditer(body or "")]
    stripped = _RE_PREFIX.sub("", subject or "").strip()
    if stripped:
        candidates.append(stripped)
    since = (datetime.now(UTC) - SUBJECT_MATCH_WINDOW).isoformat(timespec="seconds")
    for cand in candidates:
        r = _one(
            c.execute(
                "SELECT session_id FROM mails WHERE direction = 'out' AND session_id IS NOT NULL"
                " AND lower(subject) = lower(?) AND received_at >= ? ORDER BY received_at DESC"
                " LIMIT 1",
                (cand.strip(), since),
            )
        )
        if r:
            return int(r["session_id"])
    return None


_OUR_LANE = re.compile(r"\b([A-Z]{3}) to ([A-Z]{3})\b|\bto ([A-Z]{3}) airport\b")
_OUR_WEIGHT = re.compile(r",\s*(\d+(?:\.\d+)?) kg /")
_STATED_KG = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(?:kgs?|kilos?)\b(?!\s*/)")


def _kg_figure(raw: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        return float(re.sub(r"[.,]", "", raw))
    return float(raw.replace(",", "."))


def bid_request_for_reply(
    sender: str, subject: str, body: str, c: sqlite3.Connection | None = None
) -> tuple[int, int] | None:
    """Fallback for a carrier reply with no headers, no [AP-n] tag and a changed subject
    (rate desks answer unthreaded): (session, our bid request mail) when exactly one recent
    request to this carrier fits. The weight the reply names decides between two open
    requests on the same lane ("for the 1,250 kg shipment"); without a weight the lane must
    be unique. Two candidates that still fit are never guessed: the reply stays unassigned."""
    addr = parseaddr(sender or "")[1].strip().lower()
    if not addr:
        return None
    since = (datetime.now(UTC) - SUBJECT_MATCH_WINDOW).isoformat(timespec="seconds")
    rows = _rows(
        (c or conn()).execute(
            "SELECT id, session_id, subject FROM mails WHERE direction = 'out'"
            " AND category = 'bid_request' AND session_id IS NOT NULL"
            " AND lower(recipient) = ? AND received_at >= ? ORDER BY id DESC",
            (addr, since),
        )
    )
    text = f"{subject or ''}\n{body or ''}"
    stated = {_kg_figure(m[1]) for m in _STATED_KG.finditer(text)}
    upper = set(re.findall(r"\b[A-Z]{3}\b", text))
    fits: dict[int, int] = {}
    for r in rows:
        ours = r["subject"] or ""
        lane = _OUR_LANE.search(ours)
        codes = {x for x in (lane.groups() if lane else ()) if x}
        if upper & codes != codes:
            continue  # the reply names no or another lane
        weight = _OUR_WEIGHT.search(ours)
        billed = re.search(r"billed at (\d+(?:\.\d+)?) kg", ours)
        ours_kg = {float(m[1]) for m in (weight, billed) if m}
        if stated and not any(abs(k - w) <= 0.5 for k in stated for w in ours_kg):
            continue
        fits.setdefault(int(r["session_id"]), int(r["id"]))
    if len(fits) != 1:
        if len(fits) > 1:
            log.warning("unthreaded bid from %s fits sessions %s: left unassigned", addr, fits)
        return None
    return next(iter(fits.items()))


REPLY_SESSION_STATUSES = ("new", "pricing", "quoted", "accepted", "booked", "confirmed")
ORPHAN_REPLY = "orphan_reply"  # customer reply whose thread we do not know: a person decides


def mail_header(mail_id: int) -> dict[str, Any] | None:
    """A stored mail (either direction) for routing: sender, recipient, subject, raw copy.
    Lets the poller read a parent from our own DB before asking the mailbox API."""
    return _one(
        conn().execute(
            "SELECT id, direction, sender, recipient, subject, received_at, raw_json FROM mails"
            " WHERE id = ?",
            (mail_id,),
        )
    )


def session_for_headerless_reply(sender: str, subject: str) -> int | None:
    """A customer mail without in_reply_to/references that is clearly a reply ("Re:" etc.)
    to an open session of the same sender with the same subject, sent in the last two hours.
    Newest session wins. A mail without a reply prefix stays a new request."""
    if not _RE_PREFIX.match(subject or ""):
        return None
    wanted = _RE_PREFIX.sub("", subject).strip().lower()
    addr = parseaddr(sender or "")[1].strip().lower()
    if not wanted or not addr:
        return None
    since = (datetime.now(UTC) - SUBJECT_MATCH_WINDOW).isoformat(timespec="seconds")
    marks = ",".join("?" * len(REPLY_SESSION_STATUSES))
    for r in _rows(
        conn().execute(
            f"SELECT id, customer, subject FROM sessions WHERE kind = 'rfq'"
            f" AND status IN ({marks}) AND created_at >= ? ORDER BY id DESC",
            (*REPLY_SESSION_STATUSES, since),
        )
    ):
        if (
            parseaddr(r["customer"] or "")[1].strip().lower() == addr
            and _RE_PREFIX.sub("", r["subject"] or "").strip().lower() == wanted
        ):
            return int(r["id"])
    return None


_BOOK_INTENT = re.compile(
    r"\b(?:please\s+(?:book|proceed)|go\s+ahead|book\s+(?:it|the\s+shipment|this)|"
    r"we\s+(?:accept|approve)|approved|confirm(?:ed)?\s+the\s+(?:booking|offer|quote)|"
    r"(?:fest\s+)?(?:ein)?buchen|bucht|buchung(?:sfreigabe)?|freigegeben|freigabe|beauftragen|"
    r"r[ée]server|reservar|prenotare|confermiamo)\b",
    re.IGNORECASE,
)
ACCEPTANCE_MATCH_WINDOW = timedelta(hours=12)
_QUOTE_REQUEST = re.compile(
    r"\b(?:please quote|quote (?:us|me)|request for quot|rfq|price for|best (?:price|rate)|"
    r"bitte um (?:ein |ihr |euer )?(?:angebot|preis)|angebot (?:f[üu]r|anfragen)|preisanfrage|"
    r"anfrage (?:luftfracht|export))",
    re.IGNORECASE,
)
_ROUTE_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:-+>?|–|→|/|\bto\b|\bnach\b)\s*([A-Z]{3})\b")


def session_for_unthreaded_acceptance(sender: str, subject: str, body: str) -> int | None:
    """A booking approval sent as a brand-new mail ("Buchungsfreigabe MUC - ICN (480 kg)",
    no Re:, no headers) to the one quoted, unbooked session of the same sender. Any route
    or weight the mail names must match that quote; several fitting sessions or none:
    None, and the mail stays a new request."""
    text = f"{subject or ''}\n{body or ''}"
    addr = parseaddr(sender or "")[1].strip().lower()
    if not addr or not _BOOK_INTENT.search(text) or _QUOTE_REQUEST.search(text):
        return None
    since = (datetime.now(UTC) - ACCEPTANCE_MATCH_WINDOW).isoformat(timespec="seconds")
    stated_kg = {_kg_figure(m[1]) for m in _STATED_KG.finditer(text)}
    pairs = set(_ROUTE_PAIR.findall(text))
    fits = []
    for r in _rows(
        conn().execute(
            "SELECT s.id, s.customer, s.request_json, q.origin, q.destination FROM sessions s"
            " JOIN quotes q ON q.session_id = s.id WHERE s.kind = 'rfq' AND s.status = 'quoted'"
            " AND q.sent_at >= ? AND NOT EXISTS (SELECT 1 FROM bookings b"
            " WHERE b.session_id = s.id) ORDER BY s.id DESC",
            (since,),
        )
    ):
        if parseaddr(r["customer"] or "")[1].strip().lower() != addr:
            continue
        lane = (r["origin"], r["destination"])
        if pairs and lane not in pairs:
            continue  # the mail names another route
        try:
            request = json.loads(r["request_json"] or "{}")
        except ValueError:
            request = {}
        kg = request.get("weight_kg")
        if stated_kg and kg and not all(abs(k - float(kg)) <= 0.5 for k in stated_kg):
            continue
        if int(r["id"]) not in fits:
            fits.append(int(r["id"]))
    return fits[0] if len(fits) == 1 else None


_AP_TAG = re.compile(r"\bAP-(\d+)\b")
_BK_REF = re.compile(r"\bBK-[A-Z0-9]{4,}\b")


def _may_reference(c: sqlite3.Connection, session_id: int, sender: str) -> bool:
    """A sender may join a session by quoting its reference only when it is that session's
    customer, a known carrier, 5U's rate desk or tracking agent, or someone we wrote to in
    that session. Anyone else quoting "AP-<n>" would read or steer another shipment."""
    from app.config import settings
    from app.pricing.reference import carrier_by_email

    addr = parseaddr(sender or "")[1].strip().lower()
    if not addr:
        return False
    if addr in (settings.ratedesk_address, settings.tracking_address) or carrier_by_email(addr):
        return True
    row = _one(c.execute("SELECT customer FROM sessions WHERE id = ?", (session_id,)))
    if row and parseaddr(row["customer"] or "")[1].strip().lower() == addr:
        return True
    return bool(
        _one(
            c.execute(
                "SELECT 1 FROM mails WHERE session_id = ? AND direction = 'out'"
                " AND lower(recipient) = ? LIMIT 1",
                (session_id, addr),
            )
        )
    )


def _session_by_reference(
    c: sqlite3.Connection, subject: str, body: str, sender: str, category: str | None = None
) -> int | None:
    """Fallback for replies whose headers point to a mail we never stored (bid requests and
    bookings sent outside record_outgoing): our tag "[AP-<session>]" in the subject, our
    "Your reference: AP-<session>" in the body, or a known BK booking reference. Only for a
    sender allowed in that session (see _may_reference), never for quarantined mail."""
    if category == "phishing":
        return None
    for text in (subject or "", body or ""):
        for m in _AP_TAG.finditer(text):
            sid = int(m.group(1))
            if _one(c.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,))) and _may_reference(
                c, sid, sender
            ):
                return sid
    for m in _BK_REF.finditer(f"{subject or ''} {body or ''}"):
        r = _one(
            c.execute(
                "SELECT session_id FROM bookings WHERE booking_reference = ? LIMIT 1",
                (m.group(0),),
            )
        )
        if r and _may_reference(c, int(r["session_id"]), sender):
            return int(r["session_id"])
    return None


def remap_unassigned() -> int:
    """Attach stored unassigned incoming mails that carry one of our references. Idempotent;
    runs at startup so mails ingested before this fallback existed show in their session."""
    with tx() as c:
        rows = _rows(
            c.execute(
                "SELECT id, subject, body, sender, category FROM mails"
                " WHERE session_id IS NULL AND direction = 'in'"
            )
        )
        n = 0
        for r in rows:
            sid = _session_by_reference(c, r["subject"], r["body"], r["sender"], r["category"])
            if sid is not None:
                c.execute("UPDATE mails SET session_id = ? WHERE id = ?", (sid, r["id"]))
                n += 1
        return n


_CONF = {
    "bk": re.compile(r"Booking reference:\s*(BK-[A-Z0-9]+)", re.IGNORECASE),
    "ap": re.compile(r"Your reference:\s*AP-(\d+)", re.IGNORECASE),
    "airline": re.compile(r"Airline:\s*([^\n(]+?)\s*(?:\(|\n|$)", re.IGNORECASE),
    "lane": re.compile(r"Lane:\s*([A-Z]{3})-([A-Z]{3})", re.IGNORECASE),
    "price": re.compile(r"Price:\s*([\d.,]+)\s*([A-Z]{3})", re.IGNORECASE),
}


def backfill_bookings_from_confirmations() -> int:
    """Airline confirmations ("Booking BK-… confirmed") for sessions without a booking row,
    e.g. booked by an instance whose data never reached this database. Recreates the
    booking so tracking questions find it. Idempotent."""
    c = conn()
    mails = _rows(
        c.execute(
            "SELECT m.body FROM mails m WHERE m.direction = 'in' AND m.body LIKE '%Booking reference:%'"
            " AND m.body LIKE '%Your reference: AP-%' AND m.category IS NOT 'phishing'"
        )
    )
    n = 0
    for m in mails:
        body = m["body"] or ""
        found = {k: rx.search(body) for k, rx in _CONF.items()}
        if not (found["bk"] and found["ap"]):
            continue
        sid = int(found["ap"].group(1))
        if not _one(c.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,))):
            continue
        if _one(c.execute("SELECT 1 FROM bookings WHERE session_id = ?", (sid,))):
            # booked by email before the BK reference was kept: fill it in
            with tx() as w:
                w.execute(
                    "UPDATE bookings SET booking_reference = ?, updated_at = ?"
                    " WHERE session_id = ? AND booking_reference IS NULL AND status != 'failed'",
                    (found["bk"].group(1).upper(), now(), sid),
                )
            continue
        price = found["price"]
        from app.pricing.amounts import parse_decimal
        from app.pricing.pricing import to_minor

        amount = parse_decimal(price.group(1)) if price else None
        minor = to_minor(amount) if amount is not None else None
        record_booking(
            sid,
            booking_reference=found["bk"].group(1),
            rate_id=None,
            airline=found["airline"].group(1).strip() if found["airline"] else None,
            price_minor=minor,
            currency=price.group(2).upper() if price else None,
            status="confirmed",
        )
        lane = found["lane"]
        s = _one(c.execute("SELECT request_json FROM sessions WHERE id = ?", (sid,)))
        if lane and s and not s["request_json"]:
            set_request(
                sid,
                {"origin": lane.group(1).upper(), "destination": lane.group(2).upper()},
            )
        n += 1
    return n


def _create_session(
    c: sqlite3.Connection,
    session_id: int,
    category: str,
    sender: str,
    subject: str,
    received_at: str,
) -> None:
    kind = SESSION_CATEGORIES[category]
    deadline = (_parse(received_at) + DEADLINE).isoformat(timespec="seconds")
    c.execute(
        "INSERT INTO sessions(id, kind, customer, subject, status, deadline_at,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            session_id,
            kind,
            sender,
            subject,
            "new",
            deadline if kind == "rfq" else None,
            received_at,
            now(),
        ),
    )


def ingest_mail(
    *,
    mail_id: int,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    category: str,
    received_at: str | None = None,
    references: Sequence[int] = (),
    in_reply_to: int | None = None,
    raw: dict[str, Any] | None = None,
    report_new: bool = False,
    session_hint: int | None = None,
) -> int | None | tuple[int | None, bool]:
    """Store an incoming mail and return its session id (None = unassigned).

    Idempotent: a mail id seen before changes nothing and returns its stored session.
    `session_hint` is the session the poller matched a header-less reply to. A customer
    reply whose thread we do not know never opens a session: it is stored unassigned and
    raised as an `orphan_reply` error so a person decides.
    """
    received_at = received_at or now()
    refs = [int(x) for x in references]
    thread_root = refs[0] if refs else mail_id
    with tx() as c:
        known = _one(c.execute("SELECT session_id FROM mails WHERE id = ?", (mail_id,)))
        if known:
            return (known["session_id"], False) if report_new else known["session_id"]
        session_id = _resolve_session(c, in_reply_to, refs) or session_hint
        if (
            session_id is None
            and not refs
            and in_reply_to is None
            and category not in SESSION_CATEGORIES
        ):
            session_id = _session_by_subject(c, subject, body)
        if session_id is None and (category not in SESSION_CATEGORIES or category == "customer"):
            session_id = _session_by_reference(c, subject, body, sender, category)
        if session_id is None and category == "carrier_bid" and not refs and in_reply_to is None:
            match = bid_request_for_reply(sender, subject, body, c)
            session_id = match[0] if match else None
        orphan = session_id is None and category == "customer"
        created = False
        if session_id is None and category in SESSION_CATEGORIES and not orphan:
            session_id = mail_id
            created = True
            _create_session(c, session_id, category, sender, subject, received_at)
        c.execute(
            "INSERT INTO mails(id, session_id, thread_root, in_reply_to, direction, sender,"
            " recipient, subject, body, category, received_at, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mail_id,
                session_id,
                thread_root,
                in_reply_to,
                "in",
                sender,
                recipient,
                subject,
                body,
                category,
                received_at,
                _json(raw),
            ),
        )
        if session_id is not None and not created:
            c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now(), session_id))
        label = "Neue Session" if created else ("Mail" if session_id else "Unzugeordnete Mail")
        _emit(
            c,
            Ev.MAIL_IN,
            f"{label}: {category} von {sender} – {subject[:80]}",
            session_id,
            payload={"mail_id": mail_id, "category": category},
        )
        if orphan:
            _emit(
                c,
                ORPHAN_REPLY,
                f"Kundenantwort ohne bekannten Thread, keine Session eröffnet: {sender}"
                f" – {subject[:80]}",
                None,
                "error",
                payload={"mail_id": mail_id, "in_reply_to": in_reply_to, "references": refs},
            )
    return (session_id, True) if report_new else session_id


def orphan_replies(limit: int = 20) -> list[dict[str, Any]]:
    """Customer replies we could not place in a thread and nobody has assigned yet."""
    rows = _rows(
        conn().execute(
            "SELECT m.id AS mail_id, m.sender, m.subject, m.received_at FROM events e"
            " JOIN mails m ON m.id = json_extract(e.payload_json, '$.mail_id')"
            " WHERE e.kind = ? AND m.session_id IS NULL ORDER BY e.id DESC LIMIT ?",
            (ORPHAN_REPLY, limit),
        )
    )
    return rows


def record_outgoing(
    *,
    mail_id: int,
    recipient: str,
    subject: str,
    body: str,
    category: str,
    session_id: int | None = None,
    sender: str | None = None,
    references: Sequence[int] = (),
    in_reply_to: int | None = None,
    sent_at: str | None = None,
    raw: dict[str, Any] | None = None,
) -> int | None:
    """Store a mail WE sent. Must be called for every outgoing mail (bids, ratedesk, replies),
    otherwise replies to it cannot be mapped to their session."""
    refs = [int(x) for x in references]
    with tx() as c:
        if session_id is None:
            session_id = _resolve_session(c, in_reply_to, refs)
        c.execute(
            "INSERT INTO mails(id, session_id, thread_root, in_reply_to, direction, sender,"
            " recipient, subject, body, category, received_at, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (
                mail_id,
                session_id,
                refs[0] if refs else mail_id,
                in_reply_to,
                "out",
                sender,
                recipient,
                subject,
                body,
                category,
                sent_at or now(),
                _json(raw),
            ),
        )
        if session_id is not None:
            c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now(), session_id))
        _emit(
            c,
            Ev.MAIL_OUT,
            f"Gesendet an {recipient}: {subject[:80]}",
            session_id,
            payload={"mail_id": mail_id, "category": category},
        )
    return session_id


def get_session(session_id: int) -> dict[str, Any] | None:
    c = conn()
    s = _one(c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)))
    if not s:
        return None
    s["request"] = json.loads(s.pop("request_json")) if s.get("request_json") else None
    s["validation"] = validation_get(session_id)
    s["mails"] = _rows(
        c.execute(
            "SELECT id, direction, sender, recipient, subject, body, category, received_at,"
            " in_reply_to FROM mails WHERE session_id = ? ORDER BY received_at, id",
            (session_id,),
        )
    )
    quotes = _rows(
        c.execute("SELECT * FROM quotes WHERE session_id = ? ORDER BY id", (session_id,))
    )
    for q in quotes:
        a = q.pop("alternatives_json")
        q["alternatives"] = json.loads(a) if a else []
    for q in quotes:
        for column in ("request_snapshot_json", "pricing_snapshot_json"):
            q[column.removesuffix("_json")] = json.loads(q.pop(column) or "null")
    s["workflow"] = reply_workflow(session_id)
    s["case_worker"] = case_latest(session_id)
    s["quotes"] = quotes
    s["bookings"] = _rows(c.execute("SELECT * FROM bookings WHERE session_id = ?", (session_id,)))
    s["events"] = [
        _event_row(r)
        for r in _rows(
            c.execute("SELECT * FROM events WHERE session_id = ? ORDER BY id", (session_id,))
        )
    ]
    s["llm_cost_micro_usd"] = _one(
        c.execute(
            "SELECT COALESCE(SUM(cost_micro_usd),0) AS v FROM llm_calls WHERE session_id = ?",
            (session_id,),
        )
    )["v"]  # type: ignore[index]
    s["llm_usage"] = session_llm_usage(session_id)
    s["board"] = _rows(c.execute("SELECT * FROM board_results WHERE session_id = ?", (session_id,)))
    return s


def session_export_extras(session_id: int) -> dict[str, Any]:
    """Raw rows for the manual context export (app/ui/context_export.py): everything a human
    or an outside model needs to continue a thread when the pipeline fails. Read only."""
    c = conn()
    mails = _rows(
        c.execute(
            "SELECT id, thread_root, raw_json FROM mails WHERE session_id = ? ORDER BY received_at, id",
            (session_id,),
        )
    )
    for m in mails:
        raw = m.pop("raw_json")
        m["raw"] = json.loads(raw) if raw else None
    ids = [int(m["id"]) for m in mails]
    marks = ",".join("?" * len(ids))
    screening = (
        _rows(c.execute(f"SELECT * FROM mail_screening WHERE mail_id IN ({marks})", ids))
        if ids
        else []
    )
    delivery = (
        _rows(
            c.execute(
                f"SELECT mail_id, source, status, attempts, error, updated_at FROM mail_delivery"
                f" WHERE mail_id IN ({marks})",
                ids,
            )
        )
        if ids
        else []
    )
    llm = _rows(
        c.execute(
            "SELECT id, purpose, model, prompt_tokens, completion_tokens, cost_micro_usd,"
            " latency_ms, created_at FROM llm_calls WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
    )
    rescue = _rows(
        c.execute(
            "SELECT input_key, state, decision_json, updated_at FROM thread_rescue_runs"
            " WHERE session_id = ? ORDER BY updated_at",
            (session_id,),
        )
    )
    for r in rescue:
        r["decision"] = json.loads(r.pop("decision_json") or "null")
    answers = _rows(
        c.execute("SELECT mail_id FROM clarification_answers WHERE session_id = ?", (session_id,))
    )
    return {
        "mails": mails,
        "screening": screening,
        "delivery": delivery,
        "llm_calls": llm,
        "rescue_runs": rescue,
        "clarification_answers": [int(a["mail_id"]) for a in answers],
    }


# shared by list_sessions and session_ids: both must pick and order the same rows
_NEEDS_ATTENTION = """CASE WHEN s.status = 'error' OR s.paused = 1
             OR EXISTS (SELECT 1 FROM rfq_validation v WHERE v.session_id=s.id
                        AND v.state IN ('awaiting_clarification','manual_review','resolving'))
             OR (s.status IN ('new','pricing') AND s.deadline_at IS NOT NULL
                 AND (julianday(s.deadline_at) - julianday('now')) * 86400 < ?)
           THEN 1 ELSE 0 END"""
_LIST_ORDER = """ ORDER BY needs_attention DESC,
               CASE WHEN s.status IN ('new','pricing') THEN s.deadline_at END ASC,
               s.updated_at DESC LIMIT ?"""


def list_sessions(
    status: str | None = None,
    q: str | None = None,
    attention: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Sessions for the list view. Attention first (error, paused, deadline close), then by
    deadline, then most recently updated."""
    sql = f"""
    SELECT s.*,
      (SELECT message FROM events e WHERE e.session_id = s.id ORDER BY e.id DESC LIMIT 1) AS last_event,
      (SELECT COALESCE(SUM(cost_micro_usd),0) FROM llm_calls l WHERE l.session_id = s.id) AS llm_cost_micro_usd,
      (SELECT total_minor FROM quotes qq WHERE qq.session_id = s.id ORDER BY qq.id LIMIT 1) AS quote_total_minor,
      (SELECT currency FROM quotes qq WHERE qq.session_id = s.id ORDER BY qq.id LIMIT 1) AS quote_currency,
      {_NEEDS_ATTENTION} AS needs_attention
    FROM sessions s WHERE 1=1
    """
    args: list[Any] = [ATTENTION_WINDOW_S]
    if status:
        sql += " AND s.status = ?"
        args.append(status)
    if q:
        sql += " AND (s.customer LIKE ? OR s.subject LIKE ? OR CAST(s.id AS TEXT) = ?)"
        args += [f"%{q}%", f"%{q}%", q]
    if attention:
        sql += " AND needs_attention = 1"
    sql += _LIST_ORDER
    args.append(limit)
    rows = _rows(conn().execute(sql, args))
    for r in rows:
        r.pop("request_json", None)
    return rows


def request_catalog(
    *,
    q: str = "",
    status: str = "",
    kind: str = "",
    airport: str = "",
    days: int = 0,
    sort: str = "created_at",
    direction: str = "desc",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Complete paginated request catalogue; no legacy 200-row list truncation."""
    columns = {
        k: k
        for k in (
            "created_at",
            "updated_at",
            "customer",
            "status",
            "origin",
            "destination",
            "weight_kg",
            "total_minor",
            "id",
        )
    }
    if sort not in columns or direction not in ("asc", "desc"):
        raise ValueError("invalid request sorting")
    where, args = ["1=1"], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if airport:
        where.append("(origin=? OR destination=?)")
        args.extend([airport.upper(), airport.upper()])
    if days:
        where.append("created_at>=?")
        args.append((datetime.now(UTC) - timedelta(days=days)).isoformat())
    if q:
        where.append(
            "(customer LIKE ? OR subject LIKE ? OR CAST(id AS TEXT)=?"
            " OR origin LIKE ? OR destination LIKE ? OR booking_reference LIKE ?)"
        )
        args.extend([f"%{q}%", f"%{q}%", q.lstrip("#"), f"%{q}%", f"%{q}%", f"%{q}%"])
    base = (
        "WITH catalogue AS (SELECT s.id,s.kind,s.customer,s.subject,s.status,s.paused,s.mode,"
        " s.created_at,s.updated_at,s.first_reply_at,s.deadline_at,"
        " COALESCE(json_extract(s.request_json,'$.origin'),q.origin) AS origin,"
        " COALESCE(json_extract(s.request_json,'$.destination'),q.destination) AS destination,"
        " json_extract(s.request_json,'$.weight_kg') AS weight_kg,"
        " q.total_minor,q.currency,q.airline,b.booking_reference,b.status AS booking_status"
        " FROM sessions s LEFT JOIN quotes q ON q.id=(SELECT MAX(id) FROM quotes WHERE session_id=s.id)"
        " LEFT JOIN bookings b ON b.session_id=s.id) SELECT * FROM catalogue WHERE "
        + " AND ".join(where)
    )
    c = conn()
    total = c.execute("SELECT COUNT(*) FROM (" + base + ")", args).fetchone()[0]
    order = (
        "currency ASC," if sort == "total_minor" else ""
    ) + f"{columns[sort]} {direction},id {direction}"
    items = _rows(
        c.execute(base + " ORDER BY " + order + " LIMIT ? OFFSET ?", [*args, limit, offset])
    )
    return {"items": items, "total": total, "offset": offset, "limit": limit}


def session_ids(status: str, limit: int = 200) -> list[int]:
    """Ids of list_sessions(status=status, limit=limit), same rows in the same order, without
    the per-row display columns. For background loops that only walk the sessions."""
    sql = (
        f"SELECT s.id, {_NEEDS_ATTENTION} AS needs_attention FROM sessions s"
        f" WHERE s.status = ? {_LIST_ORDER}"
    )
    return [int(r["id"]) for r in conn().execute(sql, (ATTENTION_WINDOW_S, status, limit))]


def list_unassigned(limit: int = 100) -> list[dict[str, Any]]:
    return _rows(
        conn().execute(
            "SELECT id, direction, sender, recipient, subject, category, received_at FROM mails"
            " WHERE session_id IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )


def resumable_rfqs(created_after: str) -> list[dict[str, Any]]:
    """RFQ sessions left mid-flight (new/pricing, no quote, created after `created_after`),
    each with its first customer mail (the RFQ itself), oldest first."""
    return _rows(
        conn().execute(
            "SELECT s.id AS session_id, m.id, m.sender, m.recipient, m.subject, m.body,"
            " m.received_at, m.raw_json FROM sessions s JOIN mails m ON m.id = s.id"
            # 'error' too: a restart usually ships the fix, and the stored request is reused
            " WHERE s.kind = 'rfq' AND s.status IN ('new','pricing','error')"
            " AND s.first_reply_at IS NULL"
            " AND s.created_at > ? AND m.direction = 'in'"
            " AND NOT EXISTS (SELECT 1 FROM quotes q WHERE q.session_id = s.id)"
            " ORDER BY s.created_at",
            (created_after,),
        )
    )


def incoming_mails_containing(text: str, limit: int = 1000) -> list[dict[str, Any]]:
    """Incoming mails whose body contains `text` (case-insensitive), oldest first."""
    return _rows(
        conn().execute(
            "SELECT id, sender, body, received_at FROM mails WHERE direction = 'in'"
            " AND category IS NOT 'phishing' AND instr(lower(body), lower(?)) > 0"
            " ORDER BY id LIMIT ?",
            (text, limit),
        )
    )


def bid_request_recipients(session_id: int) -> set[str]:
    """Carrier addresses already contacted for this RFQ, used for restart-safe fan-out."""
    rows = conn().execute(
        "SELECT recipient FROM mails WHERE session_id = ? AND direction = 'out'"
        " AND category = 'bid_request' AND id>?",
        (session_id, reply_revision_floor(session_id)),
    )
    return {str(row["recipient"] or "").strip().lower() for row in rows if row["recipient"]}


def last_pickup(customer: str, before_session: int | None = None) -> str | None:
    """Pickup ZIP (or area) of this customer's most recent earlier request, for a new
    door-to-port request that names none."""
    rows = _rows(
        conn().execute(
            "SELECT request_json FROM sessions WHERE customer = ? AND kind = 'rfq'"
            " AND request_json IS NOT NULL AND id != ? ORDER BY created_at DESC LIMIT 20",
            (customer, before_session or -1),
        )
    )
    for r in rows:
        req = json.loads(r["request_json"]) or {}
        if req.get("pickup_zip") or req.get("pickup_city"):
            return str(req.get("pickup_zip") or req.get("pickup_city"))
    # earlier requests whose extraction failed still name their ZIP in the mail text
    # (round 4639: Rheintal's request 937 said 51896 but was never extracted)
    from app.pricing.reference import pickup_zones

    zones = pickup_zones()
    mails = _rows(
        conn().execute(
            "SELECT body FROM mails WHERE sender = ? AND direction = 'in' AND id != ?"
            " AND category IN ('rfq','customer') ORDER BY received_at DESC LIMIT 20",
            (customer, before_session or -1),
        )
    )
    for m in mails:
        for z in re.findall(r"(?<![\d.,-])(\d{5})(?![\d.,-]|\s*(?:kg|cbm|kgs))", m["body"] or ""):
            if z[:2] in zones:
                return z
    return None


def set_status(session_id: int, status: str, reason: str | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), session_id),
        )
        _emit(
            c,
            Ev.STATUS,
            f"Status → {status}" + (f" ({reason})" if reason else ""),
            session_id,
            level="error" if status == "error" else "info",
        )


def set_request(session_id: int, request: dict[str, Any]) -> None:
    """Store the extracted RFQ (origin, destination, weight, mode, ...)."""
    with tx() as c:
        c.execute(
            "UPDATE sessions SET request_json = ?, updated_at = ? WHERE id = ?",
            (_json(request), now(), session_id),
        )
        _emit(
            c,
            Ev.EXTRACTED,
            "Anfrage erkannt: " + ", ".join(f"{k}={v}" for k, v in request.items()),
            session_id,
            payload=request,
        )


# ---------------------------------------------------------------- human control


def is_global_paused() -> bool:
    return kv_get("global_pause", "0") == "1"


def is_paused(session_id: int) -> bool:
    if is_global_paused():
        return True
    r = _one(conn().execute("SELECT paused FROM sessions WHERE id = ?", (session_id,)))
    return bool(r and r["paused"])


def is_manual(session_id: int) -> bool:
    r = _one(conn().execute("SELECT mode FROM sessions WHERE id = ?", (session_id,)))
    return bool(r and r["mode"] == "manual")


def claim_quote_send(session_id: int) -> bool:
    """Atomically reserve the one quote of a session for the agent.

    Only send the quote if this returns True. Fails if paused (session or global), taken over
    by a human, or a first reply was already claimed."""
    with tx() as c:
        g = _one(c.execute("SELECT value FROM kv WHERE key = 'global_pause'"))
        if g and g["value"] == "1":
            return False
        cur = c.execute(
            "UPDATE sessions SET first_reply_at = ?, updated_at = ? WHERE id = ? AND mode = 'auto'"
            " AND paused = 0 AND first_reply_at IS NULL",
            (now(), now(), session_id),
        )
        return cur.rowcount == 1


def set_paused(session_id: int, paused: bool, who: str = "Mensch") -> None:
    with tx() as c:
        c.execute(
            "UPDATE sessions SET paused = ?, updated_at = ? WHERE id = ?",
            (int(paused), now(), session_id),
        )
        _emit(
            c,
            Ev.HUMAN_ACTION,
            f"{'Pausiert' if paused else 'Fortgesetzt'} von {who}",
            session_id,
            level="warn" if paused else "info",
        )


def set_global_pause(paused: bool, who: str = "Mensch") -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES ('global_pause', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if paused else "0",),
        )
        _emit(
            c,
            Ev.HUMAN_ACTION,
            f"Alle ausgehenden Mails {'GESTOPPT' if paused else 'wieder frei'} ({who})",
            None,
            level="warn" if paused else "info",
        )


def takeover(session_id: int, who: str = "Mensch") -> None:
    """Human takes over: the agent must not touch this session any more."""
    with tx() as c:
        c.execute(
            "UPDATE sessions SET mode = 'manual', updated_at = ? WHERE id = ?",
            (now(), session_id),
        )
        _emit(
            c,
            Ev.HUMAN_ACTION,
            f"Von {who} übernommen – Agent schweigt",
            session_id,
            level="warn",
        )


def handback(session_id: int, who: str = "Mensch") -> None:
    with tx() as c:
        c.execute(
            "UPDATE sessions SET mode = 'auto', paused = 0, updated_at = ? WHERE id = ?",
            (now(), session_id),
        )
        _emit(c, Ev.HUMAN_ACTION, f"An Agent zurückgegeben ({who})", session_id)


def mark_manual_reply(session_id: int) -> None:
    """Call after a human reply was sent: sets first_reply_at if still empty."""
    with tx() as c:
        c.execute(
            "UPDATE sessions SET first_reply_at = COALESCE(first_reply_at, ?), updated_at = ?"
            " WHERE id = ?",
            (now(), now(), session_id),
        )


# ---------------------------------------------------------------- business records


def record_quote(
    session_id: int,
    *,
    total_minor: int,
    currency: str,
    airline: str | None,
    origin: str | None,
    destination: str | None,
    transit_days: int | None,
    source: str,
    rate_id: str | None = None,
    valid_until: str | None = None,
    alternatives: list[dict[str, Any]] | None = None,
    mail_id: int | None = None,
    air_total_minor: int | None = None,
    air_currency: str | None = None,
    request_version: int | None = None,
    supersedes_id: int | None = None,
    insurance_minor: int = 0,
) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO quotes(session_id, airline, origin, destination, total_minor, currency,"
            " transit_days, source, rate_id, valid_until, alternatives_json, mail_id, sent_at,"
            " air_total_minor, air_currency, insurance_minor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                airline,
                origin,
                destination,
                total_minor,
                currency,
                transit_days,
                source,
                rate_id,
                valid_until,
                _json(alternatives),
                mail_id,
                now(),
                air_total_minor,
                air_currency,
                insurance_minor,
            ),
        )
        quote_id = int(cur.lastrowid or 0)
        request_row = _one(c.execute("SELECT request_json FROM sessions WHERE id=?", (session_id,)))
        audit = _one(
            c.execute(
                "SELECT payload_json FROM events WHERE session_id=? AND kind='pricing_audit' ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
        )
        c.execute(
            "UPDATE quotes SET request_version=?, request_snapshot_json=?, pricing_snapshot_json=?, supersedes_id=? WHERE id=?",
            (
                request_version or 1,
                request_row["request_json"] if request_row else None,
                audit["payload_json"] if audit else None,
                supersedes_id,
                quote_id,
            ),
        )
        if request_version is not None:
            c.execute(
                "UPDATE thread_workflow SET active_quote_id=?, phase='awaiting_customer', updated_at=? WHERE session_id=? AND request_version=?",
                (quote_id, now(), session_id, request_version),
            )
            c.execute(
                "UPDATE sessions SET status='quoted' WHERE id=? AND status NOT IN ('booked','confirmed')",
                (session_id,),
            )
        c.execute(
            "UPDATE sessions SET status = 'quoted', updated_at = ? WHERE id = ?"
            " AND status IN ('new','pricing')",
            (now(), session_id),
        )
        _emit(
            c,
            Ev.QUOTED,
            f"Quote: {airline or '?'} {total_minor / 100:.2f} {currency} ({source})",
            session_id,
            payload={
                "total_minor": total_minor,
                "currency": currency,
                "airline": airline,
                "rate_id": rate_id,
            },
        )
        return int(cur.lastrowid or 0)


def team_reference(session_id: int) -> str:
    return f"AP-{session_id}"


def record_booking(
    session_id: int,
    *,
    booking_reference: str | None,
    rate_id: str | None,
    airline: str | None,
    price_minor: int | None,
    currency: str | None,
    status: str = "booked",
) -> int:
    ref = team_reference(session_id)
    with tx() as c:
        c.execute(
            "INSERT INTO bookings(session_id, team_reference, booking_reference, rate_id, airline,"
            " price_minor, currency, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(team_reference) DO UPDATE SET booking_reference = excluded.booking_reference,"
            " rate_id = excluded.rate_id, status = excluded.status, updated_at = excluded.updated_at",
            (
                session_id,
                ref,
                booking_reference,
                rate_id,
                airline,
                price_minor,
                currency,
                status,
                now(),
                now(),
            ),
        )
        if status in ("booked", "confirmed"):
            c.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now(), session_id),
            )
        _emit(
            c,
            Ev.BOOKED,
            f"Buchung {booking_reference or ref}: {status}",
            session_id,
            level="error" if status == "failed" else "info",
            payload={"booking_reference": booking_reference, "rate_id": rate_id},
        )
        r = _one(c.execute("SELECT id FROM bookings WHERE team_reference = ?", (ref,)))
        return int(r["id"]) if r else 0


def update_booking_status(
    reference: str, status: str, booking_reference: str | None = None
) -> int | None:
    """reference = team_reference or booking_reference (e.g. from 'Booking BK-… confirmed').

    `booking_reference`: the airline's BK reference for a booking made by email, which has
    none until the airline confirms. Stored once; an existing reference is never replaced."""
    with tx() as c:
        r = _one(
            c.execute(
                "SELECT session_id FROM bookings WHERE team_reference = ? OR booking_reference = ?",
                (reference, reference),
            )
        )
        if not r:
            return None
        c.execute(
            "UPDATE bookings SET status = ?, booking_reference = COALESCE(booking_reference, ?),"
            " updated_at = ? WHERE team_reference = ? OR booking_reference = ?",
            (status, booking_reference, now(), reference, reference),
        )
        if status == "confirmed":
            c.execute(
                "UPDATE sessions SET status = 'confirmed', updated_at = ? WHERE id = ?",
                (now(), r["session_id"]),
            )
        _emit(c, Ev.BOOKED, f"Buchung {reference}: {status}", r["session_id"])
        return int(r["session_id"])


def cancel_session_booking(session_id: int, reason: str) -> None:
    """The customer cancelled and the portal confirmed: the booking no longer counts (status
    'failed', customer_state 'cancelled', schema unchanged) and the session is closed."""
    with tx() as c:
        c.execute(
            "UPDATE bookings SET status = 'failed', customer_state = 'cancelled', updated_at = ?"
            " WHERE session_id = ? AND status IN ('booked','confirmed')",
            (now(), session_id),
        )
        c.execute(
            "UPDATE sessions SET status = 'lost', updated_at = ? WHERE id = ?",
            (now(), session_id),
        )
        _emit(c, Ev.BOOKED, f"Buchung storniert: {reason}"[:300], session_id)


def set_booking_customer_state(
    session_id: int, state: str, customer_total_minor: int | None = None
) -> None:
    """Where the customer side of a booking stands (booking.py owns the states)."""
    with tx() as c:
        c.execute(
            "UPDATE bookings SET customer_state = ?,"
            " customer_total_minor = COALESCE(?, customer_total_minor), updated_at = ?"
            " WHERE session_id = ?",
            (state, customer_total_minor, now(), session_id),
        )


def set_airline_price(session_id: int, price_minor: int, currency: str) -> None:
    """The air price the airline confirmed (booking response or confirmation mail)."""
    with tx() as c:
        c.execute(
            "UPDATE bookings SET airline_price_minor = ?, airline_currency = ?, updated_at = ?"
            " WHERE session_id = ?",
            (price_minor, currency.upper(), now(), session_id),
        )


def bookings_with_customer_state(state: str) -> list[dict[str, Any]]:
    return _rows(conn().execute("SELECT * FROM bookings WHERE customer_state = ?", (state,)))


def tracking_records() -> list[dict[str, Any]]:
    """Every booking that did not fail, with what the tracking agent may tell: customer, subject, the request
    (JSON) and the first quote (route, transit days). Newest first."""
    rows = _rows(
        conn().execute(
            "SELECT b.session_id, b.team_reference, b.booking_reference, b.rate_id, b.airline,"
            " b.price_minor, b.currency, b.customer_total_minor, q.total_minor AS quote_total_minor,"
            " q.currency AS quote_currency, b.status, b.created_at AS booked_at, b.updated_at,"
            " s.customer, s.subject, CASE WHEN b.quote_id IS NOT NULL THEN COALESCE(q.request_snapshot_json,s.request_json) ELSE s.request_json END AS request_json,"
            " q.origin, q.destination, q.transit_days"
            " FROM bookings b JOIN sessions s ON s.id = b.session_id"
            " LEFT JOIN quotes q ON q.id = COALESCE(b.quote_id,(SELECT MIN(id) FROM quotes WHERE session_id = s.id))"
            " WHERE b.status != 'failed'"
            " ORDER BY b.created_at DESC"
        )
    )
    for r in rows:
        r["request"] = json.loads(r.pop("request_json")) if r.get("request_json") else {}
        # The customer's all-in price, never the portal's bare air rate (which can be USD and
        # leaves out trucking): the customer-confirmed total, else the quoted total.
        customer_total = r.pop("customer_total_minor")
        quote_total, quote_currency = r.pop("quote_total_minor"), r.pop("quote_currency")
        if quote_currency and (customer_total is not None or quote_total is not None):
            r["price_minor"] = customer_total if customer_total is not None else quote_total
            r["currency"] = quote_currency
    return rows


def find_booking(
    reference: str | None = None, session_id: int | None = None
) -> dict[str, Any] | None:
    """Lookup for the tracking agent: by booking/team reference or session."""
    c = conn()
    if reference:
        return _one(
            c.execute(
                "SELECT * FROM bookings WHERE booking_reference = ?"
                " OR team_reference = ? OR rate_id = ?",
                (reference,) * 3,
            )
        )
    if session_id is not None:
        return _one(c.execute("SELECT * FROM bookings WHERE session_id = ?", (session_id,)))
    return None


def rescue_run(sid: int, key: str) -> dict[str, Any] | None:
    row = _one(
        conn().execute(
            "SELECT * FROM thread_rescue_runs WHERE session_id=? AND input_key=?", (sid, key)
        )
    )
    if row:
        row["decision"] = json.loads(row.pop("decision_json") or "null")
    return row


def rescue_candidates(before: str, after: str | None = None) -> list[dict[str, Any]]:
    """Latest unanswered customer input or unquoted RFQ, after the supervisor grace time.

    `after`: ignore input received before it (old threads are not a live workflow)."""
    return _rows(
        conn().execute(
            "SELECT m.*,s.id AS session_id FROM sessions s JOIN mails m ON m.id=("
            "SELECT MAX(i.id) FROM mails i WHERE i.session_id=s.id AND i.direction='in' "
            "AND i.category IN ('rfq','customer','tracking') AND i.sender=s.customer) "
            "JOIN mails root ON root.id=s.id WHERE root.category='rfq' "
            "AND s.mode='auto' AND s.paused=0 AND m.received_at<=? AND m.received_at>=? "
            "AND (NOT EXISTS(SELECT 1 FROM quotes q WHERE q.session_id=s.id) "
            "OR (NOT EXISTS(SELECT 1 FROM mails o WHERE o.session_id=s.id AND o.direction='out' "
            "AND o.in_reply_to=m.id) AND m.id<>s.id)) "
            "AND NOT EXISTS(SELECT 1 FROM mails o WHERE o.session_id=s.id AND o.direction='out' "
            "AND o.category IN ('manual_reply','notice')) ORDER BY m.received_at LIMIT 100",
            (before, after or ""),
        )
    )


def rescue_answer_seen(sid: int, mid: int) -> None:
    """The owner consumes replies so the legacy clarification worker cannot race it."""
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO clarification_answers VALUES (?,?)", (sid, mid))


def claim_rescue_run(sid: int, key: str) -> bool:
    """A lease allows a stopped process to resume a persisted plan, never a second worker."""
    stale = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
    with tx() as c:
        if c.execute(
            "SELECT 1 FROM thread_rescue_runs WHERE session_id=? AND input_key<>? "
            "AND state IN ('running','planned','failed') AND updated_at>=?",
            (sid, key, stale),
        ).fetchone():
            return False
        return (
            c.execute(
                "INSERT INTO thread_rescue_runs VALUES (?,?,'running',NULL,?) "
                "ON CONFLICT(session_id,input_key) DO UPDATE SET state='running',updated_at=excluded.updated_at "
                "WHERE thread_rescue_runs.state IN ('running','planned','failed') "
                "AND thread_rescue_runs.updated_at<?",
                (sid, key, now(), stale),
            ).rowcount
            == 1
        )


def save_rescue_run(sid: int, key: str, state: str, decision: dict | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE thread_rescue_runs SET state=?,decision_json=COALESCE(?,decision_json),"
            "updated_at=? WHERE session_id=? AND input_key=?",
            (state, _json(decision), now(), sid, key),
        )


def reserve_rescue_call(sid: int, limit: int) -> int | None:
    key = f"thread_rescue_calls:{sid}"
    with tx() as c:
        row = _one(c.execute("SELECT value FROM kv WHERE key=?", (key,)))
        count = int(row["value"]) if row else 0
        if count >= limit:
            return None
        c.execute(
            "INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(count + 1)),
        )
    return count + 1


def reserve_llm_call(session_id: int, limit: int) -> int | None:
    """Count one more gateway call for the session, persisted so a restart cannot reset it.
    Returns the new count, or None if the session already made `limit` calls. Calls logged in
    llm_calls before this counter existed count too."""
    key = f"llm_calls:{session_id}"
    with tx() as c:
        kv = _one(c.execute("SELECT value FROM kv WHERE key = ?", (key,)))
        logged = _one(
            c.execute(
                "SELECT COUNT(*) AS n FROM llm_calls WHERE session_id = ? AND purpose NOT IN ('thread_rescue','case_plan','case_audit')",
                (session_id,),
            )
        )
        n = max(int(kv["value"]) if kv else 0, int(logged["n"]) if logged else 0)
        if n >= limit:
            return None
        c.execute(
            "INSERT INTO kv(key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(n + 1)),
        )
    return n + 1


def record_llm_call(
    session_id: int | None,
    *,
    purpose: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_micro_usd: int,
    latency_ms: int,
    accounting: dict[str, Any] | None = None,
) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO llm_calls(session_id, purpose, model, prompt_tokens, completion_tokens,"
            " cost_micro_usd, latency_ms, created_at, accounting_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                purpose,
                model,
                prompt_tokens,
                completion_tokens,
                cost_micro_usd,
                latency_ms,
                now(),
                _json(accounting) if accounting is not None else None,
            ),
        )
        _emit(
            c,
            Ev.LLM_CALL,
            f"LLM {purpose}: {model}, {prompt_tokens}+{completion_tokens} Tokens,"
            f" {cost_micro_usd / 1e6:.4f} $",
            session_id,
            duration_ms=latency_ms,
            cost_micro_usd=cost_micro_usd,
        )


def session_llm_usage(session_id: int) -> dict[str, Any]:
    calls = _rows(
        conn().execute("SELECT * FROM llm_calls WHERE session_id = ? ORDER BY id", (session_id,))
    )
    incomplete = 0
    for call in calls:
        call["accounting"] = json.loads(call.pop("accounting_json") or "null")
        if call["accounting"] and call["accounting"]["status"] != "estimated":
            incomplete += 1
    return {
        "calls": calls,
        "call_count": len(calls),
        "currency": "USD",
        "input_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
        "output_tokens": sum(c["completion_tokens"] or 0 for c in calls),
        "known_cost_micro_usd": sum(c["cost_micro_usd"] for c in calls),
        "total_micro_usd": None if incomplete else sum(c["cost_micro_usd"] for c in calls),
        "incomplete_calls": incomplete,
        "legacy_calls": sum(c["accounting"] is None for c in calls),
    }


def save_board_result(
    *,
    round_id: str,
    verdict: str | None,
    reason: str | None,
    points: dict[str, Any] | None,
    raw: dict[str, Any],
    session_id: int | None = None,
    failed: bool = False,
    cause: dict[str, Any] | None = None,
) -> bool:
    """Upsert one board round. Returns True if the verdict is new or changed. A changed
    `failed` round is emitted as an error (shows in the UI attention list). `cause` (why a
    round got no answer) is kept from the first sync that brings one."""
    with tx() as c:
        old = _one(c.execute("SELECT verdict FROM board_results WHERE round_id = ?", (round_id,)))
        c.execute(
            "INSERT INTO board_results(round_id, session_id, verdict, reason, points_json, raw_json,"
            " fetched_at, cause_json) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET"
            " session_id = COALESCE(excluded.session_id, board_results.session_id),"
            " verdict = excluded.verdict, reason = excluded.reason,"
            " points_json = excluded.points_json, raw_json = excluded.raw_json,"
            " fetched_at = excluded.fetched_at,"
            " cause_json = COALESCE(board_results.cause_json, excluded.cause_json)",
            (round_id, session_id, verdict, reason, _json(points), _json(raw), now(), _json(cause)),
        )
        changed = old is None or old["verdict"] != verdict
        if changed and verdict:
            _emit(
                c,
                Ev.BOARD_VERDICT,
                f"Board: {verdict}" + (f" ({reason})" if reason else ""),
                session_id,
                level="error" if failed else ("warn" if verdict != "accepted" else "info"),
                payload={"round_id": round_id, "points": points},
            )
        return changed


def note_dataset_version(version: str) -> bool:
    """Persist transitions and their alert atomically; the first observation is a baseline."""
    with tx() as c:
        old = _one(c.execute("SELECT value FROM kv WHERE key='portal.dataset_version'"))
        if old and old["value"] == version:
            return False
        c.execute(
            "INSERT INTO kv(key,value) VALUES ('portal.dataset_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (version,),
        )
        if old:
            _emit(
                c,
                "dataset_changed",
                f"Portal dataset_version changed {old['value']} -> {version}",
                level="warn",
                payload={"old": old["value"], "new": version},
            )
        return bool(old)


def queue_tariff(mail_id: int, raw: dict[str, Any]) -> None:
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO tariff_jobs(mail_id,raw_json) VALUES (?,?)",
            (mail_id, _json(raw)),
        )


def pending_tariffs() -> list[dict[str, Any]]:
    return _rows(
        conn().execute(
            "SELECT mail_id,raw_json FROM tariff_jobs WHERE status='pending' ORDER BY mail_id"
        )
    )


def queue_stored_tariffs() -> None:
    """Backfill tariff mails stored by older pollers without touching their cursor."""
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO tariff_jobs(mail_id,raw_json) "
            "SELECT id,raw_json FROM mails WHERE category='tariff' AND raw_json IS NOT NULL"
        )


def finish_tariff(mail_id: int, error: str | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE tariff_jobs SET status=?,error=? WHERE mail_id=?",
            ("failed" if error else "done", error, mail_id),
        )


def pricing_evidence() -> list[dict[str, Any]]:
    """Read-only audit/board join for breakpoint investigations."""
    return _rows(
        conn().execute(
            "SELECT e.session_id,e.created_at,e.payload_json,b.verdict,b.reason,b.points_json "
            "FROM events e LEFT JOIN board_results b ON b.session_id=e.session_id "
            "WHERE e.kind='pricing_audit' ORDER BY e.id"
        )
    )


# ---------------------------------------------------------------- phishing gate (mail_screening)


def screening_get(mail_id: int) -> dict[str, Any] | None:
    r = _one(conn().execute("SELECT * FROM mail_screening WHERE mail_id = ?", (mail_id,)))
    if r:
        r["reasons"] = json.loads(r.pop("reasons_json"))
        d = r.pop("detail_json")
        r["detail"] = json.loads(d) if d else None
    return r


def screening_save(
    *,
    mail_id: int,
    sender: str,
    thread_root: int,
    verdict: str,
    layer: str,
    score: float,
    blocked: bool,
    routed_as: str,
    reasons: list[str],
    detail: dict[str, Any] | None = None,
) -> None:
    """First verdict wins: a redelivered mail keeps its stored screening."""
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO mail_screening(mail_id, sender, thread_root, verdict, layer,"
            " score, blocked, routed_as, reasons_json, detail_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                mail_id,
                sender,
                thread_root,
                verdict,
                layer,
                score,
                1 if blocked else 0,
                routed_as,
                _json(reasons),
                _json(detail),
                now(),
            ),
        )


def screening_released(sender: str, thread_root: int) -> bool:
    """A person released this sender in this thread: trust the rest of the thread."""
    return (
        _one(
            conn().execute(
                "SELECT 1 FROM mail_screening WHERE sender = ? AND thread_root = ?"
                " AND released_at IS NOT NULL LIMIT 1",
                (sender, thread_root),
            )
        )
        is not None
    )


def thread_parties(mail_ids: Sequence[int]) -> list[str]:
    """Customers of the sessions the given stored mails belong to ('' for a stored mail
    without a session). Empty when none of the ids is a mail we stored."""
    ids = [int(i) for i in mail_ids][:50]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = _rows(
        conn().execute(
            f"SELECT COALESCE(s.customer, '') AS customer FROM mails m"
            f" LEFT JOIN sessions s ON s.id = m.session_id WHERE m.id IN ({marks})",
            ids,
        )
    )
    return [r["customer"] for r in rows]


def address_change_add(old_address: str, new_address: str, mail_id: int) -> str:
    """Record that the party behind `old_address` now writes from `new_address`; a chain of
    changes keeps pointing at the original verified address. Returns that original."""
    old, new = old_address.strip().lower(), new_address.strip().lower()
    with tx() as c:
        prior = _one(
            c.execute("SELECT old_address FROM address_changes WHERE new_address = ?", (old,))
        )
        original = prior["old_address"] if prior else old
        c.execute(
            "INSERT OR REPLACE INTO address_changes (new_address, old_address, mail_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (new, original, mail_id, now()),
        )
    return original


def address_changes() -> dict[str, str]:
    """new address -> original verified address, oldest change first."""
    return {
        r["new_address"]: r["old_address"]
        for r in _rows(
            conn().execute(
                "SELECT new_address, old_address FROM address_changes ORDER BY created_at, rowid"
            )
        )
    }


def known_customers() -> list[str]:
    return [
        r["customer"]
        for r in _rows(
            conn().execute(
                "SELECT DISTINCT customer FROM sessions WHERE kind = 'rfq' AND customer IS NOT NULL"
            )
        )
    ]


def quarantined(limit: int = 50) -> list[dict[str, Any]]:
    """Blocked mails a person has not released yet, newest first."""
    rows = _rows(
        conn().execute(
            "SELECT q.mail_id, q.sender, q.verdict, q.layer, q.score, q.reasons_json,"
            " q.routed_as, q.created_at, m.subject, m.session_id, m.received_at"
            " FROM mail_screening q JOIN mails m ON m.id = q.mail_id"
            " WHERE q.blocked = 1 AND q.released_at IS NULL ORDER BY q.mail_id DESC LIMIT ?",
            (limit,),
        )
    )
    for r in rows:
        r["reasons"] = json.loads(r.pop("reasons_json"))
    return rows


def reply_blocked(mail_id: int) -> bool:
    """True while `mail_id` is quarantined: nothing may be sent in reply to it."""
    return (
        _one(
            conn().execute(
                "SELECT 1 FROM mail_screening WHERE mail_id = ? AND blocked = 1"
                " AND released_at IS NULL",
                (mail_id,),
            )
        )
        is not None
    )


def release_quarantined(mail_id: int) -> dict[str, Any]:
    """Undo a quarantine: restore the routed category, open the session the mail would have
    opened, trust the sender in this thread. Returns {session_id, category, raw}.
    Raises LookupError when the mail is not quarantined."""
    with tx() as c:
        q = _one(
            c.execute(
                "SELECT * FROM mail_screening WHERE mail_id = ? AND blocked = 1"
                " AND released_at IS NULL",
                (mail_id,),
            )
        )
        m = _one(c.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)))
        if not q or not m:
            raise LookupError(f"mail {mail_id} is not in quarantine")
        category = q["routed_as"]
        session_id = m["session_id"]
        if session_id is None and category in SESSION_CATEGORIES:
            exists = _one(c.execute("SELECT 1 FROM sessions WHERE id = ?", (mail_id,)))
            if not exists:
                _create_session(
                    c, mail_id, category, m["sender"], m["subject"] or "", m["received_at"]
                )
            session_id = mail_id
        c.execute(
            "UPDATE mails SET category = ?, session_id = ? WHERE id = ?",
            (category, session_id, mail_id),
        )
        c.execute("UPDATE mail_screening SET released_at = ? WHERE mail_id = ?", (now(), mail_id))
        _emit(
            c,
            Ev.HUMAN_ACTION,
            f"Mail {mail_id} von {m['sender']} freigegeben, normale Verarbeitung als {category}",
            session_id,
            payload={"mail_id": mail_id, "category": category},
        )
    raw = json.loads(m["raw_json"]) if m.get("raw_json") else None
    return {"session_id": session_id, "category": category, "raw": raw}


# ---------------------------------------------------------------- watchers & stats


def session_by_created_at(ts: str) -> int | None:
    """Find the RFQ session whose first mail arrived at `ts` (board rows carry sent_at)."""
    r = _one(conn().execute("SELECT id FROM sessions WHERE created_at = ?", (ts,)))
    return int(r["id"]) if r else None


NO_ANSWER = "no answer"  # board verdict for a round we never answered
WATCHDOG_REPLY_CATEGORIES = ("quote", "manual_reply", "notice")


def watchdog_candidates(created_after: str, created_before: str) -> list[dict[str, Any]]:
    """RFQ sessions created in (created_after, created_before] with no quote and no other
    reply of ours that ends the thread (quote, manual reply, all-carriers-excluded notice).
    Paused/manual/claimed rows are included so the caller can say why it skips them."""
    return _rows(
        conn().execute(
            "SELECT s.id AS session_id, s.status, s.created_at, s.paused, s.mode,"
            " s.first_reply_at FROM sessions s JOIN mails m ON m.id = s.id"
            # only a root mail routed as an RFQ starts the RFQ flow (poller.dispatch); a
            # phishing-blocked mail is stored as 'phishing' and never qualifies
            " WHERE s.kind = 'rfq' AND m.direction = 'in' AND m.category = 'rfq'"
            " AND s.created_at > ? AND s.created_at <= ?"
            " AND NOT EXISTS (SELECT 1 FROM quotes q WHERE q.session_id = s.id)"
            " AND NOT EXISTS (SELECT 1 FROM mails o WHERE o.session_id = s.id"
            f" AND o.direction = 'out' AND o.category IN {WATCHDOG_REPLY_CATEGORIES!r})"
            " ORDER BY s.created_at",
            (created_after, created_before),
        )
    )


def release_unsent_quote_claim(session_id: int) -> bool:
    """Free a quote claim that provably never reached the mailbox: no quote row, no reply mail
    of ours and no transport marker (set right before the HTTP send). A claim whose send may
    have happened is never released, so this cannot cause a second quote."""
    with tx() as c:
        cur = c.execute(
            "UPDATE sessions SET first_reply_at = NULL, updated_at = ? WHERE id = ?"
            " AND first_reply_at IS NOT NULL AND mode = 'auto'"
            " AND NOT EXISTS (SELECT 1 FROM quotes q WHERE q.session_id = sessions.id)"
            " AND NOT EXISTS (SELECT 1 FROM mails o WHERE o.session_id = sessions.id"
            f" AND o.direction = 'out' AND o.category IN {WATCHDOG_REPLY_CATEGORIES!r})"
            " AND NOT EXISTS (SELECT 1 FROM kv WHERE kv.key = 'quote.transport.' || sessions.id)",
            (now(), session_id),
        )
        if cur.rowcount != 1:
            return False
        _emit(
            c,
            "quote_claim_released",
            "Quote-Claim ohne Versand freigegeben (Neustart oder Abbruch vor dem Senden)",
            session_id,
            level="warn",
        )
        return True


def llm_calls_used(session_id: int) -> int:
    """Gateway calls already counted for the session (same rule as reserve_llm_call)."""
    c = conn()
    kv = _one(c.execute("SELECT value FROM kv WHERE key = ?", (f"llm_calls:{session_id}",)))
    logged = _one(
        c.execute(
            "SELECT COUNT(*) AS n FROM llm_calls WHERE session_id = ? AND purpose NOT IN ('thread_rescue','case_plan','case_audit')",
            (session_id,),
        )
    )
    return max(int(kv["value"]) if kv else 0, int(logged["n"]) if logged else 0)


def root_mail(session_id: int) -> dict[str, Any] | None:
    """The customer's first mail of a session (the RFQ itself)."""
    return _one(
        conn().execute(
            "SELECT id, sender, recipient, subject, body, category, received_at, raw_json"
            " FROM mails WHERE id = ? AND direction = 'in'",
            (session_id,),
        )
    )


def round_cause(session_id: int | None, sent_at: str | None) -> dict[str, Any]:
    """Why a board round got no answer, from our own records: the matching session's last
    status, last error event and last event time, plus any process start inside the round.
    No session for the round's sent time: the mail never reached us (rate limit, outage), or
    it was stored without opening an RFQ session."""
    c = conn()
    cause: dict[str, Any] = {"session_id": session_id, "sent_at": sent_at}
    if sent_at:
        try:
            start = datetime.fromisoformat(sent_at)
            end = (start + DEADLINE).isoformat()
            restarts = _rows(
                c.execute(
                    "SELECT created_at FROM events WHERE kind = 'process_start'"
                    " AND created_at >= ? AND created_at <= ? ORDER BY id",
                    (start.isoformat(timespec="seconds"), end),
                )
            )
            cause["restarts_s"] = [
                int((_parse(r["created_at"]) - start).total_seconds()) for r in restarts
            ]
        except ValueError:
            pass
    if session_id is None:
        stored = (
            _one(
                c.execute(
                    "SELECT id, category FROM mails WHERE direction = 'in' AND received_at = ?",
                    (sent_at,),
                )
            )
            if sent_at
            else None
        )
        if stored:
            cause.update(
                kind="no_session",
                summary=f"Mail {stored['id']} gespeichert ({stored['category']}), keine RFQ-Session",
            )
        else:
            cause.update(kind="mail_not_received", summary="Mail nicht empfangen")
        return cause
    s = _one(c.execute("SELECT status, paused, mode FROM sessions WHERE id = ?", (session_id,)))
    last = _one(
        c.execute(
            "SELECT kind, message, created_at FROM events WHERE session_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
    )
    error = _one(
        c.execute(
            "SELECT kind, message, created_at FROM events WHERE session_id = ?"
            " AND level = 'error' AND kind != 'board_verdict' ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
    )
    status = s["status"] if s else None
    cause.update(
        kind="session",
        status=status,
        paused=bool(s and s["paused"]),
        manual=bool(s and s["mode"] == "manual"),
        last_error=error,
        last_event_at=last["created_at"] if last else None,
        last_event_kind=last["kind"] if last else None,
    )
    why = (error or {}).get("message") or (last or {}).get("message") or "kein Ereignis"
    cause["summary"] = f"Status {status}: {why}"[:200]
    return cause


def unanswered_rounds(limit: int = 200) -> list[dict[str, Any]]:
    """Board rounds with verdict "no answer" and their stored cause, newest round first."""
    rows = _rows(
        conn().execute(
            "SELECT b.round_id, b.session_id, b.raw_json, b.cause_json, s.subject, s.customer"
            " FROM board_results b LEFT JOIN sessions s ON s.id = b.session_id"
            " WHERE b.verdict = ?",
            (NO_ANSWER,),
        )
    )
    out = []
    for r in rows:
        raw = json.loads(r.pop("raw_json") or "null") or {}
        cause = json.loads(r.pop("cause_json") or "null")
        if cause is None:  # synced before causes were stored: derive it now
            cause = round_cause(r["session_id"], raw.get("sent_at"))
        out.append({**r, "sent_at": raw.get("sent_at"), "cause": cause})
    out.sort(key=lambda r: -int(r["round_id"]) if str(r["round_id"]).isdigit() else 0)
    return out[:limit]


def recent_rfqs(limit: int = 12) -> list[dict[str, Any]]:
    """Newest customer RFQ sessions first: arrival and whether our quote/reply went out."""
    return _rows(
        conn().execute(
            "SELECT s.id, s.created_at, s.first_reply_at,"
            " EXISTS (SELECT 1 FROM quotes q WHERE q.session_id = s.id) AS quoted"
            " FROM sessions s JOIN mails m ON m.id = s.id"
            " WHERE s.kind = 'rfq' AND m.direction = 'in' AND m.category = 'rfq'"
            " ORDER BY s.created_at DESC LIMIT ?",
            (limit,),
        )
    )


def deploy_busy(window_s: int = 60) -> dict[str, int]:
    """Work a restart would cut off: sessions being priced, and customer replies of the last
    `window_s` seconds (an acceptance turns into a booking call right after it arrives)."""
    since = (datetime.now(UTC) - timedelta(seconds=window_s)).isoformat(timespec="seconds")
    c = conn()
    # a session left in 'pricing' by a crash must not hold the gate red forever
    stale = (datetime.now(UTC) - timedelta(seconds=400)).isoformat(timespec="seconds")
    pricing = c.execute(
        "SELECT COUNT(*) FROM sessions WHERE status = 'pricing' AND updated_at >= ?", (stale,)
    ).fetchone()[0]
    replies = c.execute(
        "SELECT COUNT(*) FROM mails WHERE direction = 'in' AND category = 'customer'"
        " AND received_at >= ?",
        (since,),
    ).fetchone()[0]
    return {"pricing": int(pricing), "customer_replies": int(replies)}


def expire_overdue() -> list[int]:
    """Mark open RFQ sessions past their deadline as expired. Returns their ids."""
    t = now()
    # the deadline loop calls this every few seconds: take the write lock only when a row
    # would change. Both writes below re-select inside the transaction.
    due = conn().execute(
        "SELECT EXISTS(SELECT 1 FROM sessions WHERE status IN ('new','pricing')"
        " AND deadline_at IS NOT NULL AND deadline_at < ? AND first_reply_at IS NULL)"
        " OR EXISTS(SELECT 1 FROM rfq_validation v JOIN sessions s ON s.id = v.session_id"
        " WHERE v.state IN ('awaiting_clarification','resolving') AND s.deadline_at <= ?"
        " AND s.first_reply_at IS NULL)",
        (t, t),
    )
    if not due.fetchone()[0]:
        return []
    with tx() as c:
        c.execute(
            "UPDATE rfq_validation SET state='expired',updated_at=? WHERE "
            "state IN ('awaiting_clarification','resolving') AND session_id IN "
            "(SELECT id FROM sessions WHERE deadline_at <= ? AND first_reply_at IS NULL)",
            (t, t),
        )
        ids = [
            r["id"]
            for r in _rows(
                c.execute(
                    "SELECT id FROM sessions WHERE status IN ('new','pricing') AND deadline_at IS NOT NULL"
                    " AND deadline_at < ? AND first_reply_at IS NULL",
                    (t,),
                )
            )
        ]
        for sid in ids:
            c.execute(
                "UPDATE sessions SET status = 'expired', updated_at = ? WHERE id = ?",
                (t, sid),
            )
            _emit(c, Ev.WARN, "Deadline abgelaufen ohne Quote", sid, level="error")
    return ids


def stats() -> dict[str, Any]:
    c = conn()
    by_status = {
        r["status"]: r["n"]
        for r in _rows(c.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status"))
    }
    resp = _one(
        c.execute(
            "SELECT AVG((julianday(first_reply_at) - julianday(created_at)) * 86400) AS v"
            " FROM sessions WHERE kind = 'rfq' AND first_reply_at IS NOT NULL"
        )
    )
    cost = _one(
        c.execute(
            "SELECT AVG(t) AS v FROM (SELECT COALESCE(SUM(l.cost_micro_usd),0) AS t FROM quotes q"
            " LEFT JOIN llm_calls l ON l.session_id = q.session_id WHERE q.id IN (SELECT MIN(id) FROM quotes GROUP BY session_id) GROUP BY q.session_id)"
        )
    )
    board = _rows(c.execute("SELECT verdict, COUNT(*) AS n FROM board_results GROUP BY verdict"))
    return {
        "sessions_total": sum(by_status.values()),
        "by_status": by_status,
        "avg_response_s": round(resp["v"], 1) if resp and resp["v"] is not None else None,
        "avg_llm_cost_micro_usd_per_quote": int(cost["v"])
        if cost and cost["v"] is not None
        else None,
        "board": {r["verdict"]: r["n"] for r in board},
        "global_pause": is_global_paused(),
    }


def dashboard(limit: int = 50) -> dict[str, Any]:
    """Data for the CRM dashboard: KPIs, per-quote rows, bookings, hourly volume."""
    c = conn()
    quotes = _rows(
        c.execute(
            """
            SELECT q.session_id, q.airline, q.origin, q.destination, q.total_minor, q.currency,
                   q.transit_days, q.source, q.sent_at, s.status, s.customer,
                   (julianday(s.first_reply_at) - julianday(s.created_at)) * 86400 AS response_s,
                   (SELECT COALESCE(SUM(cost_micro_usd),0) FROM llm_calls l
                     WHERE l.session_id = q.session_id) AS llm_cost_micro_usd,
                   (SELECT verdict FROM board_results b WHERE b.session_id = q.session_id
                     ORDER BY fetched_at DESC LIMIT 1) AS verdict
            FROM quotes q JOIN sessions s ON s.id = q.session_id
            WHERE q.id IN (SELECT MIN(id) FROM quotes GROUP BY session_id)
            ORDER BY q.sent_at DESC LIMIT ?
            """,
            (limit,),
        )
    )
    bookings = _rows(
        c.execute(
            "SELECT b.*, s.customer FROM bookings b JOIN sessions s ON s.id = b.session_id"
            " ORDER BY b.created_at DESC LIMIT ?",
            (limit,),
        )
    )
    hourly = _rows(
        c.execute(
            "SELECT substr(created_at, 1, 13) AS hour, COUNT(*) AS sessions,"
            " SUM(CASE WHEN first_reply_at IS NOT NULL THEN 1 ELSE 0 END) AS answered"
            " FROM sessions WHERE kind = 'rfq' GROUP BY hour ORDER BY hour DESC LIMIT 24"
        )
    )
    llm_total = _one(c.execute("SELECT COALESCE(SUM(cost_micro_usd),0) AS v FROM llm_calls"))
    return {
        "stats": stats(),
        "quotes": quotes,
        "bookings": bookings,
        "hourly": list(reversed(hourly)),
        "llm_total_micro_usd": llm_total["v"] if llm_total else 0,
        "unassigned": len(list_unassigned(1000)),
    }


WON = ("accepted", "booked", "confirmed")
LOST = ("lost", "expired", "error")


def booking_volume_24h() -> dict[str, Any]:
    """Agreed customer order value by booking time, independent of RFQ cohort filters."""
    end = datetime.now(UTC)
    start = end - timedelta(hours=24)
    rows = _rows(
        conn().execute(
            "SELECT b.created_at AS at,b.status,b.customer_state,"
            " COALESCE(b.customer_total_minor,q.total_minor) AS minor,q.currency"
            " FROM bookings b JOIN sessions s ON s.id=b.session_id"
            " LEFT JOIN quotes q ON q.id=COALESCE(b.quote_id,"
            " (SELECT MIN(id) FROM quotes WHERE session_id=b.session_id))"
            " WHERE s.kind='rfq' AND b.created_at>=? AND b.created_at<=? ORDER BY b.created_at",
            (start.isoformat(), end.isoformat()),
        )
    )
    events, amounts, unknown = [], {}, 0
    for row in rows:
        if row["status"] not in ("booked", "confirmed") or row["customer_state"] not in (
            None,
            "confirmed_sent",
            "price_accepted",
        ):
            continue
        if row["minor"] is None or not row["currency"]:
            unknown += 1
            continue
        currency = row["currency"].upper()
        events.append({"at": row["at"], "minor": row["minor"], "currency": currency})
        amounts[currency] = amounts.get(currency, 0) + row["minor"]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "events": events,
        "unknown": unknown,
        "amounts": [{"currency": c, "minor": v} for c, v in sorted(amounts.items())],
    }


def business_metrics(days: int = 0) -> dict[str, Any]:
    """RFQ cohort metrics, without CRM display limits or mixing currency amounts.

    Booked order value is an agreed customer total, not airline cost or invoiced revenue.
    Revisions count once. Pending customer price approval is never booked value.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days else ""
    c = conn()
    rows = _rows(
        c.execute(
            "SELECT s.id,s.customer,s.status,s.created_at,s.first_reply_at,s.request_json,"
            " q.id AS quote_id,q.total_minor,q.currency,"
            " w.session_id AS workflow_id,w.active_quote_id,w.phase,"
            " b.status AS booking_status,b.customer_state,b.customer_total_minor,b.created_at AS booked_at,"
            " bq.total_minor AS booked_quote_minor,bq.currency AS booked_currency,"
            " bq.request_snapshot_json AS booked_request_json"
            " FROM sessions s"
            " LEFT JOIN thread_workflow w ON w.session_id=s.id"
            " LEFT JOIN quotes q ON q.id=(SELECT MAX(id) FROM quotes WHERE session_id=s.id)"
            " LEFT JOIN bookings b ON b.session_id=s.id"
            " LEFT JOIN quotes bq ON bq.id=COALESCE(b.quote_id,"
            " (SELECT MIN(id) FROM quotes WHERE session_id=s.id))"
            " WHERE s.kind='rfq' AND s.created_at>=?",
            (since,),
        )
    )
    totals: dict[str, dict[str, int]] = {k: {} for k in ("booked", "offered", "pipeline")}
    counts = {
        "requests": len(rows),
        "quoted": 0,
        "booked": 0,
        "awaiting_price": 0,
        "open": 0,
        "customers": len({r["customer"] for r in rows if r["customer"]}),
        "unknown_booked_value": 0,
        "answered": 0,
        "answered_within_120s": 0,
    }
    response: list[float] = []
    weight = 0.0
    weight_known = 0
    booked_events: list[dict[str, Any]] = []
    airports: dict[str, dict[str, Any]] = {}
    routes: dict[tuple[str, str], int] = {}

    def add(kind: str, amount: int | None, currency: str | None) -> bool:
        if amount is None or not currency:
            return False
        code = currency.upper()
        totals[kind][code] = totals[kind].get(code, 0) + amount
        return True

    for r in rows:
        try:
            shipment = json.loads(r["request_json"] or "{}")
            origin, destination = shipment.get("origin"), shipment.get("destination")
            if (
                isinstance(origin, str)
                and isinstance(destination, str)
                and re.fullmatch(r"[A-Z]{3}", origin)
                and re.fullmatch(r"[A-Z]{3}", destination)
                and origin != destination
            ):
                routes[(origin, destination)] = routes.get((origin, destination), 0) + 1
            for key in ("origin", "destination"):
                code = shipment.get(key)
                if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code):
                    point = airports.setdefault(
                        code, {"iata": code, "departures": 0, "arrivals": 0}
                    )
                    point["departures" if key == "origin" else "arrivals"] += 1
        except (ValueError, TypeError, AttributeError):
            pass
        if r["quote_id"]:
            counts["quoted"] += 1
            add("offered", r["total_minor"], r["currency"])
        settled = r["booking_status"] in ("booked", "confirmed") and r["customer_state"] in (
            None,
            "confirmed_sent",
            "price_accepted",
        )
        if settled:
            counts["booked"] += 1
            # A legacy unbound booking uses the first quote, never a later revision.
            amount = r["customer_total_minor"]
            if amount is None:
                amount = r["booked_quote_minor"]
            if not add("booked", amount, r["booked_currency"]):
                counts["unknown_booked_value"] += 1
            else:
                booked_events.append(
                    {
                        "at": r["booked_at"],
                        "minor": amount,
                        "currency": r["booked_currency"].upper(),
                    }
                )
            try:
                cargo = json.loads(r["booked_request_json"] or r["request_json"] or "{}")
                kg = cargo.get("weight_kg")
                if isinstance(kg, (int, float)) and 0 <= kg < float("inf"):
                    weight += kg
                    weight_known += 1
            except (ValueError, TypeError, AttributeError):
                pass
        elif r["customer_state"] == "price_change_asked" and r["booking_status"] != "failed":
            counts["awaiting_price"] += 1
        elif r["status"] not in (*LOST, "booked", "confirmed", "accepted"):
            counts["open"] += 1
            current_offer = not r["workflow_id"] or (
                r["active_quote_id"] == r["quote_id"]
                and r["phase"] not in ("repricing", "closed", "deferred")
            )
            if r["quote_id"] and current_offer:
                add("pipeline", r["total_minor"], r["currency"])
        created, answered = _ts(r["created_at"]), _ts(r["first_reply_at"])
        if created and answered:
            seconds = max(0, (answered - created).total_seconds())
            response.append(seconds)
            counts["answered"] += 1
            counts["answered_within_120s"] += seconds <= 120
    return {
        "days": days,
        "cohort": "request_created_at",
        **counts,
        "amounts": {
            k: [{"currency": ccy, "minor": amount} for ccy, amount in sorted(v.items())]
            for k, v in totals.items()
        },
        "avg_response_s": round(sum(response) / len(response), 1) if response else None,
        "booked_weight_kg": round(weight, 2) if weight_known else None,
        "weight_known_bookings": weight_known,
        "booked_events": booked_events,
        "airports": sorted(airports.values(), key=lambda a: a["iata"]),
        "routes": [
            {"origin": origin, "destination": destination, "requests": count}
            for (origin, destination), count in sorted(routes.items())
        ],
    }


def crm(limit: int = 200) -> dict[str, Any]:
    """CRM view: KPIs, customers with won/lost, quote history, bookings."""
    c = conn()
    won = ",".join(f"'{s}'" for s in WON)
    lost = ",".join(f"'{s}'" for s in LOST)
    customers = _rows(
        c.execute(
            f"""
            SELECT s.customer,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN s.first_reply_at IS NOT NULL THEN 1 ELSE 0 END) AS quoted,
                   SUM(CASE WHEN s.status IN ({won}) THEN 1 ELSE 0 END) AS won,
                   SUM(CASE WHEN s.status IN ({lost}) THEN 1 ELSE 0 END) AS lost,
                   SUM(CASE WHEN s.status IN ('new','pricing','quoted') THEN 1 ELSE 0 END) AS open,
                   (SELECT COALESCE(SUM(b.price_minor),0) FROM bookings b
                     JOIN sessions s2 ON s2.id = b.session_id
                     WHERE s2.customer = s.customer AND b.status != 'failed') AS booked_minor,
                   MAX(s.updated_at) AS last_contact
            FROM sessions s WHERE s.kind = 'rfq' AND s.customer IS NOT NULL
            GROUP BY s.customer ORDER BY last_contact DESC LIMIT ?
            """,
            (limit,),
        )
    )
    quotes = _rows(
        c.execute(
            f"""
            SELECT q.session_id, s.customer, s.subject, q.airline, q.origin, q.destination,
                   q.total_minor, q.currency, q.transit_days, q.source, q.sent_at, s.status,
                   CASE WHEN s.status IN ({won}) THEN 'won'
                        WHEN s.status IN ({lost}) THEN 'lost' ELSE 'open' END AS outcome,
                   (SELECT verdict FROM board_results br WHERE br.session_id = q.session_id
                     ORDER BY br.fetched_at DESC LIMIT 1) AS verdict
            FROM quotes q JOIN sessions s ON s.id = q.session_id
            WHERE q.id IN (SELECT MIN(id) FROM quotes GROUP BY session_id)
            ORDER BY q.sent_at DESC LIMIT ?
            """,
            (limit,),
        )
    )
    bookings = _rows(
        c.execute(
            "SELECT b.*, s.customer, s.subject FROM bookings b"
            " JOIN sessions s ON s.id = b.session_id ORDER BY b.created_at DESC LIMIT ?",
            (limit,),
        )
    )
    totals = (
        _one(
            c.execute(
                f"""
            SELECT COUNT(*) AS requests,
                   SUM(CASE WHEN first_reply_at IS NOT NULL THEN 1 ELSE 0 END) AS quoted,
                   SUM(CASE WHEN status IN ({won}) THEN 1 ELSE 0 END) AS won,
                   SUM(CASE WHEN status IN ({lost}) THEN 1 ELSE 0 END) AS lost,
                   COUNT(DISTINCT customer) AS customers
            FROM sessions WHERE kind = 'rfq'
            """
            )
        )
        or {}
    )
    booked = _one(
        c.execute("SELECT COALESCE(SUM(price_minor),0) AS v FROM bookings WHERE status != 'failed'")
    )
    return {
        "pricing": pricing_history(),
        "limit": limit,
        "totals": {k: (v or 0) for k, v in totals.items()},
        "booked_minor": booked["v"] if booked else 0,
        "customers": customers,
        "quotes": quotes,
        "bookings": bookings,
    }


def pricing_history(limit: int = 100) -> list[dict[str, Any]]:
    """Every priced RFQ with what we could have quoted: all air options, the trucker bids,
    our choice and 5U's verdict. Basis for the CRM 'Preise' tab and pricing reviews."""
    c = conn()
    audits = _rows(
        c.execute(
            "SELECT e.session_id, e.payload_json, s.customer, s.created_at FROM events e"
            " JOIN sessions s ON s.id = e.session_id WHERE e.kind = 'pricing_audit'"
            " AND e.id = (SELECT MAX(id) FROM events WHERE session_id = e.session_id"
            " AND kind = 'pricing_audit') ORDER BY s.created_at DESC LIMIT ?",
            (limit,),
        )
    )
    board = _board_by_session(c)
    out = []
    for a in audits:
        p = json.loads(a["payload_json"] or "{}")
        sid = a["session_id"]
        req, sel = p.get("request") or {}, p.get("selected") or {}
        opts = [
            {
                "airline": o.get("airline"),
                "source": o.get("source"),
                "transit_days": o.get("transit_days"),
                "eur_minor": o.get("total_eur_minor"),
            }
            for o in p.get("options") or []
            if o.get("total_eur_minor") is not None
        ]
        from app.pricing.reference import carrier_by_name

        bids = []
        for e in _rows(
            c.execute(
                "SELECT message FROM events WHERE session_id = ? AND kind = 'bid_received'",
                (sid,),
            )
        ):
            name = e["message"].removeprefix("Gebot ").split(":")[0]
            carrier = carrier_by_name(name)
            bids.append({"name": name, "kind": carrier.kind if carrier else None})
        tr = p.get("trucking") or None
        from decimal import Decimal

        from app.pricing.pricing import to_minor

        truck_minor = to_minor(Decimal(str(tr["total_eur"]))) if tr else 0
        quote = _one(
            c.execute(
                "SELECT total_minor FROM quotes WHERE session_id = ? ORDER BY id LIMIT 1",
                (sid,),
            )
        )
        cheapest = min((o["eur_minor"] for o in opts), default=None)
        b = board.get(sid, {})
        out.append(
            {
                "session_id": sid,
                "created_at": a["created_at"],
                "customer": a["customer"],
                "origin": sel.get("origin"),
                "destination": sel.get("destination") or req.get("destination"),
                "preference": req.get("preference"),
                "weight_kg": req.get("weight_kg"),
                "chargeable_kg": sel.get("chargeable_kg"),
                "pickup": req.get("pickup_zip") or req.get("pickup_city"),
                "pickup_assumed": bool(req.get("pickup_assumed")),
                "selected": {
                    "airline": sel.get("airline"),
                    "source": sel.get("source"),
                    "transit_days": sel.get("transit_days"),
                    "eur_minor": sel.get("total_eur_minor"),
                },
                "options": sorted(opts, key=lambda o: o["eur_minor"]),
                "truck": {"name": tr["carrier"]["name"], "eur_minor": truck_minor} if tr else None,
                "truck_bids": sum(1 for b in bids if b["kind"] == "trucker"),
                "airline_bids": sum(1 for b in bids if b["kind"] == "airline"),
                "quote_minor": quote["total_minor"] if quote else None,
                "cheapest_air_minor": cheapest,
                "verdict": b.get("verdict"),
                "reason": b.get("reason"),
            }
        )
    return out


def customer_sessions(customer: str) -> list[dict[str, Any]]:
    rows = _rows(
        conn().execute(
            "SELECT s.id, s.kind, s.subject, s.status, s.created_at, s.first_reply_at,"
            " (SELECT total_minor FROM quotes q WHERE q.session_id = s.id ORDER BY q.id LIMIT 1)"
            " AS quote_total_minor,"
            " (SELECT currency FROM quotes q WHERE q.session_id = s.id ORDER BY q.id LIMIT 1)"
            " AS quote_currency FROM sessions s WHERE s.customer = ? ORDER BY s.created_at DESC",
            (customer,),
        )
    )
    return rows


# ---------------------------------------------------------------- overview (dashboard v2)

ACTIVITY_KINDS = (
    "quoted",
    "booked",
    "board_verdict",
    "error",
    "human_action",
    "status",
)


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def clarification_cap_open(limit: int = 20) -> list[dict[str, Any]]:
    """CLARIFY_CAP_RESOLVE: sessions whose two questions are spent with items still open and
    no reply to the customer yet; the latest `clarification_cap_open` message names them."""
    return _rows(
        conn().execute(
            "SELECT e.session_id, e.message, s.subject, s.customer, s.status, s.deadline_at"
            " FROM events e JOIN sessions s ON s.id = e.session_id"
            " WHERE e.kind = 'clarification_cap_open' AND s.first_reply_at IS NULL"
            " AND e.id = (SELECT MAX(x.id) FROM events x WHERE x.session_id = e.session_id"
            " AND x.kind = 'clarification_cap_open') ORDER BY e.id DESC LIMIT ?",
            (limit,),
        )
    )


def attention_items(limit: int = 20) -> list[dict[str, Any]]:
    """What needs a human: question cap reached without a quote (first), errors, expired, board
    rejections, paused/manual, close deadlines and acceptances that are not booked yet."""
    c = conn()
    items: list[dict[str, Any]] = [
        {
            "session_id": r["session_id"],
            "kind": "clarification_cap_open",
            "reason": r["message"],
            "subject": r["subject"],
            "customer": r["customer"],
            "status": r["status"],
        }
        for r in clarification_cap_open(limit)
    ]
    items += [
        {
            "session_id": row["session_id"],
            "kind": "case_worker",
            "reason": "Externer Ausgang unklar"
            if row["status"] == "unknown"
            else "Fallbearbeitung benötigt Prüfung",
            "subject": row["subject"],
            "customer": row["customer"],
            "status": row["status"],
        }
        for row in _rows(
            c.execute(
                "SELECT j.session_id,j.status,s.subject,s.customer FROM case_jobs j "
                "JOIN sessions s ON s.id=j.session_id WHERE j.status='unknown' OR "
                "(j.status='review' AND j.id=(SELECT MAX(n.id) FROM case_jobs n WHERE n.session_id=j.session_id)) "
                "ORDER BY j.updated_at DESC LIMIT ?",
                (limit,),
            )
        )
    ]
    items += [
        {
            "session_id": r["session_id"],
            "kind": "reply_workflow",
            "reason": " · ".join(json.loads(r["open_json"])) or "Kundenanliegen benötigt Prüfung",
            "subject": r["subject"],
            "customer": r["customer"],
            "status": r["phase"],
        }
        for r in _rows(
            c.execute(
                "SELECT w.*,s.subject,s.customer FROM thread_workflow w JOIN sessions s ON s.id=w.session_id WHERE w.phase='needs_review' ORDER BY w.updated_at DESC LIMIT ?",
                (limit,),
            )
        )
    ]
    items += [
        {
            "session_id": q["session_id"],
            "mail_id": q["mail_id"],
            "kind": "quarantine",
            "reason": "Phishing-Verdacht: keine Antwort gesendet",
            "subject": q["subject"],
            "customer": q["sender"],
            "status": "quarantined",
            "reasons": q["reasons"],
        }
        for q in quarantined(limit)
    ]
    items += [
        {
            "session_id": None,
            "mail_id": o["mail_id"],
            "kind": ORPHAN_REPLY,
            "reason": "Kundenantwort ohne bekannten Thread: Session zuordnen",
            "subject": o["subject"],
            "customer": o["sender"],
            "status": "unassigned",
        }
        for o in orphan_replies(limit)
    ]
    for s in list_sessions(attention=True, limit=limit):
        reason = (
            "Fehler" if s["status"] == "error" else "pausiert" if s["paused"] else "Deadline < 90 s"
        )
        validation = validation_get(s["id"])
        if validation and validation["state"] in (
            "awaiting_clarification",
            "manual_review",
            "resolving",
        ):
            reason = (
                "Rückfrage offen"
                if validation["state"] == "awaiting_clarification"
                else "Eingangsdaten prüfen"
            )
        items.append(
            {
                "session_id": s["id"],
                "kind": "attention",
                "reason": reason,
                "subject": s["subject"],
                "customer": s["customer"],
                "status": s["status"],
            }
        )
    for r in _rows(
        c.execute(
            "SELECT id, subject, customer, status FROM sessions WHERE status = 'expired'"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
    ):
        items.append(
            {
                "session_id": r["id"],
                "kind": "expired",
                "reason": "ohne Quote abgelaufen",
                "subject": r["subject"],
                "customer": r["customer"],
                "status": r["status"],
            }
        )
    for r in _rows(
        c.execute(
            "SELECT b.round_id, b.session_id, b.verdict, b.reason, b.cause_json, s.subject,"
            " s.customer, s.status FROM board_results b LEFT JOIN sessions s"
            " ON s.id = b.session_id WHERE b.verdict NOT IN ('accepted','open','pending')"
            " AND (b.session_id IS NOT NULL OR b.verdict = ?)"
            " ORDER BY b.fetched_at DESC LIMIT ?",
            (NO_ANSWER, limit),
        )
    ):
        cause = json.loads(r["cause_json"]) if r["cause_json"] else None
        items.append(
            {
                "session_id": r["session_id"],
                "kind": "board",
                "round_id": r["round_id"],
                "reason": f"5U: {r['verdict']}"
                + (f" ({r['reason']})" if r["reason"] else "")
                + (f" · {cause['summary']}" if cause and cause.get("summary") else ""),
                "cause": cause,
                "subject": r["subject"],
                "customer": r["customer"],
                "status": r["status"],
            }
        )
    for r in _rows(
        c.execute(
            """
        SELECT s.id, s.subject, s.customer, s.status FROM sessions s
        WHERE s.kind = 'rfq' AND s.status IN ('quoted','accepted') AND s.first_reply_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.session_id = s.id AND b.status != 'failed')
          AND EXISTS (SELECT 1 FROM mails m WHERE m.session_id = s.id AND m.direction = 'in'
                      AND m.received_at >= s.first_reply_at AND m.category = 'customer')
        ORDER BY s.updated_at DESC LIMIT ?
        """,
            (limit,),
        )
    ):
        items.append(
            {
                "session_id": r["id"],
                "kind": "to_book",
                "reason": "Kunde hat geantwortet, noch nicht gebucht",
                "subject": r["subject"],
                "customer": r["customer"],
                "status": r["status"],
            }
        )
    for r in _rows(
        c.execute(
            "SELECT e.session_id, MAX(e.id) AS last, s.subject, s.customer, s.status"
            " FROM events e JOIN sessions s ON s.id = e.session_id WHERE e.kind = 'needs_human'"
            " GROUP BY e.session_id ORDER BY last DESC LIMIT ?",
            (limit,),
        )
    ):
        items.append(
            {
                "session_id": r["session_id"],
                "kind": "needs_human",
                "reason": "Rückfrage ohne brauchbare Antwort: prüfen",
                "subject": r["subject"],
                "customer": r["customer"],
                "status": r["status"],
            }
        )
    seen: set[tuple[Any, str]] = set()
    out = []
    for i in items:
        # phishing items carry mail_id, unanswered rounds without a session their round_id
        key = (i.get("mail_id") or i["session_id"] or i.get("round_id"), i["kind"])
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out[:limit]


def overview(hours: int = 6) -> dict[str, Any]:
    """KPIs for the window (hours, 0 = everything), the previous window for trends, sparkline
    series per bucket, ring values, attention items and recent activity."""
    c = conn()
    now_dt = datetime.now(UTC)
    sessions = _rows(
        c.execute(
            "SELECT id, kind, status, created_at, first_reply_at FROM sessions WHERE kind = 'rfq'"
        )
    )
    llm = {
        r["session_id"]: r["v"]
        for r in _rows(
            c.execute(
                "SELECT session_id, SUM(cost_micro_usd) AS v FROM llm_calls GROUP BY session_id"
            )
        )
    }
    bookings = _rows(
        c.execute(
            "SELECT session_id, price_minor, status, created_at FROM bookings WHERE status != 'failed'"
        )
    )
    board = {
        r["session_id"]: r["verdict"]
        for r in _rows(
            c.execute("SELECT session_id, verdict FROM board_results WHERE session_id IS NOT NULL")
        )
    }

    created = [_ts(s["created_at"]) for s in sessions]
    first = min((t for t in created if t), default=now_dt)
    if hours <= 0:
        start = first
        span = max(now_dt - start, timedelta(hours=1))
    else:
        span = timedelta(hours=hours)
        start = now_dt - span
    prev_start = start - span
    n_buckets = (
        12 if span <= timedelta(hours=6) else min(48, max(6, int(span.total_seconds() // 3600)))
    )
    width = span / n_buckets

    def window_stats(lo: datetime, hi: datetime) -> dict[str, Any]:
        ss = [s for s, t in zip(sessions, created) if t and lo <= t < hi]
        quoted = [s for s in ss if s["first_reply_at"]]
        resp = [
            (_ts(s["first_reply_at"]) - _ts(s["created_at"])).total_seconds()
            for s in quoted
            if _ts(s["first_reply_at"]) and _ts(s["created_at"])
        ]
        costs = [llm.get(s["id"], 0) or 0 for s in quoted]
        ids = {s["id"] for s in ss}
        bk = [b for b in bookings if b["session_id"] in ids]
        verdicts = [board[i] for i in ids if i in board]
        rated = [v for v in verdicts if v not in ("open", "pending")]
        return {
            "rfqs": len(ss),
            "quoted": len(quoted),
            "won": len({b["session_id"] for b in bk}),
            "booked_minor": sum(b["price_minor"] or 0 for b in bk),
            "avg_response_s": round(sum(resp) / len(resp), 1) if resp else None,
            "avg_cost_micro_usd": int(sum(costs) / len(costs)) if costs else None,
            "accepted": sum(1 for v in rated if v == "accepted"),
            "rated": len(rated),
            "lost": sum(1 for s in ss if s["status"] in LOST),
        }

    cur, prev = (
        window_stats(start, now_dt + timedelta(seconds=1)),
        window_stats(prev_start, start),
    )
    series = []
    for i in range(n_buckets):
        lo = start + width * i
        hi = lo + width if i < n_buckets - 1 else now_dt + timedelta(seconds=1)
        w = window_stats(lo, hi)
        series.append({"t": lo.isoformat(timespec="minutes"), **w})

    activity = _rows(
        c.execute(
            f"SELECT e.id, e.session_id, e.kind, e.level, e.message, e.created_at, s.subject,"
            f" s.customer, s.status FROM events e LEFT JOIN sessions s ON s.id = e.session_id"
            f" WHERE e.kind IN ({','.join('?' * len(ACTIVITY_KINDS))}) OR e.level = 'error'"
            f" ORDER BY e.id DESC LIMIT 12",
            ACTIVITY_KINDS,
        )
    )
    return {
        "hours": hours,
        "window": {
            "start": start.isoformat(timespec="minutes"),
            "end": now_dt.isoformat(timespec="minutes"),
        },
        "current": cur,
        "previous": prev,
        "series": series,
        "attention": attention_items(),
        "activity": activity,
        "global_pause": is_global_paused(),
    }


# ---------------------------------------------------------------- status board (Übersicht)

CHECK_WINDOW = timedelta(hours=2)
DECLINE_MARK = "Kundenantwort (Absage)"  # emitted by agents/booking.py on a customer "no"


def _level(ok: bool, warn: bool) -> str:
    return "ok" if ok else ("warn" if warn else "bad")


def _board_by_session(c: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for r in _rows(
        c.execute(
            "SELECT session_id, verdict, reason, raw_json FROM board_results"
            " WHERE session_id IS NOT NULL ORDER BY fetched_at"
        )
    ):
        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
        out[r["session_id"]] = {
            "verdict": r["verdict"],
            "reason": r["reason"],
            "booked": bool(raw.get("booked")),
        }
    return out


def _rfq_rows(c: sqlite3.Connection, since: str | None, limit: int | None) -> list[dict[str, Any]]:
    """One row per customer request with the outcome of every step (Quote → 5U → Buchung)."""
    q = (
        "SELECT s.id, s.customer, s.subject, s.status, s.paused, s.mode, s.request_json,"
        " s.created_at, s.deadline_at, s.first_reply_at,"
        " (SELECT total_minor FROM quotes WHERE session_id = s.id ORDER BY id LIMIT 1) AS quote_minor,"
        " (SELECT currency FROM quotes WHERE session_id = s.id ORDER BY id LIMIT 1) AS quote_currency,"
        " (SELECT airline FROM quotes WHERE session_id = s.id ORDER BY id LIMIT 1) AS airline,"
        " (SELECT COALESCE(SUM(cost_micro_usd),0) FROM llm_calls WHERE session_id = s.id) AS cost,"
        " (SELECT booking_reference FROM bookings WHERE session_id = s.id AND status != 'failed'"
        "  ORDER BY id LIMIT 1) AS booking_reference,"
        " (SELECT message FROM events WHERE session_id = s.id AND level != 'info'"
        "  AND kind != 'board_verdict' ORDER BY id DESC LIMIT 1) AS last_problem,"
        " EXISTS (SELECT 1 FROM events WHERE session_id = s.id AND message LIKE ?) AS declined,"
        " EXISTS (SELECT 1 FROM mails m WHERE m.session_id = s.id AND m.direction = 'in'"
        "  AND m.category = 'customer' AND s.first_reply_at IS NOT NULL"
        "  AND m.received_at >= s.first_reply_at) AS customer_replied"
        " FROM sessions s WHERE s.kind = 'rfq'"
    )
    args: list[Any] = [DECLINE_MARK + "%"]
    if since:
        q += " AND s.created_at >= ?"
        args.append(since)
    q += " ORDER BY s.created_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    board = _board_by_session(c)
    now_dt = datetime.now(UTC)
    rows = []
    for r in _rows(c.execute(q, args)):
        req = json.loads(r.pop("request_json") or "null") or {}
        created, first = _ts(r["created_at"]), _ts(r["first_reply_at"])
        deadline = _ts(r["deadline_at"])
        b = board.get(r["id"], {})
        response_s = (first - created).total_seconds() if first and created else None
        if first:
            quote = {
                "state": "ok" if (response_s or 0) <= 360 else "warn",
                "label": secs_label(response_s),
            }
        elif r["status"] in ("new", "pricing") and deadline and deadline > now_dt:
            quote = {"state": "run", "label": "läuft"}
        else:
            quote = {"state": "bad", "label": r["last_problem"] or "keine Quote"}
        v = b.get("verdict")
        if v == "accepted":
            five = {"state": "ok", "label": "akzeptiert"}
        elif v in (None, "open", "pending"):
            five = {
                "state": "run" if first else "none",
                "label": "offen" if first else "–",
            }
        else:
            five = {
                "state": "bad",
                "label": v + (f" ({b['reason']})" if b.get("reason") else ""),
            }
        if r["booking_reference"] or b.get("booked"):
            book = {"state": "ok", "label": r["booking_reference"] or "gebucht"}
        elif r["declined"]:
            book = {"state": "none", "label": "Kunde lehnt ab"}
        elif r["customer_replied"]:
            book = {"state": "bad", "label": "Antwort, nicht gebucht"}
        elif v not in (None, "open", "pending", "accepted"):
            book = {"state": "none", "label": "–"}
        elif first and now_dt - first > timedelta(minutes=30):
            book = {"state": "none", "label": "keine Zusage"}
        elif first:
            book = {"state": "run", "label": "wartet auf Kunde"}
        else:
            book = {"state": "none", "label": "–"}
        lane = " → ".join(x for x in (req.get("origin"), req.get("destination")) if x)
        rows.append(
            {
                **{
                    k: r[k]
                    for k in (
                        "id",
                        "customer",
                        "subject",
                        "status",
                        "created_at",
                        "quote_minor",
                        "quote_currency",
                        "airline",
                        "cost",
                    )
                },
                "lane": lane,
                "weight_kg": req.get("weight_kg"),
                "response_s": response_s,
                "deadline_at": r["deadline_at"],
                "paused": r["paused"],
                "mode": r["mode"],
                "steps": {"quote": quote, "board": five, "booking": book},
                "verdict": v,
                "declined": bool(r["declined"]),
                "customer_replied": bool(r["customer_replied"]),
                "booked": book["state"] == "ok",
            }
        )
    return rows


def secs_label(s: float | None) -> str:
    if s is None:
        return "–"
    return f"{round(s)} s" if s < 60 else f"{int(s // 60)}:{round(s % 60):02d} min"


_PROBLEM_NUMBER = re.compile(r"\d[\d.,:]*")


PROBLEMS_ACK_KEY = "problems.ack"
# the board watcher's old ERROR log line repeats the board_verdict event of the same round
_BOARD_LOG_ECHO = "app.ui.watchers: board: round "


def _problem_acks() -> dict[str, int]:
    try:
        raw = json.loads(kv_get(PROBLEMS_ACK_KEY) or "{}")
    except ValueError:
        return {}
    return (
        {str(k): int(v) for k, v in raw.items() if isinstance(v, int)}
        if isinstance(raw, dict)
        else {}
    )


def ack_problems(acks: dict[str, int]) -> int:
    """Mark problem groups as handled up to their latest event id; a newer event of the same
    group shows the group again. Keeps the newest 300 marks."""
    merged = _problem_acks()
    for key, event_id in acks.items():
        if isinstance(key, str) and isinstance(event_id, int) and 0 < len(key) <= 600:
            merged[key] = max(event_id, merged.get(key, 0))
    kept = dict(sorted(merged.items(), key=lambda kv: kv[1])[-300:])
    kv_set(PROBLEMS_ACK_KEY, json.dumps(kept))
    return len(acks)


def problems(hours: float = 3, limit: int = 400) -> dict[str, Any]:
    """Übersicht: every error and warning event of the last `hours`, grouped by kind and
    message with the numbers taken out (same failure, different session = one group).

    Echoes are left out: the board watcher's log line next to its 5U verdict, and an
    error status change of a session whose own error event names the cause. Groups marked
    handled stay hidden until a newer event arrives.

    Each group: key, level, kind, latest message, count, first/last time, affected sessions."""
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _rows(
        conn().execute(
            "SELECT e.id, e.session_id, e.kind, e.level, e.message, e.created_at, s.subject, "
            "s.customer FROM events e LEFT JOIN sessions s ON s.id = e.session_id "
            "WHERE e.level IN ('warn','error') AND e.created_at >= ? ORDER BY e.id DESC LIMIT ?",
            (since, limit),
        )
    )
    explained = {
        r["session_id"]
        for r in rows
        if r["level"] == "error" and r["session_id"] is not None and r["kind"] != Ev.STATUS
    }
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:  # newest first
        message = r["message"] or ""
        if r["kind"] == "log_error" and message.startswith(_BOARD_LOG_ECHO):
            continue
        if r["kind"] == Ev.STATUS and r["session_id"] in explained:
            continue
        key = (r["level"], r["kind"], _PROBLEM_NUMBER.sub("#", message))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "key": "|".join(key),
                "level": r["level"],
                "kind": r["kind"],
                "message": message,
                "count": 0,
                "last_at": r["created_at"],
                "first_at": r["created_at"],
                "last_event_id": r["id"],
                "sessions": [],
            }
        g["count"] += 1
        g["first_at"] = r["created_at"]
        sid = r["session_id"]
        if (
            sid is not None
            and all(x["id"] != sid for x in g["sessions"])
            and len(g["sessions"]) < 6
        ):
            g["sessions"].append({"id": sid, "subject": r["subject"], "customer": r["customer"]})
    acks = _problem_acks()
    handled = [g for g in groups.values() if acks.get(g["key"], 0) >= g["last_event_id"]]
    open_groups = [g for g in groups.values() if acks.get(g["key"], 0) < g["last_event_id"]]
    ordered = sorted(open_groups, key=lambda g: (g["level"] != "error", -g["last_event_id"]))
    return {
        "hours": hours,
        "handled": len(handled),
        "errors": sum(g["count"] for g in ordered if g["level"] == "error"),
        "warnings": sum(g["count"] for g in ordered if g["level"] == "warn"),
        "truncated": len(rows) >= limit,
        "groups": ordered,
    }


def status_overview() -> dict[str, Any]:
    """Übersicht: does each part of the pipeline work right now (last 2 h), the score
    estimate since start, the latest requests step by step, and what a human must do."""
    c = conn()
    now_dt = datetime.now(UTC)
    since = (now_dt - CHECK_WINDOW).isoformat(timespec="seconds")
    recent = _rfq_rows(c, since, None)
    every = _rfq_rows(c, None, None)
    checks: list[dict[str, Any]] = []

    last_in = _one(c.execute("SELECT MAX(received_at) AS t FROM mails WHERE direction = 'in'"))
    age = (now_dt - _ts(last_in["t"])).total_seconds() / 60 if last_in and last_in["t"] else None
    checks.append(
        {
            "key": "mail",
            "name": "Mail-Abruf",
            "level": "bad" if age is None else _level(age <= 8, age <= 15),
            "value": "keine Mail" if age is None else f"vor {int(age)} min",
            "hint": "Alle 6 min kommt eine RFQ. Länger still heißt: Poller steht oder Server ist aus.",
        }
    )

    done = [r for r in recent if r["steps"]["quote"]["state"] != "run"]
    quoted = [r for r in done if r["steps"]["quote"]["state"] in ("ok", "warn")]
    missed = len(done) - len(quoted)
    checks.append(
        {
            "key": "quotes",
            "name": "Anfragen beantwortet",
            "level": "ok" if not done else _level(missed == 0, missed <= 1),
            "value": f"{len(quoted)} / {len(done)}" if done else "–",
            "hint": "RFQs der letzten 2 h mit Quote vor Ablauf. Jede verpasste ist verloren.",
        }
    )

    resp = [r["response_s"] for r in recent if r["response_s"] is not None]
    avg_resp = sum(resp) / len(resp) if resp else None
    checks.append(
        {
            "key": "speed",
            "name": "Antwortzeit",
            "level": "ok" if avg_resp is None else _level(avg_resp <= 120, avg_resp <= 240),
            "value": secs_label(avg_resp),
            "hint": "Ø bis zur Quote. ≤ 2 min volle 15 Zeitpunkte, ab 6 min null.",
        }
    )

    rated = [r for r in recent if r["verdict"] not in (None, "open", "pending")]
    acc = sum(1 for r in rated if r["verdict"] == "accepted")
    checks.append(
        {
            "key": "price",
            "name": "5U akzeptiert",
            "level": "ok" if not rated else _level(acc == len(rated), acc / len(rated) >= 0.6),
            "value": f"{acc} / {len(rated)}" if rated else "–",
            "hint": "Urteil vom Leaderboard. too_high = Preis > 5 % über Bestpreis, no answer = nichts gesendet.",
        }
    )

    costs = [r["cost"] for r in recent if r["steps"]["quote"]["state"] in ("ok", "warn")]
    avg_cost = sum(costs) / len(costs) if costs else None
    checks.append(
        {
            "key": "cost",
            "name": "LLM-Kosten je Quote",
            "level": "ok" if avg_cost is None else _level(avg_cost <= 2000, avg_cost <= 10000),
            "value": "–" if avg_cost is None else f"${avg_cost / 1e6:.4f}",
            "hint": "0 $ = 40 Punkte, 0,10 $ = 0. Mindestens ein Gateway-Call pro Quote nötig.",
        }
    )

    pending_book = [r for r in recent if r["steps"]["booking"]["state"] == "bad"]
    booked = sum(1 for r in recent if r["booked"])
    checks.append(
        {
            "key": "booking",
            "name": "Buchungen",
            "level": _level(not pending_book, False),
            "value": f"{booked} gebucht" + (f", {len(pending_book)} offen" if pending_book else ""),
            "hint": "Kunde hat zugesagt → Buchung per Rate-ID. Offen = Zusage ohne Buchung.",
        }
    )

    trk = _rows(
        c.execute(
            "SELECT s.id, EXISTS (SELECT 1 FROM mails m WHERE m.session_id = s.id AND m.direction = 'out')"
            " AS answered, EXISTS (SELECT 1 FROM events e WHERE e.session_id = s.id"
            " AND e.message LIKE '%keine Buchung gefunden%') AS not_found"
            " FROM sessions s WHERE s.kind = 'tracking' AND s.created_at >= ?",
            (since,),
        )
    )
    unanswered = sum(1 for t in trk if not t["answered"])
    not_found = sum(1 for t in trk if t["not_found"])
    checks.append(
        {
            "key": "tracking",
            "name": "Tracking-Antworten",
            "level": "ok"
            if not trk
            else _level(unanswered == 0 and not_found == 0, unanswered == 0),
            "value": f"{len(trk) - unanswered} / {len(trk)}"
            + (f", {not_found} ohne Treffer" if not_found else "")
            if trk
            else "–",
            "hint": "Antwort aus unserem CRM. Ohne Treffer = keine passende Buchung gefunden, Rückfrage gesendet.",
        }
    )

    bids = {
        r["kind"]: r["n"]
        for r in _rows(
            c.execute(
                "SELECT kind, COUNT(*) AS n FROM events WHERE kind IN ('bid_received','bid_unreadable',"
                "'bid_declined') AND created_at >= ? GROUP BY kind",
                (since,),
            )
        )
    }
    got, bad_bids = bids.get("bid_received", 0), bids.get("bid_unreadable", 0)
    checks.append(
        {
            "key": "bids",
            "name": "Carrier-Gebote gelesen",
            "level": "ok" if not (got + bad_bids) else _level(bad_bids == 0, got >= bad_bids),
            "value": f"{got} / {got + bad_bids}" if got + bad_bids else "–",
            "hint": "Gebote mit erkanntem Preis. Nicht gelesen sind oft Absagen „lane not served“.",
        }
    )

    # same view as "Fehler & Warnungen": no echoes, nothing a person marked handled
    errors = [
        g
        for g in problems(hours=CHECK_WINDOW.total_seconds() / 3600)["groups"]
        if g["level"] == "error" and g["kind"] not in ("board_verdict", "warn")
    ]
    unassigned = _one(
        c.execute(
            "SELECT COUNT(*) AS n FROM mails WHERE session_id IS NULL AND direction = 'in'"
            " AND received_at >= ?",
            (since,),
        )
    )["n"]
    checks.append(
        {
            "key": "errors",
            "name": "Fehler · unzugeordnet",
            "level": _level(not errors and not unassigned, not errors),
            "value": f"{sum(g['count'] for g in errors)} · {unassigned}",
            "hint": (
                errors[0]["message"][:120]
                if errors
                else "Programmfehler im Log und Mails, die keiner Session zugeordnet werden konnten."
            ),
        }
    )

    # score since start: points per accepted quote (price 45 is only known as accepted y/n)
    all_rated = [r for r in every if r["verdict"] not in (None, "open", "pending")]
    accepted = [r for r in all_rated if r["verdict"] == "accepted"]
    acc_resp = [r["response_s"] for r in accepted if r["response_s"] is not None]
    acc_cost = [r["cost"] for r in accepted]
    t_avg = sum(acc_resp) / len(acc_resp) if acc_resp else None
    c_avg = sum(acc_cost) / len(acc_cost) if acc_cost else None
    time_pts = (
        None
        if t_avg is None
        else (15 if t_avg <= 120 else 0 if t_avg >= 360 else 15 * (360 - t_avg) / 240)
    )
    cost_pts = None if c_avg is None else max(0.0, 40 * (1 - c_avg / 100000))
    verdicts: dict[str, int] = {}
    for r in all_rated:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1

    # question cap reached, nothing sent: first, while the round can still be answered
    todo = [
        {
            "session_id": r["session_id"],
            "subject": r["subject"],
            "customer": r["customer"],
            "lane": None,
            "reason": r["message"],
            "tone": "red",
        }
        for r in clarification_cap_open()
        if r["deadline_at"] and _ts(r["deadline_at"]) > now_dt
    ]
    for r in every:
        st = r["steps"]
        if r["paused"] or r["mode"] == "manual":
            if st["quote"]["state"] == "run" or (r["mode"] == "manual" and not r["response_s"]):
                todo.append(
                    {
                        **_todo(r),
                        "reason": "pausiert / übernommen – antworte selbst",
                        "tone": "orange",
                    }
                )
        elif st["quote"]["state"] == "run" and r["deadline_at"]:
            left = (_ts(r["deadline_at"]) - now_dt).total_seconds()
            if left < 120:
                todo.append(
                    {
                        **_todo(r),
                        "reason": f"noch {int(max(left, 0))} s, keine Quote",
                        "tone": "red",
                    }
                )
        if st["booking"]["state"] == "bad":
            todo.append(
                {
                    **_todo(r),
                    "reason": "Kunde hat geantwortet, nicht gebucht",
                    "tone": "blue",
                }
            )

    todo += [
        {
            "session_id": None,
            "subject": o["subject"],
            "customer": o["sender"],
            "lane": None,
            "mail_id": o["mail_id"],
            "reason": f"Mail {o['mail_id']}: Kundenantwort ohne bekannten Thread",
            "tone": "red",
        }
        for o in orphan_replies()
    ]
    for r in unanswered_rounds(limit=50):
        if not r["sent_at"] or r["sent_at"] < since:
            continue
        todo.append(
            {
                "session_id": r["session_id"],
                "subject": r["subject"] or f"Runde {r['round_id']}",
                "customer": r["customer"],
                "lane": None,
                "round_id": r["round_id"],
                "reason": f"Runde {r['round_id']} unbeantwortet: {r['cause'].get('summary')}",
                "tone": "red",
            }
        )

    return {
        "checks": checks,
        "window_hours": int(CHECK_WINDOW.total_seconds() // 3600),
        "score": {
            "rfqs": len(every),
            "rated": len(all_rated),
            "accepted": len(accepted),
            "verdicts": verdicts,
            "time_pts": time_pts,
            "cost_pts": cost_pts,
            "avg_response_s": t_avg,
            "avg_cost_micro_usd": c_avg,
            "per_quote": None if time_pts is None or cost_pts is None else 45 + time_pts + cost_pts,
            "booked": sum(1 for r in every if r["booked"]),
        },
        "requests": every[:12],
        "todo": todo,
        "quarantine": quarantined(20),
        "global_pause": is_global_paused(),
    }


def _todo(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": r["id"],
        "subject": r["subject"],
        "customer": r["customer"],
        "lane": r["lane"],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(f"DB ready at {_db_path}")


# ---------------------------------------------------------------- versioned replies


def reply_workflow(sid: int) -> dict[str, Any] | None:
    row = _one(conn().execute("SELECT * FROM thread_workflow WHERE session_id=?", (sid,)))
    if row:
        row["open_items"] = json.loads(row.pop("open_json"))
        row["approval"] = json.loads(row.pop("approval_json") or "null")
    return row


def _reply_enroll(c: sqlite3.Connection, sid: int, mid: int) -> None:
    if c.execute("SELECT 1 FROM thread_workflow WHERE session_id=?", (sid,)).fetchone():
        return
    session = _one(c.execute("SELECT request_json FROM sessions WHERE id=?", (sid,)))
    quote = _one(
        c.execute("SELECT * FROM quotes WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,))
    )
    safe = bool(
        quote
        and quote["request_snapshot_json"]
        and session
        and json.loads(quote["request_snapshot_json"])
        == json.loads(session["request_json"] or "null")
        and not json.loads(session["request_json"] or "{}").get("additional_shipments")
    )
    c.execute(
        "INSERT INTO thread_workflow(session_id,request_version,active_quote_id,phase,last_mail_id,updated_at) VALUES (?,1,?,'interpreting',?,?)",
        (sid, quote["id"] if safe else None, mid, now()),
    )
    if session and session["request_json"]:
        c.execute(
            "INSERT OR IGNORE INTO request_revisions VALUES (?,1,?,?,?,?)",
            (sid, sid, session["request_json"], "{}", now()),
        )


def reply_enroll(sid: int, mid: int) -> dict[str, Any]:
    """Enrol on NEW input, without ever executing historical customer approvals."""
    with tx() as c:
        _reply_enroll(c, sid, mid)
    state = reply_workflow(sid)
    assert state is not None
    return state


def reply_phase(sid: int, phase: str, *, open_items: list[str] | None = None) -> None:
    with tx() as c:
        c.execute(
            "UPDATE thread_workflow SET phase=?, open_json=?, updated_at=? WHERE session_id=?",
            (phase, _json(open_items or []), now(), sid),
        )
        _emit(
            c,
            "reply_workflow",
            phase,
            sid,
            payload={"phase": phase, "open_items": open_items or []},
        )


def reply_revise(sid: int, mid: int, request: dict[str, Any], evidence: dict[str, Any]) -> int:
    with tx() as c:
        saved = _one(
            c.execute(
                "SELECT version FROM request_revisions WHERE session_id=? AND source_mail_id=?",
                (sid, mid),
            )
        )
        if saved:
            return int(saved["version"])
        state = _one(
            c.execute("SELECT request_version FROM thread_workflow WHERE session_id=?", (sid,))
        )
        if state is None:
            raise ValueError("thread not enrolled")
        version = int(state["request_version"]) + 1
        c.execute(
            "INSERT INTO request_revisions VALUES (?,?,?,?,?,?)",
            (sid, version, mid, _json(request), _json(evidence), now()),
        )
        c.execute(
            "UPDATE sessions SET request_json=?,updated_at=? WHERE id=?",
            (_json(request), now(), sid),
        )
        c.execute(
            "UPDATE thread_workflow SET request_version=?,active_quote_id=NULL,approval_json=NULL,phase='repricing',last_mail_id=?,open_json='[]',updated_at=? WHERE session_id=?",
            (version, mid, now(), sid),
        )
        _emit(
            c,
            "request_revised",
            "Sendungsdaten aktualisiert, bisheriges Angebot gesperrt",
            sid,
            payload={"version": version, "mail_id": mid, "changes": evidence},
        )
        return version


def reply_decision(
    sid: int, mid: int, decision: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if decision is not None:
        with tx() as c:
            state = reply_workflow(sid)
            cursor = c.execute(
                "INSERT OR IGNORE INTO reply_decisions VALUES (?,?,?,?,?)",
                (sid, mid, (state or {}).get("request_version", 1), _json(decision), now()),
            )
            if cursor.rowcount:
                _emit(
                    c,
                    "reply_decision",
                    "Kundenantwort ausgewertet",
                    sid,
                    payload={"mail_id": mid, "decision": decision},
                )
    row = _one(
        conn().execute(
            "SELECT decision_json FROM reply_decisions WHERE session_id=? AND mail_id=?", (sid, mid)
        )
    )
    return json.loads(row["decision_json"]) if row else None


def reply_enqueue(sid: int, mail: dict[str, Any], *, shadow: bool = False) -> None:
    with tx() as c:
        inserted = c.execute(
            "INSERT OR IGNORE INTO workflow_actions(session_id,mail_id,kind,payload_json,updated_at) VALUES (?,?,?,?,?)",
            (sid, mail["id"], "shadow" if shadow else "reply", _json(mail), now()),
        ).rowcount
        if (
            inserted
            and not shadow
            and c.execute("SELECT 1 FROM quotes WHERE session_id=?", (sid,)).fetchone()
        ):
            _reply_enroll(c, sid, mail["id"])
            c.execute(
                "UPDATE thread_workflow SET phase=CASE WHEN EXISTS(SELECT 1 FROM workflow_actions e WHERE e.session_id=thread_workflow.session_id AND e.status='dispatching') THEN 'needs_review' ELSE 'interpreting' END,approval_json=NULL,updated_at=? WHERE session_id=?",
                (now(), sid),
            )


def reply_claim(
    owner: str, timestamp: float, lease_s: float = 90, kind: str | None = None
) -> dict[str, Any] | None:
    with tx() as c:
        # A lease expiration cannot tell whether an external operation already happened.
        expired = _rows(
            c.execute(
                "SELECT * FROM workflow_actions WHERE kind IN ('reply','shadow') AND status='processing' AND lease_until<?",
                (timestamp,),
            )
        )
        for row in expired:
            ambiguous = c.execute(
                "SELECT 1 FROM workflow_actions WHERE session_id=? AND mail_id=? AND status='dispatching'",
                (row["session_id"], row["mail_id"]),
            ).fetchone()
            status = "unknown" if ambiguous else "pending"
            c.execute(
                "UPDATE workflow_actions SET status=?,lease_owner=NULL WHERE id=?",
                (status, row["id"]),
            )
            if ambiguous:
                c.execute(
                    "UPDATE thread_workflow SET phase='needs_review',open_json=? WHERE session_id=?",
                    (_json(["Unklarer externer Ausgang nach Neustart"]), row["session_id"]),
                )
                _emit(
                    c,
                    "reply_unknown",
                    "Externer Ausgang unklar, keine automatische Wiederholung",
                    row["session_id"],
                    level="error",
                )
        row = _one(
            c.execute(
                """SELECT a.* FROM workflow_actions a WHERE a.kind IN ('reply','shadow') AND a.status='pending' AND (? IS NULL OR a.kind=?)
          AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id=a.session_id AND (s.paused=1 OR s.mode='manual'))
          AND NOT EXISTS (SELECT 1 FROM workflow_actions e WHERE e.session_id=a.session_id AND e.status='dispatching')
          AND NOT EXISTS (SELECT 1 FROM workflow_actions b WHERE b.session_id=a.session_id
            AND b.kind IN ('reply','shadow') AND (b.status='processing' OR (b.status='pending' AND b.mail_id<a.mail_id)))
          ORDER BY a.mail_id,a.id LIMIT 1""",
                (kind, kind),
            )
        )
        if row:
            c.execute(
                "UPDATE workflow_actions SET status='processing',lease_owner=?,lease_until=?,updated_at=? WHERE id=?",
                (owner, timestamp + lease_s, now(), row["id"]),
            )
        return row


def reply_heartbeat(action_id: int, owner: str, timestamp: float) -> bool:
    with tx() as c:
        return (
            c.execute(
                "UPDATE workflow_actions SET lease_until=? WHERE id=? AND lease_owner=? AND status='processing'",
                (timestamp + 90, action_id, owner),
            ).rowcount
            == 1
        )


def reply_finish(
    action_id: int, owner: str, status: str = "done", error: str | None = None
) -> None:
    with tx() as c:
        c.execute(
            "UPDATE workflow_actions SET status=?,error=?,lease_owner=NULL,updated_at=? WHERE id=? AND lease_owner=?",
            (status, error, now(), action_id, owner),
        )


def reply_effect(
    sid: int,
    mid: int,
    kind: str,
    version: int,
    *,
    dispatch: bool = False,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO workflow_actions(session_id,mail_id,kind,version,updated_at) VALUES (?,?,?,?,?)",
            (sid, mid, kind, version, now()),
        )
        row = _one(
            c.execute(
                "SELECT * FROM workflow_actions WHERE session_id=? AND mail_id=? AND kind=? AND version=?",
                (sid, mid, kind, version),
            )
        )
        assert row is not None
        if result is not None:
            c.execute(
                "UPDATE workflow_actions SET status='done',result_json=?,updated_at=? WHERE id=?",
                (_json(result), now(), row["id"]),
            )
        elif dispatch:
            if row["status"] != "pending":
                raise ValueError("Action already dispatched or completed")
            c.execute(
                "UPDATE workflow_actions SET status='dispatching',updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
        return row


def reply_has_pending(sid: int) -> bool:
    return (
        conn()
        .execute(
            "SELECT 1 FROM workflow_actions WHERE session_id=? AND kind='reply' AND status IN ('pending','processing','unknown')",
            (sid,),
        )
        .fetchone()
        is not None
    )


def reply_current(sid: int, mid: int, version: int) -> bool:
    state = reply_workflow(sid)
    context = getattr(_local, "reply_context", None)
    if context:
        owner, action_id = context
        import time

        lease = _one(
            conn().execute(
                "SELECT lease_owner,lease_until,status FROM workflow_actions WHERE id=?",
                (action_id,),
            )
        )
        if (
            not lease
            or lease["lease_owner"] != owner
            or lease["lease_until"] <= time.time()
            or lease["status"] != "processing"
        ):
            return False
    if (
        not state
        or state["request_version"] != version
        or is_paused(sid)
        or is_manual(sid)
        or reply_blocked(mid)
    ):
        return False
    row = _one(
        conn().execute(
            "SELECT MAX(m.id) AS id FROM mails m JOIN sessions s ON s.id=m.session_id WHERE m.session_id=? AND m.direction='in' AND lower(m.sender)=lower(s.customer)",
            (sid,),
        )
    )
    # Also fence messages durably enqueued before the dispatcher has stored them as mails.
    for delivery in _rows(
        conn().execute(
            "SELECT raw_json FROM mail_delivery WHERE mail_id>? AND status IN ('pending','processing','stored','dispatching')",
            (mid,),
        )
    ):
        raw = json.loads(delivery["raw_json"])
        refs = raw.get("references") or []
        if sid in refs or raw.get("in_reply_to") == mid:
            return False
    return bool(row and row["id"] == mid)


def reply_approve(
    sid: int, mid: int, quote_id: int, evidence: str, conditions: list[dict[str, Any]]
) -> None:
    with tx() as c:
        state = reply_workflow(sid)
        if (
            not state
            or state["active_quote_id"] != quote_id
            or not reply_current(sid, mid, state["request_version"])
        ):
            raise ValueError("Stale approval")
        approval = {
            "mail_id": mid,
            "quote_id": quote_id,
            "version": state["request_version"],
            "evidence": evidence,
            "conditions": conditions,
        }
        c.execute(
            "UPDATE thread_workflow SET approval_json=?,phase='booking',open_json='[]',updated_at=? WHERE session_id=?",
            (_json(approval), now(), sid),
        )


def reply_authorized(sid: int, mid: int) -> bool | None:
    state = reply_workflow(sid)
    if not state:
        return None
    approval = state["approval"] or {}
    if approval.get("kind") == "price_change":
        booking = _one(
            conn().execute(
                "SELECT quote_id,customer_state,customer_total_minor FROM bookings WHERE session_id=?",
                (sid,),
            )
        )
        return bool(
            booking
            and booking["customer_state"] == "price_change_asked"
            and booking["quote_id"] == approval.get("quote_id")
            and booking["customer_total_minor"] == approval.get("customer_total_minor")
            and approval.get("mail_id") == mid
            and approval.get("version") == state["request_version"]
            and state["phase"] == "booking"
            and reply_current(sid, mid, state["request_version"])
        )
    return bool(
        approval.get("mail_id") == mid
        and approval.get("quote_id") == state["active_quote_id"]
        and approval.get("version") == state["request_version"]
        and state["phase"] == "booking"
        and reply_current(sid, mid, state["request_version"])
    )


def reply_revision_floor(sid: int) -> int:
    row = _one(
        conn().execute(
            "SELECT source_mail_id FROM request_revisions WHERE session_id=? ORDER BY version DESC LIMIT 1",
            (sid,),
        )
    )
    return int(row["source_mail_id"]) if row else 0


def reply_accepts_bid(sid: int, mid: int, subject: str, references: Sequence[int]) -> bool:
    state = reply_workflow(sid)
    if not state or state["request_version"] == 1:
        return True
    floor = reply_revision_floor(sid)
    if mid <= floor:
        return False
    asked = {
        r[0]
        for r in conn().execute(
            "SELECT id FROM mails WHERE session_id=? AND id>? AND direction='out' AND category='bid_request'",
            (sid, floor),
        )
    }
    return bool(asked.intersection(references) or f"[RV-{state['request_version']}]" in subject)


def reply_bind_booking(sid: int, quote_id: int) -> None:
    with tx() as c:
        c.execute(
            "UPDATE bookings SET quote_id=? WHERE session_id=? AND quote_id IS NULL",
            (quote_id, sid),
        )


def reply_seen(sid: int, mid: int) -> None:
    with tx() as c:
        c.execute("UPDATE thread_workflow SET last_mail_id=? WHERE session_id=?", (mid, sid))
        c.execute("INSERT OR IGNORE INTO clarification_answers VALUES (?,?)", (sid, mid))


def reply_quote_guard(sid: int, mid: int, version: int) -> None:
    if not reply_current(sid, mid, version):
        from app.hackloop import CustomerSendBlocked

        raise CustomerSendBlocked("Revised quote superseded by newer customer input")
    reply_effect(sid, mid, "quote_revision", version, dispatch=True)


def reply_interrupt(sid: int) -> None:
    with tx() as c:
        c.execute(
            "UPDATE thread_workflow SET phase='interpreting',approval_json=NULL,updated_at=? WHERE session_id=?",
            (now(), sid),
        )


def reply_set_context(owner: str, action_id: int) -> None:
    _local.reply_context = (owner, action_id)


def reply_clear_context() -> None:
    _local.reply_context = None


def reply_unknown_effect(sid: int, mid: int) -> bool:
    return (
        conn()
        .execute(
            "SELECT 1 FROM workflow_actions WHERE session_id=? AND mail_id=? AND status='dispatching'",
            (sid, mid),
        )
        .fetchone()
        is not None
    )


def reply_suspend_offer(sid: int) -> None:
    with tx() as c:
        c.execute(
            "UPDATE thread_workflow SET active_quote_id=NULL,approval_json=NULL WHERE session_id=?",
            (sid,),
        )


def reply_approve_price(sid: int, mid: int, evidence: str) -> None:
    with tx() as c:
        state = reply_workflow(sid)
        booking = _one(
            c.execute(
                "SELECT quote_id,customer_state,customer_total_minor FROM bookings WHERE session_id=?",
                (sid,),
            )
        )
        if (
            not state
            or not booking
            or booking["customer_state"] != "price_change_asked"
            or booking["customer_total_minor"] is None
            or not reply_current(sid, mid, state["request_version"])
        ):
            raise ValueError("Stale price approval")
        approval = {
            "kind": "price_change",
            "mail_id": mid,
            "quote_id": booking["quote_id"],
            "version": state["request_version"],
            "customer_total_minor": booking["customer_total_minor"],
            "evidence": evidence,
        }
        c.execute(
            "UPDATE thread_workflow SET approval_json=?,phase='booking',open_json='[]',updated_at=? WHERE session_id=?",
            (_json(approval), now(), sid),
        )
