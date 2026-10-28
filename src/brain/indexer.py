"""Follow every transcript and add what was written since the last run to the index.

Each transcript has a checkpoint: how many bytes of it were read, and a fingerprint of the last
line read. A file whose size and mtime are unchanged is skipped without opening it; otherwise
reading resumes at the checkpoint, unless the fingerprint no longer matches the bytes there (the
file was rewritten), in which case it is read again from the start. Messages are keyed by their
time and content, so neither reading a line twice nor an agent writing a message out again
under a new id (Claude Code does, on compaction) stores it twice.
"""

import hashlib
import json
import sqlite3
from compression import zstd
from dataclasses import dataclass, field
from pathlib import Path

from brain.sources import PARSERS, WANTED, SourceFile, codex_root, discover

# Messages written per transaction: bounds memory on a first read of a large transcript and how
# long a search waits for the write lock.
BATCH = 2000


@dataclass
class Result:
    files: int = 0  # transcripts with something new
    messages: int = 0  # messages added
    rereads: int = 0  # transcripts rewritten underneath us and read again
    errors: list[str] = field(default_factory=list)


def digest(line: bytes) -> str:
    return hashlib.blake2b(line, digest_size=8).hexdigest()


def message_key(m) -> str:
    if m.ts is None:
        return m.source_id
    return f"{m.ts}:{digest(f'{m.role}/{m.kind}/{m.text}'.encode())}"


def open_bytes(path: Path):
    return zstd.open(path, "rb") if path.suffix == ".zst" else open(path, "rb")


def index_all(db: sqlite3.Connection, progress=None) -> Result:
    """Index every transcript; `progress(done, total, result)` is called every 100 files."""
    result = Result()
    sources = list(discover())
    for n, src in enumerate(sources):
        if progress and n % 100 == 0:
            progress(n, len(sources), result)
        try:
            index_file(db, src, result)
        except (OSError, zstd.ZstdError) as e:
            result.errors.append(f"{src.path}: {e}")
    try:
        index_codex_titles(db, force=result.files > 0)
    except OSError as e:
        result.errors.append(f"codex titles: {e}")
    return result


def index_file(db: sqlite3.Connection, src: SourceFile, result: Result) -> None:
    try:
        st = src.path.stat()
    except FileNotFoundError:
        return  # gone since discovery
    cp = db.execute("SELECT * FROM checkpoints WHERE key = ?", (src.key,)).fetchone()
    if cp and (cp["path"], cp["size"], cp["mtime_ns"]) == (str(src.path), st.st_size, st.st_mtime_ns):
        return

    parse, wanted = PARSERS[src.agent], WANTED[src.agent]
    stat = (st.st_size, st.st_mtime_ns)
    added = 0
    with open_bytes(src.path) as f:
        pos, last = 0, b""
        if cp and cp["offset"]:
            f.seek(cp["offset"] - cp["last_len"])
            tail = f.read(cp["last_len"])
            if digest(tail) == cp["last_hash"]:
                pos, last = cp["offset"], tail
            else:
                f.seek(0)
                result.rereads += 1

        rows, meta = [], {}
        for line in f:
            if not line.endswith(b"\n"):
                break  # still being written: picked up once it is finished
            offset = pos
            pos += len(line)
            last = line
            if wanted and not any(w in line for w in wanted):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            parsed = parse(rec, offset)
            for k, v in parsed.meta.items():
                if v and (k not in meta or k in ("title", "custom_title")):
                    meta[k] = v
            if m := parsed.message:
                rows.append((src.agent, src.session_id, message_key(m), m.ts, m.role, m.kind, m.text))
            if len(rows) >= BATCH:
                added += write(db, src, rows, meta, pos, last, stat)
                rows, meta = [], {}
        added += write(db, src, rows, meta, pos, last, stat)

    if added:
        result.files += 1
        result.messages += added


def write(db, src: SourceFile, rows, meta, pos: int, last: bytes, stat) -> int:
    """Store a batch of messages, its session's metadata and the checkpoint after it, together."""
    times = [r[3] for r in rows if r[3] is not None]
    db.execute("BEGIN IMMEDIATE")
    try:
        added = db.executemany(
            "INSERT OR IGNORE INTO messages (agent, session_id, source_id, ts, role, kind, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        ).rowcount if rows else 0
        db.execute(
            """
            INSERT INTO sessions (agent, session_id, parent_id, path, cwd, title, custom_title,
                                  started_at, updated_at)
            VALUES (:agent, :session_id, :parent_id, :path, :cwd, :title, :custom_title,
                    :first, :last)
            ON CONFLICT (agent, session_id) DO UPDATE SET
                path = excluded.path,
                parent_id = coalesce(sessions.parent_id, excluded.parent_id),
                cwd = coalesce(sessions.cwd, excluded.cwd),
                title = coalesce(excluded.title, sessions.title),
                custom_title = coalesce(excluded.custom_title, sessions.custom_title),
                started_at = min(coalesce(sessions.started_at, excluded.started_at),
                                 coalesce(excluded.started_at, sessions.started_at)),
                updated_at = max(coalesce(sessions.updated_at, excluded.updated_at),
                                 coalesce(excluded.updated_at, sessions.updated_at))
            """,
            {
                "agent": src.agent,
                "session_id": src.session_id,
                "parent_id": meta.get("parent_id") or src.parent_id,
                "path": str(src.path),
                "cwd": meta.get("cwd"),
                "title": meta.get("title"),
                "custom_title": meta.get("custom_title"),
                "first": min(times, default=None),
                "last": max(times, default=None),
            },
        )
        if added:
            db.execute(
                "UPDATE sessions SET messages = (SELECT count(*) FROM messages "
                "WHERE agent = :a AND session_id = :s) WHERE agent = :a AND session_id = :s",
                {"a": src.agent, "s": src.session_id},
            )
        db.execute(
            "INSERT OR REPLACE INTO checkpoints (key, path, size, mtime_ns, offset, last_len, last_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (src.key, str(src.path), *stat, pos, len(last), digest(last)),
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return added


def index_codex_titles(db: sqlite3.Connection, force: bool = False) -> None:
    """Codex keeps thread names in an index file rather than in the transcript. Applied again
    whenever sessions were added, since a name can be written before its session's first line."""
    path = codex_root() / "session_index.jsonl"
    try:
        st = path.stat()
    except FileNotFoundError:
        return
    stat = (str(path), st.st_size, st.st_mtime_ns)
    cp = db.execute("SELECT path, size, mtime_ns FROM checkpoints WHERE key = 'codex:titles'").fetchone()
    if cp and tuple(cp) == stat and not force:
        return
    titles = {}
    with open(path, "rb") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("id") and rec.get("thread_name"):
                titles[rec["id"]] = rec["thread_name"]
    db.execute("BEGIN IMMEDIATE")
    db.executemany(
        "UPDATE sessions SET title = ? WHERE agent = 'codex' AND session_id = ? AND title IS NOT ?",
        [(t, i, t) for i, t in titles.items()],
    )
    db.execute(
        "INSERT OR REPLACE INTO checkpoints VALUES ('codex:titles', ?, ?, ?, 0, 0, '')", stat
    )
    db.execute("COMMIT")
