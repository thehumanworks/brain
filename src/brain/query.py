"""Reading the index: ranked search, session listing and session replay."""

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

from brain.models import Hit, Message, MessagePage, Session

# Tool calls (whole files written), tool output and injected context match a lot of words
# without being what was said, so their BM25 scores (negative, lower is better) are scaled
# towards zero.
KIND_WEIGHT = """CASE m.kind WHEN 'text' THEN 1.0 WHEN 'summary' THEN 1.0 WHEN 'thinking' THEN 0.8
                 ELSE 0.5 END"""

# A session's title, or else its first prompt: subagents and most short sessions have no title,
# and a subagent's first prompt is the task it was given.
TITLE = """coalesce(s.custom_title, s.title, (
    SELECT substr(p.text, 1, 100) FROM messages p
    WHERE p.agent = s.agent AND p.session_id = s.session_id AND p.role = 'user' AND p.kind = 'text'
    ORDER BY p.ts, p.id LIMIT 1))"""


@dataclass
class Filters:
    agent: str | None = None
    project: str | None = None  # substring of the session's working directory
    since: int | None = None  # unix ms
    role: str | None = None

    def sql(self, alias_m: str = "m", alias_s: str = "s") -> tuple[str, dict]:
        where, params = [], {}
        if self.agent:
            where.append(f"{alias_m}.agent = :agent")
            params["agent"] = self.agent
        if self.project:
            where.append(f"{alias_s}.cwd LIKE :project")
            params["project"] = f"%{self.project}%"
        if self.since:
            where.append(f"{alias_m}.ts >= :since")
            params["since"] = self.since
        if self.role:
            where.append(f"{alias_m}.role = :role")
            params["role"] = self.role
        return "".join(f" AND {w}" for w in where), params


def parse_since(value: str) -> int:
    """`7d`, `12h`, `2w` or an ISO date, as unix ms."""
    if m := re.fullmatch(r"(\d+)([hdw])", value):
        n, unit = int(m.group(1)), m.group(2)
        seconds = n * {"h": 3600, "d": 86400, "w": 7 * 86400}[unit]
        return int((time.time() - seconds) * 1000)
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def fts_query(text: str, any_term: bool = False) -> str:
    """Plain words to an FTS5 query: each word (or "quoted phrase") must match, `word*` matches
    a prefix. Quoting every term keeps punctuation like `--delete` or `foo.py` from being read
    as FTS5 syntax."""
    terms = []
    for raw in re.findall(r'"[^"]*"|\S+', text):
        prefix = raw.endswith("*") and not raw.startswith('"')
        term = raw.strip('"').rstrip("*")
        if not re.search(r"\w", term):
            continue
        terms.append('"' + term.replace('"', '""') + '"' + ("*" if prefix else ""))
    return (" OR " if any_term else " ").join(terms)


def search(db: sqlite3.Connection, query: str, filters: Filters, limit: int = 20,
           per_session: int = 2, raw: bool = False, any_term: bool = False) -> list[Hit]:
    match = query if raw else fts_query(query, any_term)
    if not match:
        return []
    where, params = filters.sql()
    rows = db.execute(
        f"""
        WITH hits AS (
            SELECT m.id, m.agent, m.session_id, m.ts, m.role, m.kind,
                   bm25(messages_fts) * {KIND_WEIGHT} AS score
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.agent = m.agent AND s.session_id = m.session_id
            WHERE messages_fts MATCH :match {where}
        ),
        ranked AS (
            SELECT *, row_number() OVER (PARTITION BY agent, session_id ORDER BY score) AS nth
            FROM hits
        )
        SELECT r.*, s.cwd, {TITLE} AS title, s.parent_id
        FROM ranked r JOIN sessions s USING (agent, session_id)
        WHERE r.nth <= :per_session
        ORDER BY r.score
        LIMIT :limit
        """,
        {**params, "match": match, "per_session": per_session, "limit": limit},
    ).fetchall()
    # Snippets only for the rows shown: computing one for every match is the slow part.
    results = []
    for row in rows:
        snip = db.execute(
            "SELECT snippet(messages_fts, 0, '[', ']', ' … ', 32) FROM messages_fts "
            "WHERE messages_fts MATCH ? AND rowid = ?",
            (match, row["id"]),
        ).fetchone()[0]
        results.append(Hit(**dict(row), snippet=snip))
    return results


def sessions(db: sqlite3.Connection, filters: Filters, limit: int = 20,
             subagents: bool = False) -> list[Session]:
    """The `limit` most recently updated sessions, without subagents unless asked for."""
    where, params = [] if subagents else ["parent_id IS NULL"], {}
    if filters.agent:
        where.append("agent = :agent")
        params["agent"] = filters.agent
    if filters.project:
        where.append("cwd LIKE :project")
        params["project"] = f"%{filters.project}%"
    if filters.since:
        where.append("updated_at >= :since")
        params["since"] = filters.since
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    return [Session(**dict(r)) for r in db.execute(
        f"""SELECT agent, session_id, parent_id, cwd, {TITLE} AS title,
                   started_at, updated_at, messages, path
            FROM sessions s {sql_where} ORDER BY updated_at DESC LIMIT :limit""",
        {**params, "limit": limit},
    )]


def find_sessions(db: sqlite3.Connection, ref: str) -> list[Session]:
    """Sessions whose id equals, starts with or (hyphens aside) ends with `ref`."""
    flat = ref.replace("-", "").lower()
    return [Session(**dict(r)) for r in db.execute(
        f"""SELECT agent, session_id, parent_id, cwd, {TITLE} AS title,
                  started_at, updated_at, messages, path
           FROM sessions s
           WHERE session_id = :ref OR session_id LIKE :prefix
              OR (length(:flat) >= 6 AND replace(lower(session_id), '-', '') LIKE :suffix)
           ORDER BY updated_at DESC LIMIT 10""",
        {"ref": ref, "prefix": f"{ref}%", "flat": flat, "suffix": f"%{flat}"},
    )]


def messages(db: sqlite3.Connection, agent: str, session_id: str, at: int | None = None,
             context: int = 10, tools: bool = True) -> list[Message]:
    """A session's messages in order; with `at`, only the `context` either side of that one."""
    rows = [Message(**dict(r)) for r in db.execute(
        "SELECT id, ts, role, kind, text FROM messages WHERE agent = ? AND session_id = ? "
        "ORDER BY ts, id", (agent, session_id))]
    if not tools:
        rows = [r for r in rows if r.kind not in ("tool_call", "tool_result") or r.id == at]
    idx = next((i for i, r in enumerate(rows) if r.id == at), None)
    if idx is None:
        return rows
    return rows[max(0, idx - context): idx + context + 1]


def page(db: sqlite3.Connection, agent: str, session_id: str, offset: int = 0, limit: int = 30,
         anchor: int | None = None, context: int = 5, tools: bool = True,
         keep: int | None = None) -> MessagePage:
    """`limit` messages from `offset`, or from `context` before `anchor`; without `tools`, tool
    calls and results other than `keep` are left out. Run it in one transaction, so the total,
    the anchor's position and the rows agree."""
    where = "agent = :agent AND session_id = :session"
    params = {"agent": agent, "session": session_id, "keep": keep}
    if not tools:
        where += " AND (kind NOT IN ('tool_call', 'tool_result') OR id = :keep)"
    total = db.execute(f"SELECT count(*) FROM messages WHERE {where}", params).fetchone()[0]
    if anchor is not None:
        target = db.execute(
            "SELECT ts FROM messages WHERE agent = :agent AND session_id = :session AND id = :anchor",
            {**params, "anchor": anchor},
        ).fetchone()
        if target:
            # Find the insertion position even when hiding tools hides the anchor.
            # SQLite sorts NULL timestamps first; equal timestamps use message IDs.
            position = db.execute(
                f"SELECT count(*) FROM messages WHERE {where} AND (ts < :ts "
                "OR (ts IS NULL AND :ts IS NOT NULL) OR (ts IS :ts AND id < :anchor))",
                {**params, "anchor": anchor, "ts": target["ts"]},
            ).fetchone()[0]
            offset = max(0, position - context)
    offset = max(0, min(offset, max(0, total - 1)))
    rows = db.execute(
        f"SELECT id, ts, role, kind, text FROM messages WHERE {where} "
        "ORDER BY ts, id LIMIT :limit OFFSET :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return MessagePage([Message(**dict(r)) for r in rows], offset, total)


def short_id(session_id: str) -> str:
    """The last 8 hex digits: UUIDv7 ids (Codex, Pi) share their leading, time-based digits."""
    flat = session_id.replace("-", "")
    return flat[-8:]
