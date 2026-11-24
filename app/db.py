"""Storage. Neon (Postgres) in production via DATABASE_URL; SQLite only as an offline fallback
for local tests. All SQL in the app is written once with `?` placeholders and portable syntax;
this module adapts it to the backend.

Neon notes: use the *pooled* connection string (host contains '-pooler'); TLS with SNI is
always on; connections are re-opened automatically after Neon suspends an idle compute.
"""
import json
import re
import sqlite3
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

from . import config

_local = threading.local()
_write_lock = threading.RLock()
SCHEMA_VERSION = "4"

# Tables whose primary key is a generated integer id (INSERT returns it).
SERIAL_TABLES = {"applications", "claims", "gaps", "scores", "decisions", "corrections", "emails",
                 "calendar_events", "llm_calls", "memo_passages", "backtest", "alerts"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS founders (
    id TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT, linkedin TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS applications (
    id SERIALPK, ref TEXT UNIQUE, founder_id TEXT,
    company TEXT, website TEXT, sector TEXT, stage TEXT, city TEXT, source TEXT,
    deck_text TEXT, form_json TEXT, batch TEXT,
    status TEXT NOT NULL DEFAULT 'received', status_reason TEXT,
    attempts INTEGER DEFAULT 0, trace_id TEXT,
    created_at TEXT, updated_at TEXT, started_at TEXT);
CREATE INDEX IF NOT EXISTS ix_app_status ON applications(status);
CREATE TABLE IF NOT EXISTS claims (
    id SERIALPK, app_id INTEGER, cid TEXT, text TEXT, source_type TEXT, source_ref TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_claims_app ON claims(app_id);
CREATE TABLE IF NOT EXISTS gaps (id SERIALPK, app_id INTEGER, criterion TEXT, missing TEXT, cost TEXT);
CREATE TABLE IF NOT EXISTS scores (
    id SERIALPK, app_id INTEGER, criterion TEXT, score FLOAT8, ai_score FLOAT8,
    rationale TEXT, claim_ids TEXT, memo_refs TEXT, corrections_used TEXT, corrected_by TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_scores_app ON scores(app_id);
CREATE TABLE IF NOT EXISTS recommendations (
    app_id INTEGER PRIMARY KEY, total FLOAT8, coverage FLOAT8, verdict TEXT, gate_failures TEXT,
    case_for TEXT, case_against TEXT, two_liner TEXT, prompt_version TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    id SERIALPK, app_id INTEGER, decision TEXT, by_user TEXT, note TEXT, ai_verdict TEXT, agreed INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS corrections (
    id SERIALPK, app_id INTEGER, criterion TEXT, sector TEXT, keywords TEXT,
    original FLOAT8, corrected FLOAT8, reason TEXT, by_user TEXT, times_applied INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS emails (
    id SERIALPK, app_id INTEGER, direction TEXT, kind TEXT, subject TEXT, body TEXT, ai_draft TEXT,
    status TEXT, created_at TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS outreach (
    app_id INTEGER PRIMARY KEY, stage TEXT, follow_ups INTEGER DEFAULT 0,
    last_out_at TEXT, offered_slots TEXT, meeting_start TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS calendar_events (
    id SERIALPK, owner TEXT, title TEXT, start_at TEXT, end_at TEXT, app_id INTEGER, kind TEXT);
CREATE TABLE IF NOT EXISTS llm_calls (
    id SERIALPK, app_id INTEGER, trace_id TEXT, span_id TEXT, step TEXT,
    prompt_name TEXT, prompt_version TEXT, model TEXT, route TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd FLOAT8, latency_ms INTEGER,
    status TEXT, error TEXT, sent_payload TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_calls_created ON llm_calls(created_at);
CREATE INDEX IF NOT EXISTS ix_calls_app ON llm_calls(app_id);
CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY, trace_id TEXT, parent_id TEXT, app_id INTEGER, name TEXT, kind TEXT,
    start_at TEXT, end_at TEXT, status TEXT, input TEXT, output TEXT, metadata TEXT);
CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id);
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY, app_id INTEGER, name TEXT, start_at TEXT, end_at TEXT,
    status TEXT, prompt_versions TEXT, cost_usd FLOAT8 DEFAULT 0);
CREATE TABLE IF NOT EXISTS memos (
    id TEXT PRIMARY KEY, company TEXT, sector TEXT, stage TEXT, decision TEXT, date TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS memo_passages (id SERIALPK, memo_id TEXT, idx INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS backtest (
    id SERIALPK, run_id TEXT, memo_id TEXT, company TEXT, actual TEXT, predicted TEXT,
    total FLOAT8, agreed INTEGER, analysis TEXT, human_verdict TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    id SERIALPK, level TEXT, message TEXT, created_at TEXT, acknowledged INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_pages (
    url TEXT PRIMARY KEY, final_url TEXT, title TEXT, text TEXT, method TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)
"""


def is_pg() -> bool:
    return config.DATABASE_URL.startswith(("postgres://", "postgresql://"))


# ---------------- connections ----------------
def _pg_connect():
    import pg8000.dbapi
    u = urlparse(config.DATABASE_URL)
    qs = parse_qs(u.query)
    local = u.hostname in ("localhost", "127.0.0.1")
    sslmode = (qs.get("sslmode") or ["disable" if local else "require"])[0]
    kw = dict(user=unquote(u.username or ""), password=unquote(u.password or "") or None,
              host=u.hostname, port=u.port or 5432, database=(u.path or "/postgres").lstrip("/") or "postgres",
              timeout=30, application_name="allin-screening")
    if sslmode != "disable":
        kw["ssl_context"] = ssl.create_default_context()
    c = pg8000.dbapi.connect(**kw)
    c.autocommit = True
    return c


def _sqlite_connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _raw():
    c = getattr(_local, "conn", None)
    if c is None:
        c = _pg_connect() if is_pg() else _sqlite_connect()
        _local.conn = c
    return c


def _reset():
    c = getattr(_local, "conn", None)
    _local.conn = None
    try:
        if c:
            c.close()
    except Exception:
        pass


# ---------------- SQL adaptation ----------------
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def _adapt(sql: str) -> str:
    if not is_pg():
        return sql
    sql = sql.replace("%", "%%").replace("?", "%s")
    m = _INSERT_RE.match(sql)
    if m and m.group(1).lower() in SERIAL_TABLES and "RETURNING" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
    return sql


class Result:
    def __init__(self, cur, sql):
        self._rows = None
        self.lastrowid = None
        if is_pg():
            if cur.description:
                cols = [d[0] for d in cur.description]
                self._rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                if _INSERT_RE.match(sql) and self._rows and "id" in self._rows[0]:
                    self.lastrowid = self._rows[0]["id"]
            else:
                self._rows = []
        else:
            self._rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            self.lastrowid = cur.lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class Conn:
    """What `tx()` yields: execute() with `?` placeholders, rows as dicts, lastrowid on inserts."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        cur = self.raw.cursor()
        cur.execute(_adapt(sql), tuple(args))
        return Result(cur, sql)


def _execute(sql, args=()):
    for attempt in (1, 2):
        try:
            return Conn(_raw()).execute(sql, args)
        except Exception as e:
            if attempt == 1 and _is_disconnect(e):
                _reset()  # Neon suspended the compute / dropped the socket: reconnect once
                continue
            raise


def _is_disconnect(e) -> bool:
    n = type(e).__name__
    return n in ("InterfaceError", "OperationalError", "BrokenPipeError", "ConnectionResetError") and is_pg()


@contextmanager
def tx():
    """One write transaction. Postgres: row locks (see worker.claim_next). SQLite: serialised."""
    with _write_lock if not is_pg() else _nolock():
        for attempt in (1, 2):
            raw = _raw()
            try:
                raw.cursor().execute("BEGIN IMMEDIATE" if not is_pg() else "BEGIN")
                break
            except Exception as e:
                if attempt == 1 and _is_disconnect(e):
                    _reset()
                    continue
                raise
        c = Conn(raw)
        try:
            yield c
            raw.cursor().execute("COMMIT")
        except Exception:
            try:
                raw.cursor().execute("ROLLBACK")
            except Exception:
                _reset()
            raise


@contextmanager
def _nolock():
    yield


def q(sql, args=()):
    return _execute(sql, args).fetchall()


def q1(sql, args=()):
    return _execute(sql, args).fetchone()


def run(sql, args=()):
    if is_pg():
        return _execute(sql, args).lastrowid
    with tx() as c:
        return c.execute(sql, args).lastrowid


def init(force=False):
    if not force:
        try:
            if get_state("schema_version") == SCHEMA_VERSION:
                return
        except Exception:
            _reset()
    ddl = SCHEMA.replace("SERIALPK", "BIGSERIAL PRIMARY KEY" if is_pg() else "INTEGER PRIMARY KEY AUTOINCREMENT")
    if not is_pg():
        ddl = ddl.replace("FLOAT8", "REAL")
    for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
        _execute(stmt)
    set_state("schema_version", SCHEMA_VERSION)


def backend_label() -> str:
    if not is_pg():
        return "sqlite (local)"
    host = urlparse(config.DATABASE_URL).hostname or ""
    return "neon" if "neon.tech" in host else "postgres"


# ---- time (with a demo clock offset so follow-ups can be shown without waiting days)
_clock = {"off": 0.0, "at": 0.0}


def now() -> datetime:
    import time as _t
    if _t.time() - _clock["at"] > 3:  # avoid a database round trip for every timestamp
        _clock["off"] = float(get_state("clock_offset_days", "0") or 0)
        _clock["at"] = _t.time()
    return datetime.now(timezone.utc) + timedelta(days=_clock["off"])


def invalidate_clock():
    _clock["at"] = 0.0


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def get_state(k, default=None):
    r = q1("SELECT v FROM state WHERE k=?", (k,))
    return r["v"] if r else default


def set_state(k, v):
    run("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    if k == "clock_offset_days":
        invalidate_clock()


def jdump(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def jload(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
