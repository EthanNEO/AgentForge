import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH

_lock = threading.RLock()
_conn = None
_db_path_override = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL,
    run_id TEXT,
    node_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    reported_tokens INTEGER NOT NULL,
    responses INTEGER NOT NULL,
    metered_responses INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    latency REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_fallbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    phase TEXT,
    node_id TEXT,
    from_provider TEXT NOT NULL,
    from_model TEXT NOT NULL,
    category TEXT NOT NULL,
    wait_seconds REAL NOT NULL,
    to_provider TEXT,
    to_model TEXT,
    detail TEXT,
    cooldown_seconds REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT,
    role TEXT,
    description TEXT,
    model TEXT,
    skills TEXT,
    tools TEXT,
    cost_per_call REAL DEFAULT 0,
    avg_latency REAL DEFAULT 0,
    success INTEGER DEFAULT 0,
    failure INTEGER DEFAULT 0,
    consecutive_failure INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ready',
    created_at TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kb (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    source TEXT,
    content TEXT,
    tags TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_drafts (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task TEXT,
    scenario TEXT,
    status TEXT,
    mode TEXT,
    created_at TEXT,
    finished_at TEXT,
    stats TEXT,
    error TEXT,
    plan TEXT,
    plan_version INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS hidden_run_history (
    run_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    node_id TEXT,
    name TEXT,
    role TEXT,
    agent_id TEXT,
    agent_name TEXT,
    status TEXT,
    attempt INTEGER,
    verdict TEXT,
    tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    latency REAL DEFAULT 0,
    started_at TEXT,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    node_id TEXT,
    kind TEXT,
    level TEXT,
    message TEXT,
    payload TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    signature TEXT,
    strategy TEXT,
    patch TEXT,
    hits INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    provenance TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id,id);
CREATE TABLE IF NOT EXISTS worker_sessions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    role TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL,
    provenance TEXT NOT NULL,
    started_at REAL,
    deadline REAL,
    ended_at REAL
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _db_path_override or DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _ensure_columns(_conn)
        _conn.commit()
    return _conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Small additive migrations for databases created by earlier prototypes."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "plan" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN plan TEXT")
    if "plan_version" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN plan_version INTEGER DEFAULT 1")

    if "source_run" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN source_run TEXT")
    if "draft_id" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN draft_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_draft_id ON runs(draft_id)")

    fallback_columns = {row[1] for row in conn.execute("PRAGMA table_info(gateway_fallbacks)")}
    if fallback_columns and "cooldown_seconds" not in fallback_columns:
        conn.execute("ALTER TABLE gateway_fallbacks ADD COLUMN cooldown_seconds REAL")

    agent_columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
    if agent_columns and "total_tokens" not in agent_columns:
        conn.execute("ALTER TABLE agents ADD COLUMN total_tokens INTEGER DEFAULT 0")
    if agent_columns and "total_cost" not in agent_columns:
        conn.execute("ALTER TABLE agents ADD COLUMN total_cost REAL DEFAULT 0")


def reset_connection(db_path=None) -> None:
    """Close the process connection and optionally redirect the next one for tests."""
    global _conn, _db_path_override
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _db_path_override = db_path


def query(sql: str, args=()):
    with _lock:
        cur = get_conn().execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def one(sql: str, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args=()):
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


@contextmanager
def transaction():
    """Keep a group of writes on the shared connection atomic."""
    with _lock:
        conn = get_conn()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def json_load(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
