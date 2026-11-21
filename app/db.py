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


def init_db(path: str | None = None) -> None:
    """Create the database file and schema. Call once at startup (and per test)."""
    global _db_path, _generation
    if path is not None:
        _db_path = path
        _generation += 1
    d = os.path.dirname(_db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    c = conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)


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
        (session_id, kind, level, message, duration_ms, cost_micro_usd, _json(payload), now()),
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
                c, kind, message, session_id, level, duration_ms, cost_micro_usd, payload or None
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


def kv_set(key: str, value: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
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
) -> int | None:
    """Store an incoming mail and return its session id (None = unassigned).

    Idempotent: a mail id seen before changes nothing and returns its stored session.
    """
    received_at = received_at or now()
    refs = [int(x) for x in references]
    thread_root = refs[0] if refs else mail_id
    with tx() as c:
        known = _one(c.execute("SELECT session_id FROM mails WHERE id = ?", (mail_id,)))
        if known:
            return known["session_id"]
        session_id = _resolve_session(c, in_reply_to, refs)
        if (
            session_id is None
            and not refs
            and in_reply_to is None
            and category not in SESSION_CATEGORIES
        ):
            session_id = _session_by_subject(c, subject, body)
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
    return session_id


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
    s["board"] = _rows(c.execute("SELECT * FROM board_results WHERE session_id = ?", (session_id,)))
    return s


def list_sessions(
    status: str | None = None, q: str | None = None, attention: bool = False, limit: int = 200
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


def incoming_mails_containing(text: str, limit: int = 1000) -> list[dict[str, Any]]:
    """Incoming mails whose body contains `text` (case-insensitive), oldest first."""
    return _rows(
        conn().execute(
            "SELECT id, sender, body, received_at FROM mails"
            " WHERE direction = 'in' AND instr(lower(body), lower(?)) > 0 ORDER BY id LIMIT ?",
            (text, limit),
        )
    )


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
            "UPDATE sessions SET mode = 'manual', updated_at = ? WHERE id = ?", (now(), session_id)
        )
        _emit(
            c, Ev.HUMAN_ACTION, f"Von {who} übernommen – Agent schweigt", session_id, level="warn"
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
) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO quotes(session_id, airline, origin, destination, total_minor, currency,"
            " transit_days, source, rate_id, valid_until, alternatives_json, mail_id, sent_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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


def record_llm_call(
    session_id: int | None,
    *,
    purpose: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_micro_usd: int,
    latency_ms: int,
) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO llm_calls(session_id, purpose, model, prompt_tokens, completion_tokens,"
            " cost_micro_usd, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                purpose,
                model,
                prompt_tokens,
                completion_tokens,
                cost_micro_usd,
                latency_ms,
                now(),
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


def save_board_result(
    *,
    round_id: str,
    verdict: str | None,
    reason: str | None,
    points: dict[str, Any] | None,
    raw: dict[str, Any],
    session_id: int | None = None,
) -> bool:
    """Upsert one board round. Returns True if the verdict is new or changed."""
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
                level="warn"
                if verdict in ("rejected", "no_answer", "no answer", "voided")
                else "info",
                payload={"round_id": round_id, "points": points},
            )
        return changed


# ---------------------------------------------------------------- watchers & stats


def session_by_created_at(ts: str) -> int | None:
    """Find the RFQ session whose first mail arrived at `ts` (board rows carry sent_at)."""
    r = _one(conn().execute("SELECT id FROM sessions WHERE created_at = ?", (ts,)))
    return int(r["id"]) if r else None


def expire_overdue() -> list[int]:
    """Mark open RFQ sessions past their deadline as expired. Returns their ids."""
    t = now()
    with tx() as c:
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
                "UPDATE sessions SET status = 'expired', updated_at = ? WHERE id = ?", (t, sid)
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
        "totals": {k: (v or 0) for k, v in totals.items()},
        "booked_minor": booked["v"] if booked else 0,
        "customers": customers,
        "quotes": quotes,
        "bookings": bookings,
    }


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

ACTIVITY_KINDS = ("quoted", "booked", "board_verdict", "error", "human_action", "status")


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

    cur, prev = window_stats(start, now_dt + timedelta(seconds=1)), window_stats(prev_start, start)
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(f"DB ready at {_db_path}")
