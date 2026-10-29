"""Read-only, keyboard-driven browsing. SQLite work stays off the terminal event loop."""

import asyncio
import sqlite3
import threading
import unicodedata
from bisect import bisect_right
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.utils import get_cwidth

import brain
from brain import Brain, BrainError, Hit, MessagePage, Session

RESULT_BATCH = 100
MESSAGE_PAGE = 30
AGENTS = (None, *brain.AGENTS)


def safe_text(value: str | None) -> str:
    """Render transcript data literally, never as terminal commands or bidi overrides."""
    return "".join(
        c if (c in "\n\t" or unicodedata.category(c) != "Cc")
        and c not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" else "�"
        for c in (value or "")
    )


def one_line(value: str | None) -> str:
    return " ".join(safe_text(value).split())


def timestamp(value: int | None) -> str:
    try:
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M") if value else "?"
    except (ValueError, OverflowError, OSError):
        return "?"


@lru_cache(maxsize=128)
def visual_starts(line: str, width: int) -> tuple[int, ...]:
    """Character offsets of wrapped screen lines, matching TextArea's cell-based wrapping."""
    width = max(1, width)
    starts, column = [0], 0
    # BufferControl renders an extra space for the end-of-line cursor position.
    for index, char in enumerate(line + " "):
        cells = min(width, get_cwidth(char))
        if column + cells > width:
            starts.append(index)
            column = 0
        column += cells
    return tuple(starts)


def hit_id(row) -> int | None:
    """The matched message of a search hit; None for a session from the recent list."""
    return getattr(row, "id", None)


def identity(row: Hit | Session) -> tuple:
    return row.agent, row.session_id, hit_id(row)


class IndexReader:
    """One short-lived read-only Brain per worker request, safe alongside the watcher."""

    def __init__(self, path: Path):
        self.path = path

    def open(self, cancelled: threading.Event | None = None) -> Brain:
        if cancelled and cancelled.is_set():
            raise sqlite3.OperationalError("interrupted")
        return Brain(self.path, readonly=True, timeout=2,
                     interrupt=cancelled.is_set if cancelled is not None else None)

    def results(self, text: str, limit: int, subagents: bool, *, agent: str | None = None,
                project: str | None = None, since: int | None = None,
                cancelled: threading.Event | None = None) -> tuple[list[Hit | Session], bool]:
        with self.open(cancelled) as index:
            if text.strip():
                rows = index.search(text, agent=agent, project=project, since=since,
                                    limit=limit + 1)
            else:
                rows = index.sessions(agent=agent, project=project, since=since,
                                      limit=limit + 1, subagents=subagents)
            return rows[:limit], len(rows) > limit

    def messages(self, row, offset: int = 0, anchor: int | None = None,
                 tools: bool = True, *, context: int = 5,
                 cancelled: threading.Event | None = None) -> MessagePage:
        with self.open(cancelled) as index:
            # Keep the matched message visible even when it is a hidden tool result.
            return index.page(row, offset=offset, limit=MESSAGE_PAGE, anchor=anchor,
                              context=context, tools=tools, keep=hit_id(row))


class Browser:
    def __init__(self, path: Path, *, text: str = "", agent: str | None = None,
                 project: str | None = None, since: int | None = None,
                 subagents: bool = False, input=None, output=None):
        self.reader = IndexReader(path)
        self.agent = agent
        self.since = since
        self.subagents = subagents
        self.rows: list[Hit | Session] = []
        self.selected = 0
        self.limit = RESULT_BATCH
        self.has_more = False
        self.active: Hit | Session | None = None
        self.page = MessagePage([], 0, 0)
        self.message_positions: list[tuple[int, int]] = []
        self.show_tools = True
        self.loading = False
        self.error = ""
        self.generation = 0
        self.task: asyncio.Task | None = None
        self.cancelled = threading.Event()
        self.applied_text = text
        self.applied_project = project or ""

        self.search = TextArea(text=text, multiline=False, prompt="Search  › ",
                               accept_handler=lambda _: self.submit())
        self.project = TextArea(text=self.applied_project, multiline=False, prompt="Project › ",
                                accept_handler=lambda _: self.submit())
        self.result_control = FormattedTextControl(
            self.result_text, focusable=True,
            get_cursor_position=lambda: Point(0, self.selected * 3),
        )
        self.result_window = Window(self.result_control, wrap_lines=False)
        self.body = TextArea(read_only=True, scrollbar=True, wrap_lines=True)
        self.app = Application(
            layout=Layout(HSplit([
                Window(FormattedTextControl(self.heading), height=1, style="class:heading"),
                self.search,
                self.project,
                ConditionalContainer(self.result_window, Condition(lambda: self.active is None)),
                ConditionalContainer(self.body, Condition(lambda: self.active is not None)),
                Window(FormattedTextControl(self.status), height=1, style="class:status"),
                Window(FormattedTextControl(self.help_text), height=2, wrap_lines=True, style="class:help"),
            ]), focused_element=self.search),
            key_bindings=self.bindings(), full_screen=True, mouse_support=False,
            input=input, output=output,
            style=Style.from_dict({
                "heading": "bold", "selected": "reverse", "meta": "fg:ansicyan",
                "status": "fg:ansicyan", "help": "fg:ansibrightblack",
            }),
        )
        # A lone Escape should not feel sluggish when returning from a conversation.
        self.app.ttimeoutlen = 0.1

    def heading(self) -> str:
        agent = self.agent or "all agents"
        if self.active:
            title = one_line(self.active.title) or "Untitled conversation"
            return f" brain · {self.active.agent} · {title}"
        return f" brain · {agent} · {'Search results' if self.applied_text.strip() else 'Recent conversations'}"

    def result_text(self) -> list[tuple[str, str]]:
        if not self.rows:
            return [("", "\n  No matches. Change the search or filters." if self.applied_text.strip()
                     else "\n  No conversations. Run brain index, then press F5. Check filters too.")]
        lines = []
        for i, row in enumerate(self.rows):
            style = "class:selected" if i == self.selected else ""
            title = one_line(row.title) or "Untitled conversation"
            if isinstance(row, Hit):
                date, detail = timestamp(row.ts), one_line(row.snippet)
            else:
                date, detail = timestamp(row.updated_at), f"{row.messages} messages"
            lines.extend([
                (style, f"{'›' if i == self.selected else ' '} {title}\n"),
                (style or "class:meta", f"  {row.agent} · {date} · {one_line(row.cwd)}\n"),
                (style, f"  {detail}\n"),
            ])
        return lines

    def status(self) -> str:
        if self.error:
            return " " + one_line(self.error)
        if self.loading:
            return " Loading… (Ctrl-Q to quit)"
        if self.active:
            start = self.page.offset + 1 if self.page.messages else 0
            end = self.page.offset + len(self.page.messages)
            return f" Messages {start}–{end} of {self.page.total} · tools {'shown' if self.show_tools else 'hidden'}"
        return f" {len(self.rows)} results{' · more available (m)' if self.has_more else ''} · F2 agent: {self.agent or 'all'}"

    def help_text(self) -> str:
        if self.active:
            return " Ctrl-Q quit · Esc back · ↑↓/PgUp/PgDn scroll · F6/F7 prev/next page · F3 tools · F5 refresh"
        return " Ctrl-Q quit · Enter search/open · Tab focus · ↑↓ select · / search · F2 agent · m more · F5 refresh"

    def cancel_pending(self) -> None:
        self.generation += 1
        self.cancelled.set()
        if self.task:
            self.task.cancel()

    def request(self, work, apply) -> None:
        self.cancel_pending()
        generation = self.generation
        self.cancelled = cancelled = threading.Event()
        self.loading, self.error = True, ""

        async def load():
            try:
                result = await asyncio.to_thread(work, cancelled)
            except (sqlite3.Error, OSError, BrainError) as exc:
                if generation == self.generation:
                    self.error = f"Cannot read index: {exc}. Run brain index if needed; F5 retries."
            else:
                if generation == self.generation:
                    apply(result)
            finally:
                if generation == self.generation:
                    self.loading = False
                    self.app.invalidate()

        self.task = self.app.create_background_task(load())
        self.app.invalidate()

    def load_results(self, preserve: bool = False) -> None:
        old = identity(self.rows[self.selected]) if preserve and self.rows else None
        # Snapshot values before starting a worker; edits cannot change an in-flight query.
        text, limit, subagents = self.applied_text, self.limit, self.subagents
        filters = {"agent": self.agent, "project": self.applied_project or None, "since": self.since}

        def apply(result):
            self.rows, self.has_more = result
            self.selected = next((i for i, row in enumerate(self.rows) if identity(row) == old), 0)
            self.result_window.vertical_scroll = 0

        self.request(lambda cancel: self.reader.results(text, limit, subagents, **filters,
                                                       cancelled=cancel), apply)

    def submit(self) -> bool:
        self.active = None
        self.applied_text, self.applied_project = self.search.text, self.project.text
        self.limit = RESULT_BATCH
        self.rows, self.selected = [], 0
        self.load_results()
        self.app.layout.focus(self.result_control)
        return True  # Keep the search text; TextArea must not reset its buffer.

    def open_selected(self) -> None:
        if not self.rows or self.loading:
            return
        self.active = self.rows[self.selected]
        self.page = MessagePage([], 0, 0)
        self.body.buffer.set_document(Document("Loading…"), bypass_readonly=True)
        self.app.layout.focus(self.body)
        self.load_messages(anchor=hit_id(self.active))

    def load_messages(self, offset: int = 0, anchor: int | None = None, context: int = 5) -> None:
        row, tools = self.active, self.show_tools

        def apply(page):
            self.page = page
            chunks, cursor, length = [], 0, 0
            self.message_positions = []
            for message in page.messages:
                self.message_positions.append((length, message.id))
                matched = message.id == hit_id(row)
                if message.id == anchor:
                    cursor = length
                header = (f"{'▶ MATCH · ' if matched else ''}{message.role} / {message.kind}"
                          f" · {timestamp(message.ts)}\n")
                chunk = safe_text(header + message.text).expandtabs(4) + "\n\n"
                chunks.append(chunk)
                length += len(chunk)
            content = "".join(chunks) or "No messages in this conversation."
            self.body.buffer.set_document(Document(content, cursor), bypass_readonly=True)
            self.body.window.vertical_scroll = 0
            self.body.window.vertical_scroll_2 = 0

        self.request(lambda cancel: self.reader.messages(row, offset, anchor, tools,
                                                        context=context, cancelled=cancel), apply)

    def current_message(self) -> int | None:
        cursor = self.body.buffer.cursor_position
        return next((mid for start, mid in reversed(self.message_positions) if start <= cursor), None)

    def move_visual(self, delta: int) -> None:
        """Move within wrapped paragraphs, not just between newline-delimited logical lines."""
        info = self.body.window.render_info
        if info is None:
            return
        document = self.body.document
        row, col = document.cursor_position_row, document.cursor_position_col
        starts = visual_starts(document.lines[row], info.window_width)
        index = bisect_right(starts, col) - 1
        target = index + delta
        # TextArea aligns a short logical line as a whole. Before leaving a
        # viewport-tall paragraph, expose its remaining tail/head rather than
        # jumping directly to the following/preceding newline.
        if len(starts) > info.window_height:
            if index > 0 and target < 0:
                target = 0
            elif index < len(starts) - 1 and target >= len(starts):
                target = len(starts) - 1
        while target >= len(starts) and row < document.line_count - 1:
            target -= len(starts)
            row += 1
            starts = visual_starts(document.lines[row], info.window_width)
        while target < 0 and row > 0:
            row -= 1
            starts = visual_starts(document.lines[row], info.window_width)
            target += len(starts)
        col = starts[max(0, min(target, len(starts) - 1))]
        self.body.buffer.cursor_position = document.translate_row_col_to_index(row, col)

    def back(self) -> None:
        self.cancel_pending()
        self.loading, self.error = False, ""
        self.active = None
        self.app.layout.focus(self.result_control)

    def bindings(self) -> KeyBindings:
        kb = KeyBindings()
        results = Condition(lambda: self.app.layout.has_focus(self.result_control))
        reading = Condition(lambda: self.active is not None and self.app.layout.has_focus(self.body))

        @kb.add("c-q")
        @kb.add("c-c")
        def quit_app(event):
            event.app.exit()

        @kb.add("tab")
        def tab(event):
            event.app.layout.focus_next()

        @kb.add("s-tab")
        def backtab(event):
            event.app.layout.focus_previous()

        @kb.add("escape")
        def back(event):
            if self.active:
                self.back()
            else:
                event.app.layout.focus(self.search)

        @kb.add("/", filter=results)
        def focus_search(event):
            event.app.layout.focus(self.search)

        @kb.add("enter", filter=results)
        def open_result(event):
            self.open_selected()

        for key, delta in [("up", -1), ("down", 1), ("pageup", -10), ("pagedown", 10)]:
            def move(event, delta=delta):
                self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))
            kb.add(key, filter=results)(move)

        for key, direction, page in [("up", -1, False), ("down", 1, False),
                                     ("pageup", -1, True), ("pagedown", 1, True)]:
            def scroll(event, direction=direction, page=page):
                info = self.body.window.render_info
                amount = max(1, info.window_height - 1) if page and info else 1
                self.move_visual(direction * amount)
            kb.add(key, filter=reading)(scroll)

        @kb.add("m", filter=results)
        def more(event):
            if self.has_more and not self.loading:
                self.limit += RESULT_BATCH
                self.load_results(preserve=True)

        @kb.add("f2")
        def agent(event):
            self.agent = AGENTS[(AGENTS.index(self.agent) + 1) % len(AGENTS)]
            self.submit()

        @kb.add("f3", filter=reading)
        def tools(event):
            if not self.loading:
                self.show_tools = not self.show_tools
                self.load_messages(anchor=self.current_message(), context=0)

        @kb.add("f5")
        def refresh(event):
            if self.active:
                self.load_messages(self.page.offset)
            else:
                self.load_results(preserve=True)

        @kb.add("f6", filter=reading)
        def previous(event):
            if not self.loading and self.page.offset:
                self.load_messages(max(0, self.page.offset - MESSAGE_PAGE))

        @kb.add("f7", filter=reading)
        def following(event):
            if not self.loading and self.page.offset + len(self.page.messages) < self.page.total:
                self.load_messages(self.page.offset + len(self.page.messages))

        return kb

    async def run_async(self) -> None:
        try:
            await self.app.run_async(pre_run=self.load_results)
        finally:
            self.cancel_pending()


def run(path: Path, **kwargs) -> None:
    asyncio.run(Browser(path, **kwargs).run_async())
