"""SQLite storage. Face embeddings are stored as float32 blobs; per-event search is
done in numpy, which is plenty fast for a few thousand faces per wedding."""
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    source_type   TEXT NOT NULL,           -- 'drive' | 'local'
    source_ref    TEXT NOT NULL,           -- drive folder id or local path
    pin           TEXT,                    -- optional PIN guests must enter
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|indexing|ready|error
    status_msg    TEXT,
    total_photos  INTEGER NOT NULL DEFAULT 0,
    done_photos   INTEGER NOT NULL DEFAULT 0,
    face_count    INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL,           -- drive file id or relative path
    name          TEXT NOT NULL,
    mime          TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|done|error
    error         TEXT,
    width         INTEGER,
    height        INTEGER,
    UNIQUE(event_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_photos_event ON photos(event_id, status);

CREATE TABLE IF NOT EXISTS faces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    photo_id      INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    embedding     BLOB NOT NULL,
    x INTEGER, y INTEGER, w INTEGER, h INTEGER,
    confidence    REAL
);
CREATE INDEX IF NOT EXISTS idx_faces_event ON faces(event_id);

CREATE TABLE IF NOT EXISTS guest_sessions (
    id            TEXT PRIMARY KEY,        -- random token
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_photos (
    session_id    TEXT NOT NULL REFERENCES guest_sessions(id) ON DELETE CASCADE,
    photo_id      INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    score         REAL NOT NULL,
    PRIMARY KEY (session_id, photo_id)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = _connect()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.close()
        _initialized = True


@contextmanager
def get_conn():
    init()
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction():
    with get_conn() as conn:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def now() -> float:
    return time.time()
