"""`Brain`: the index, opened for scripting. The `brain` command is built on this class."""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from brain import db as dbmod, indexer, query
from brain.errors import AmbiguousSession, SessionNotFound
from brain.models import AgentStats, Hit, IndexResult, Message, MessagePage, Session, Stats

# Where a session can be given: a Session, a Hit, or an id as `Brain.session` takes it.
type SessionRef = Session | Hit | str
# A point in time: unix ms, a datetime, or `7d`, `12h`, `2w` or an ISO date.
type Since = int | datetime | str


def since_ms(value: Since | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return query.parse_since(value)


class Brain:
    """Every Claude Code, Codex and Pi conversation on this machine, in one SQLite index.

    `path` defaults to $BRAIN_DB or ~/.local/share/brain/index.db, the `brain` command's own
    index. A writable index is created when missing. `readonly=True` opens an existing index
    without ever writing to it. `interrupt` is polled while a query runs; once it returns true,
    the query stops with `sqlite3.OperationalError`. Like the SQLite connection it holds (`db`,
    for SQL of your own), a Brain belongs to the thread that opened it.
    """

    def __init__(self, path: str | Path | None = None, *, readonly: bool = False,
                 timeout: float = 30, interrupt: Callable[[], bool] | None = None):
        self.path = Path(path) if path is not None else dbmod.default_path()
        self.db = dbmod.connect(self.path, readonly=readonly, timeout=timeout)
        if interrupt:
            self.db.set_progress_handler(lambda: int(bool(interrupt())), 1000)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Brain":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Read in one transaction, so a write in between can't make the parts disagree."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN")
        try:
            yield
        finally:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")

    # --- Keeping it current -------------------------------------------------------------------

    def index(self, progress: Callable[[int, int, IndexResult], None] | None = None) -> IndexResult:
        """Add what every agent wrote since the last run. `progress(done, total, result)` is
        called every 100 transcripts."""
        return indexer.index_all(self.db, progress)

    def watch(self, interval: float = 5) -> Iterator[IndexResult]:
        """Index now and every `interval` seconds from then on, yielding each run's result."""
        while True:
            result = self.index()
            yield result
            time.sleep(max(0.0, interval - result.seconds))

    # --- Reading ------------------------------------------------------------------------------

    def search(self, text: str, *, agent: str | None = None, project: str | None = None,
               since: Since | None = None, role: str | None = None, limit: int = 20,
               per_session: int = 2, any_term: bool = False, raw: bool = False) -> list[Hit]:
        """Messages matching `text`, best first, at most `per_session` from each session.

        Every word must match (`any_term`: any of them), a "quoted phrase" matches as a whole,
        `prefix*` matches a prefix, and other punctuation is literal; `raw` hands `text` to
        SQLite FTS5 as it is. `project` is a substring of the session's working directory,
        `since` filters on the message's time and `role` is user, assistant, tool or system.
        """
        filters = query.Filters(agent, project, since_ms(since), role)
        with self.snapshot():
            return query.search(self.db, text, filters, limit=limit, per_session=per_session,
                                raw=raw, any_term=any_term)

    def sessions(self, *, agent: str | None = None, project: str | None = None,
                 since: Since | None = None, limit: int = 20,
                 subagents: bool = False) -> list[Session]:
        """The `limit` most recently updated sessions, with subagents only if asked for.
        `since` filters on when the session was last updated."""
        filters = query.Filters(agent, project, since_ms(since))
        return query.sessions(self.db, filters, limit, subagents=subagents)

    def find_sessions(self, ref: str) -> list[Session]:
        """Up to 10 sessions whose id is `ref`, starts with it or (hyphens aside, from 6
        characters) ends with it, most recently updated first."""
        return query.find_sessions(self.db, ref)

    def session(self, ref: str) -> Session:
        """The session `ref` names: a full id, or the start or end of one (as `brain list`
        shows it). If several match, the most recently updated wins only if its id is exactly
        `ref`; otherwise this raises AmbiguousSession. Raises SessionNotFound if none match."""
        found = self.find_sessions(ref)
        if not found:
            raise SessionNotFound(ref)
        if len(found) > 1 and found[0].session_id != ref:
            raise AmbiguousSession(ref, found)
        return found[0]

    def messages(self, session: SessionRef, *, at: int | None = None, context: int = 10,
                 tools: bool = True) -> list[Message]:
        """A session's messages in order. With `at` (a message id, such as `Hit.id`), only the
        `context` messages either side of it. `tools=False` leaves out tool calls and their
        results, other than `at` itself."""
        agent, session_id = self.locate(session)
        return query.messages(self.db, agent, session_id, at, context, tools)

    def page(self, session: SessionRef, *, offset: int = 0, limit: int = 30,
             anchor: int | None = None, context: int = 5, tools: bool = True,
             keep: int | None = None) -> MessagePage:
        """`limit` of a session's messages from `offset`, and how many it has in all: for
        paging through long sessions. With `anchor` (a message id) the page starts `context`
        messages before it instead, which works even when `anchor` is hidden. `tools=False`
        leaves out tool calls and their results other than `keep`."""
        agent, session_id = self.locate(session)
        with self.snapshot():
            return query.page(self.db, agent, session_id, offset, limit, anchor, context,
                              tools, keep)

    def locate(self, session: SessionRef) -> tuple[str, str]:
        if isinstance(session, str):
            session = self.session(session)
        return session.agent, session.session_id

    def stats(self) -> Stats:
        """Sessions and messages per agent, and the index's size on disk."""
        agents = [AgentStats(**dict(row)) for row in self.db.execute(
            "SELECT agent, count(*) AS sessions, coalesce(sum(messages), 0) AS messages, "
            "min(started_at) AS first, max(updated_at) AS last FROM sessions GROUP BY agent"
        )]
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*"))
        return Stats(self.path, size, agents)
