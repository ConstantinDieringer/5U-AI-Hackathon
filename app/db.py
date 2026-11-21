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
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS tariff_jobs (
  mail_id INTEGER PRIMARY KEY,
  raw_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT
);
"""


# ---------------------------------------------------------------- connection

_local = threading.local()
_db_path: str = settings.db_path  # from .env via app/config.py (default data/forwarder.db)
_generation = 0  # bumped by init_db so threads reopen after a path change (tests)


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
    """Create the database file and schema. Call once at startup (and per test)."""
    global _db_path, _generation
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
    with tx() as migration:
        columns = {row["name"] for row in migration.execute("PRAGMA table_info(llm_calls)")}
        if "accounting_json" not in columns:
            migration.execute("ALTER TABLE llm_calls ADD COLUMN accounting_json TEXT")
        quote_columns = {row["name"] for row in migration.execute("PRAGMA table_info(quotes)")}
        if "air_total_minor" not in quote_columns:
            migration.execute("ALTER TABLE quotes ADD COLUMN air_total_minor INTEGER")
        if "air_currency" not in quote_columns:
            migration.execute("ALTER TABLE quotes ADD COLUMN air_currency TEXT")
    remap_unassigned()
    backfill_bookings_from_confirmations()
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


def kv_get(key: str, default: str | None = None) -> str | None:
    r = _one(conn().execute("SELECT value FROM kv WHERE key = ?", (key,)))
    return r["value"] if r else default


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
            " AND direction='in' AND id<? ORDER BY id DESC LIMIT 20",
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


_AP_TAG = re.compile(r"\bAP-(\d+)\b")
_BK_REF = re.compile(r"\bBK-[A-Z0-9]{4,}\b")


def _session_by_reference(c: sqlite3.Connection, subject: str, body: str) -> int | None:
    """Fallback for replies whose headers point to a mail we never stored (bid requests and
    bookings sent outside record_outgoing): our tag "[AP-<session>]" in the subject, our
    "Your reference: AP-<session>" in the body, or a known BK booking reference."""
    for text in (subject or "", body or ""):
        for m in _AP_TAG.finditer(text):
            if _one(c.execute("SELECT 1 FROM sessions WHERE id = ?", (int(m.group(1)),))):
                return int(m.group(1))
    for m in _BK_REF.finditer(f"{subject or ''} {body or ''}"):
        r = _one(
            c.execute(
                "SELECT session_id FROM bookings WHERE booking_reference = ? LIMIT 1",
                (m.group(0),),
            )
        )
        if r:
            return int(r["session_id"])
    return None


def remap_unassigned() -> int:
    """Attach stored unassigned incoming mails that carry one of our references. Idempotent;
    runs at startup so mails ingested before this fallback existed show in their session."""
    with tx() as c:
        rows = _rows(
            c.execute(
                "SELECT id, subject, body FROM mails WHERE session_id IS NULL AND direction = 'in'"
            )
        )
        n = 0
        for r in rows:
            sid = _session_by_reference(c, r["subject"], r["body"])
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
            " AND m.body LIKE '%Your reference: AP-%'"
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
            continue
        price = found["price"]
        minor = round(float(price.group(1).replace(",", "")) * 100) if price else None
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
) -> int | None | tuple[int | None, bool]:
    """Store an incoming mail and return its session id (None = unassigned).

    Idempotent: a mail id seen before changes nothing and returns its stored session.
    """
    received_at = received_at or now()
    refs = [int(x) for x in references]
    thread_root = refs[0] if refs else mail_id
    with tx() as c:
        known = _one(c.execute("SELECT session_id FROM mails WHERE id = ?", (mail_id,)))
        if known:
            return (known["session_id"], False) if report_new else known["session_id"]
        session_id = _resolve_session(c, in_reply_to, refs)
        if (
            session_id is None
            and not refs
            and in_reply_to is None
            and category not in SESSION_CATEGORIES
        ):
            session_id = _session_by_subject(c, subject, body)
        if session_id is None and category not in SESSION_CATEGORIES:
            session_id = _session_by_reference(c, subject, body)
        created = False
        if session_id is None and category in SESSION_CATEGORIES:
            session_id = mail_id
            created = True
            deadline = (_parse(received_at) + DEADLINE).isoformat(timespec="seconds")
            c.execute(
                "INSERT INTO sessions(id, kind, customer, subject, status, deadline_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    SESSION_CATEGORIES[category],
                    sender,
                    subject,
                    "new",
                    deadline if SESSION_CATEGORIES[category] == "rfq" else None,
                    received_at,
                    now(),
                ),
            )
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
    return (session_id, True) if report_new else session_id


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


def list_sessions(
    status: str | None = None,
    q: str | None = None,
    attention: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Sessions for the list view. Attention first (error, paused, deadline close), then by
    deadline, then most recently updated."""
    sql = """
    SELECT s.*,
      (SELECT message FROM events e WHERE e.session_id = s.id ORDER BY e.id DESC LIMIT 1) AS last_event,
      (SELECT COALESCE(SUM(cost_micro_usd),0) FROM llm_calls l WHERE l.session_id = s.id) AS llm_cost_micro_usd,
      (SELECT total_minor FROM quotes qq WHERE qq.session_id = s.id ORDER BY qq.id LIMIT 1) AS quote_total_minor,
      (SELECT currency FROM quotes qq WHERE qq.session_id = s.id ORDER BY qq.id LIMIT 1) AS quote_currency,
      CASE WHEN s.status = 'error' OR s.paused = 1
             OR EXISTS (SELECT 1 FROM rfq_validation v WHERE v.session_id=s.id
                        AND v.state IN ('awaiting_clarification','manual_review','resolving'))
             OR (s.status IN ('new','pricing') AND s.deadline_at IS NOT NULL
                 AND (julianday(s.deadline_at) - julianday('now')) * 86400 < ?)
           THEN 1 ELSE 0 END AS needs_attention
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
    sql += """ ORDER BY needs_attention DESC,
               CASE WHEN s.status IN ('new','pricing') THEN s.deadline_at END ASC,
               s.updated_at DESC LIMIT ?"""
    args.append(limit)
    rows = _rows(conn().execute(sql, args))
    for r in rows:
        r.pop("request_json", None)
    return rows


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
            "SELECT id, sender, body, received_at FROM mails"
            " WHERE direction = 'in' AND instr(lower(body), lower(?)) > 0 ORDER BY id LIMIT ?",
            (text, limit),
        )
    )


def bid_request_recipients(session_id: int) -> set[str]:
    """Carrier addresses already contacted for this RFQ, used for restart-safe fan-out."""
    rows = conn().execute(
        "SELECT recipient FROM mails WHERE session_id = ? AND direction = 'out'"
        " AND category = 'bid_request'",
        (session_id,),
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
) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO quotes(session_id, airline, origin, destination, total_minor, currency,"
            " transit_days, source, rate_id, valid_until, alternatives_json, mail_id, sent_at,"
            " air_total_minor, air_currency)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
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


def update_booking_status(reference: str, status: str) -> int | None:
    """reference = team_reference or booking_reference (e.g. from 'Booking BK-… confirmed')."""
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
            "UPDATE bookings SET status = ?, updated_at = ? WHERE team_reference = ?"
            " OR booking_reference = ?",
            (status, now(), reference, reference),
        )
        if status == "confirmed":
            c.execute(
                "UPDATE sessions SET status = 'confirmed', updated_at = ? WHERE id = ?",
                (now(), r["session_id"]),
            )
        _emit(c, Ev.BOOKED, f"Buchung {reference}: {status}", r["session_id"])
        return int(r["session_id"])


def tracking_records() -> list[dict[str, Any]]:
    """Every booking with what the tracking agent may tell: customer, subject, the request
    (JSON) and the first quote (route, transit days). Newest first."""
    rows = _rows(
        conn().execute(
            "SELECT b.session_id, b.team_reference, b.booking_reference, b.rate_id, b.airline,"
            " b.price_minor, b.currency, b.status, b.created_at AS booked_at, b.updated_at,"
            " s.customer, s.subject, s.request_json,"
            " q.origin, q.destination, q.transit_days"
            " FROM bookings b JOIN sessions s ON s.id = b.session_id"
            " LEFT JOIN quotes q ON q.id = (SELECT MIN(id) FROM quotes WHERE session_id = s.id)"
            " ORDER BY b.created_at DESC"
        )
    )
    for r in rows:
        r["request"] = json.loads(r.pop("request_json")) if r.get("request_json") else {}
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


def reserve_llm_call(session_id: int, limit: int) -> int | None:
    """Count one more gateway call for the session, persisted so a restart cannot reset it.
    Returns the new count, or None if the session already made `limit` calls. Calls logged in
    llm_calls before this counter existed count too."""
    key = f"llm_calls:{session_id}"
    with tx() as c:
        kv = _one(c.execute("SELECT value FROM kv WHERE key = ?", (key,)))
        logged = _one(
            c.execute(
                "SELECT COUNT(*) AS n FROM llm_calls WHERE session_id = ?",
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
) -> bool:
    """Upsert one board round. Returns True if the verdict is new or changed. A changed
    `failed` round is emitted as an error (shows in the UI attention list)."""
    with tx() as c:
        old = _one(c.execute("SELECT verdict FROM board_results WHERE round_id = ?", (round_id,)))
        c.execute(
            "INSERT INTO board_results(round_id, session_id, verdict, reason, points_json, raw_json,"
            " fetched_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET"
            " session_id = COALESCE(excluded.session_id, board_results.session_id),"
            " verdict = excluded.verdict, reason = excluded.reason,"
            " points_json = excluded.points_json, raw_json = excluded.raw_json,"
            " fetched_at = excluded.fetched_at",
            (round_id, session_id, verdict, reason, _json(points), _json(raw), now()),
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


# ---------------------------------------------------------------- watchers & stats


def session_by_created_at(ts: str) -> int | None:
    """Find the RFQ session whose first mail arrived at `ts` (board rows carry sent_at)."""
    r = _one(conn().execute("SELECT id FROM sessions WHERE created_at = ?", (ts,)))
    return int(r["id"]) if r else None


def expire_overdue() -> list[int]:
    """Mark open RFQ sessions past their deadline as expired. Returns their ids."""
    t = now()
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
            " LEFT JOIN llm_calls l ON l.session_id = q.session_id GROUP BY q.session_id)"
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
        truck_minor = round(float(tr["total_eur"]) * 100) if tr else 0
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


def attention_items(limit: int = 20) -> list[dict[str, Any]]:
    """What needs a human: errors, expired, board rejections, paused/manual, close deadlines and
    customer acceptances that are not booked yet."""
    c = conn()
    items: list[dict[str, Any]] = []
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
            "SELECT b.session_id, b.verdict, b.reason, s.subject, s.customer, s.status"
            " FROM board_results b JOIN sessions s ON s.id = b.session_id"
            " WHERE b.verdict NOT IN ('accepted','open','pending') ORDER BY b.fetched_at DESC LIMIT ?",
            (limit,),
        )
    ):
        items.append(
            {
                "session_id": r["session_id"],
                "kind": "board",
                "reason": f"5U: {r['verdict']}" + (f" ({r['reason']})" if r["reason"] else ""),
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
    seen: set[tuple[int, str]] = set()
    out = []
    for i in items:
        key = (i["session_id"], i["kind"])
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

    errors = _rows(
        c.execute(
            "SELECT message FROM events WHERE level = 'error' AND kind NOT IN ('board_verdict','warn')"
            " AND created_at >= ? ORDER BY id DESC",
            (since,),
        )
    )
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
            "value": f"{len(errors)} · {unassigned}",
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

    todo = []
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
