"""The local index: one SQLite file with a full-text index over every message."""

import os
import sqlite3
from pathlib import Path

from brain.errors import IndexFormatError

VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    agent        TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    parent_id    TEXT,              -- the session that spawned this one (subagents)
    path         TEXT NOT NULL,     -- the transcript it was read from
    cwd          TEXT,
    title        TEXT,              -- set by the agent
    custom_title TEXT,              -- set by the user, wins over title
    started_at   INTEGER,           -- unix ms
    updated_at   INTEGER,
    messages     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, session_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    agent      TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_id  TEXT NOT NULL,       -- time and content hash; the line's id if it has no time
    ts         INTEGER,
    role       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    UNIQUE (agent, session_id, source_id)
);
CREATE INDEX IF NOT EXISTS messages_session ON messages (agent, session_id, ts, id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content = 'messages', content_rowid = 'id', tokenize = 'unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

-- How far each transcript has been read. `last_len` and `last_hash` fingerprint the line
-- ending at `offset`, so a transcript rewritten underneath us is noticed and read again.
CREATE TABLE IF NOT EXISTS checkpoints (
    key       TEXT PRIMARY KEY,
    path      TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    offset    INTEGER NOT NULL,
    last_len  INTEGER NOT NULL,
    last_hash TEXT NOT NULL
);
"""


def default_path() -> Path:
    if env := os.environ.get("BRAIN_DB"):
        return Path(env)
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "brain" / "index.db"


def connect(path: Path | None = None, *, readonly: bool = False,
            timeout: float = 30) -> sqlite3.Connection:
    """Open the index, creating it on first use. A read-only connection never writes, not even to
    create the index, so it fails unless the index exists."""
    path = path or default_path()
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout,
                             isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        if not readonly:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and not readonly:
            db.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {VERSION}; COMMIT;")
        elif version != VERSION:
            raise IndexFormatError(f"{path} has index format {version}, this brain expects "
                                   f"{VERSION}: delete it and run `brain index` to rebuild")
        return db
    except BaseException:
        db.close()
        raise
