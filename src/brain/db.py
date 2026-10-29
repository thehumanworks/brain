"""The local index: one SQLite file with a full-text index over every message."""

import os
import sqlite3
from pathlib import Path

from brain.errors import IndexFormatError
from brain.sources import pi_paths

# The index format. 2: a Pi reply is a message per block (thinking, text, tool call), not one.
VERSION = 2

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


def upgrade(db: sqlite3.Connection) -> None:
    """Bring an older index up to this format. From 1: Pi sessions are read again from their
    transcripts, so their replies split into blocks. Only those indexing will find again are: a
    session whose transcript is gone, or elsewhere, keeps the messages it has."""
    found = {str(p) for p in pi_paths()}
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] == 1:  # not upgraded meanwhile
            for session_id, path in db.execute(
                    "SELECT session_id, path FROM sessions WHERE agent = 'pi'").fetchall():
                if path in found:
                    db.execute("DELETE FROM messages WHERE agent = 'pi' AND session_id = ?",
                               (session_id,))
                    db.execute("UPDATE sessions SET messages = 0 WHERE agent = 'pi' AND session_id = ?",
                               (session_id,))
                    db.execute("DELETE FROM checkpoints WHERE key = ?", (path,))
            db.execute(f"PRAGMA user_version = {VERSION}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def connect(path: Path | None = None, *, readonly: bool = False,
            timeout: float = 30) -> sqlite3.Connection:
    """Open the index, creating it on first use and upgrading an older one. A read-only
    connection never writes, not even to create the index, so it fails unless the index exists;
    it reads an older format as it is, since the tables are the same."""
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
        elif version == 1 and not readonly:
            upgrade(db)
        elif version != VERSION and not (readonly and version == 1):
            raise IndexFormatError(f"{path} has index format {version}, this brain expects "
                                   f"{VERSION}: delete it and run `brain index` to rebuild")
        return db
    except BaseException:
        db.close()
        raise
