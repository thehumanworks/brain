"""Real SQLite/FTS and actual terminal key bindings, without a real terminal or user index."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from brain import Brain, BrainError, Session, cli
from brain.cli.tui import Browser, IndexReader, MESSAGE_PAGE, safe_text, visual_starts


def as_hit(row: Session, message_id: int) -> SimpleNamespace:
    """What the reader needs of a search hit: its session and matched message."""
    return SimpleNamespace(agent=row.agent, session_id=row.session_id, id=message_id)


class IndexFixture:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "index.db"
        self.brain = Brain(self.path)
        self.addCleanup(self.brain.close)
        self.db = self.brain.db
        self.reader = IndexReader(self.path)

    def session(self, agent="claude", sid="session", title="Conversation", texts=("hello",),
                updated=1000, cwd="/work/api", parent=None):
        self.db.execute(
            "INSERT INTO sessions(agent, session_id, path, cwd, title, started_at, updated_at, messages, parent_id) "
            "VALUES (?, ?, '/transcript.jsonl', ?, ?, 1000, ?, ?, ?)",
            (agent, sid, cwd, title, updated, len(texts), parent),
        )
        ids = []
        for i, text in enumerate(texts):
            kind = "text"
            if isinstance(text, tuple):
                kind, text = text
            ids.append(self.db.execute(
                "INSERT INTO messages(agent, session_id, source_id, ts, role, kind, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent, sid, str(i), 1000 + i, "tool" if kind == "tool_result" else "user", kind, text),
            ).lastrowid)
        return Session(agent, sid, parent, cwd, title, 1000, updated, len(texts), "/transcript.jsonl"), ids


class ReaderTest(IndexFixture, unittest.TestCase):
    def test_real_fts_filters_and_more_results(self):
        self.session(sid="old", title="Old", texts=("rate limiting",), updated=1000)
        self.session(sid="new", title="New", texts=("rate limiting",), updated=3000, cwd="/work/web")
        self.session(agent="pi", sid="child", texts=("rate limiting",), updated=4000, parent="new")
        rows, more = self.reader.results("", 1, False)
        self.assertEqual([r.title for r in rows], ["New"])
        self.assertTrue(more)
        rows, more = self.reader.results("rate", 100, False, agent="claude", project="api")
        self.assertEqual([r.session_id for r in rows], ["old"])
        self.assertFalse(more)
        rows, _ = self.reader.results("", 100, True, since=3500)
        self.assertEqual([r.session_id for r in rows], ["child"])
        self.assertEqual(self.reader.results("-- ' \"", 100, False), ([], False))

    def test_pages_cover_session_exactly_and_anchor_uses_agent_and_message(self):
        self.session(agent="pi", sid="same", texts=("wrong agent",))
        row, ids = self.session(sid="same", texts=tuple(f"message {i}" for i in range(75)))
        # Equal timestamps and NULL timestamps must not lose or duplicate messages.
        self.db.execute("UPDATE messages SET ts = 1000 WHERE agent = 'claude'")
        self.db.execute("UPDATE messages SET ts = NULL WHERE id = ?", (ids[0],))
        found, offset = [], 0
        while True:
            page = self.reader.messages(row, offset)
            found.extend(r.id for r in page.messages)
            self.assertLessEqual(len(page.messages), MESSAGE_PAGE)
            self.assertEqual(page.total, 75)
            offset += len(page.messages)
            if offset >= page.total:
                break
        self.assertEqual(found, ids)
        hit = self.reader.messages(as_hit(row, ids[55]), anchor=ids[55])
        self.assertEqual(hit.offset, 50)
        self.assertEqual(hit.messages[5].id, ids[55])
        self.assertNotIn("wrong agent", [r.text for r in hit.messages])

    def test_hiding_tools_retains_the_matched_tool_message(self):
        row, ids = self.session(texts=("question", ("tool_result", "hidden"),
                                      ("tool_result", "matched"), "answer"))
        page = self.reader.messages(as_hit(row, ids[2]), anchor=ids[2], tools=False)
        self.assertEqual([r.text for r in page.messages], ["question", "matched", "answer"])
        self.assertEqual(page.total, 3)

    def test_hidden_anchor_keeps_place_instead_of_jumping_to_session_start(self):
        row, ids = self.session(texts=tuple(
            ("tool_result", "hidden anchor") if i == 50 else f"message {i}" for i in range(80)))
        page = self.reader.messages(row, anchor=ids[50], tools=False)
        self.assertEqual(page.offset, 45)
        self.assertNotIn(ids[50], [r.id for r in page.messages])
        self.assertIn(ids[51], [r.id for r in page.messages])

    def test_reader_does_not_create_or_mutate_an_index(self):
        missing = self.path.parent / "absent" / "index.db"
        with self.assertRaises(sqlite3.OperationalError):
            IndexReader(missing).results("", 100, False)
        self.assertFalse(missing.parent.exists())
        reader = self.reader.open()
        self.addCleanup(reader.close)
        with self.assertRaises(sqlite3.OperationalError):
            reader.db.execute("DELETE FROM messages")
        self.db.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(BrainError, "index format 999"):
            self.reader.results("", 100, False)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_cancel_interrupts_sql_instead_of_only_hiding_old_results(self):
        cancelled = threading.Event()
        reader = self.reader.open(cancelled)
        self.addCleanup(reader.close)
        cancelled.set()
        with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
            reader.db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                       "SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n").fetchone()

    def test_visual_lines_use_terminal_cells_not_codepoint_count(self):
        self.assertEqual(visual_starts("abcdef", 3), (0, 3, 6))
        self.assertEqual(visual_starts("日本e\u0301語", 4), (0, 2))
        self.assertEqual(visual_starts("", 80), (0,))

    def test_transcript_control_sequences_are_literal_and_unicode_survives(self):
        text = "\x1b]52;c;clipboard\x07\x1b[2J\x9b31m\u202eevil\u2066 text\n\t日本語 👩‍💻"
        rendered = safe_text(text)
        for unsafe in ("\x1b", "\x07", "\x9b", "\u202e", "\u2066"):
            self.assertNotIn(unsafe, rendered)
        self.assertIn("\n\t日本語 👩‍💻", rendered)


class BrowserTest(IndexFixture, unittest.IsolatedAsyncioTestCase):
    async def start(self, **kwargs):
        pipe_context = create_pipe_input()
        self.pipe = pipe_context.__enter__()
        self.addCleanup(pipe_context.__exit__, None, None, None)
        self.browser = Browser(self.path, input=self.pipe, output=DummyOutput(), **kwargs)
        self.running = asyncio.create_task(self.browser.run_async())
        self.addAsyncCleanup(self.stop)
        await self.wait(lambda: self.browser.task is not None and not self.browser.loading)
        return self.browser

    async def wait(self, predicate):
        async def poll():
            while not predicate():
                if self.running.done():
                    self.running.result()
                    self.fail("TUI exited unexpectedly")
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout=5)

    async def keys(self, text):
        self.pipe.send_text(text)
        await asyncio.sleep(0)  # Give the real input processor a chance to wake up.

    async def stop(self):
        if not self.running.done():
            self.pipe.send_text("\x11")  # Ctrl-Q, the actual quit binding.
        await asyncio.wait_for(self.running, timeout=5)

    async def test_search_open_exact_hit_scroll_and_back_preserves_search(self):
        self.session(agent="pi", sid="same", title="Wrong agent", texts=("unrelated",))
        _, ids = self.session(sid="same", title="Fix deploy", texts=tuple(
            "unique needle\n" + "long message\n" * 100 if i == 45 else f"context {i}" for i in range(70)))
        browser = await self.start()
        await self.keys("unique needle\r")
        await self.wait(lambda: not browser.loading and browser.applied_text == "unique needle")
        self.assertEqual(len(browser.rows), 1)
        self.assertIn("Fix deploy", "".join(t for _, t in browser.result_text()))
        self.assertEqual(browser.rows[0].id, ids[45])
        await self.keys("\r")
        await self.wait(lambda: browser.active is not None and not browser.loading)
        self.assertEqual(browser.active.agent, "claude")
        self.assertTrue(browser.body.document.current_line.startswith("▶ MATCH"))
        self.assertIn("unique needle", browser.body.text)
        cursor = browser.body.buffer.cursor_position
        await self.keys("\x1b[B")  # Down within the actual read-only TextArea.
        await self.wait(lambda: browser.body.buffer.cursor_position > cursor)
        await self.keys("\x1b")
        await self.wait(lambda: browser.active is None)
        self.assertEqual(browser.search.text, "unique needle")
        self.assertEqual(browser.rows[browser.selected].id, ids[45])
        self.assertTrue(browser.app.layout.has_focus(browser.result_control))

    async def test_select_refresh_project_filter_and_agent_cycle(self):
        self.session(sid="older", title="Old", updated=1000)
        self.session(agent="pi", sid="newer", title="New", updated=2000, cwd="/work/web")
        browser = await self.start()
        await self.keys("\t\t\x1b[B")  # Search -> project -> results, then select second row.
        await self.wait(lambda: browser.selected == 1)
        self.assertEqual(browser.rows[browser.selected].title, "Old")
        self.session(sid="latest", title="Latest", updated=3000)
        await self.keys("\x1b[15~")  # F5 refresh, preserve the selected identity despite reorder.
        await self.wait(lambda: len(browser.rows) == 3 and not browser.loading)
        self.assertEqual(browser.rows[browser.selected].title, "Old")
        await self.keys("/\tweb\r")  # Project filter is editable in the UI.
        await self.wait(lambda: browser.applied_project == "web" and not browser.loading)
        self.assertEqual([r.title for r in browser.rows], ["New"])
        await self.keys("\x1bOQ")  # F2: all -> Claude; no results with project=web.
        await self.wait(lambda: browser.agent == "claude" and not browser.loading)
        self.assertEqual(browser.rows, [])

    async def test_conversation_paging_and_tools_toggle(self):
        _, ids = self.session(texts=tuple(("tool_result", f"tool {i}") if i % 2 else f"message {i}"
                                         for i in range(75)))
        browser = await self.start()
        await self.keys("\t\t\r")
        await self.wait(lambda: browser.active is not None and not browser.loading)
        self.assertEqual(browser.page.offset, 0)
        await self.keys("\x1b[18~")  # F7 next page.
        await self.wait(lambda: browser.page.offset == MESSAGE_PAGE and not browser.loading)
        self.assertEqual(browser.page.messages[0].id, ids[MESSAGE_PAGE])
        await self.keys("\x1b[17~")  # F6 previous page.
        await self.wait(lambda: browser.page.offset == 0 and not browser.loading)
        await self.keys("\x1bOR")  # F3 hides tools.
        await self.wait(lambda: not browser.show_tools and not browser.loading)
        self.assertEqual(browser.page.total, 38)
        self.assertNotIn("tool_result", browser.body.text)

    async def test_tool_toggle_on_later_pages_preserves_place_without_drift(self):
        _, ids = self.session(texts=tuple(
            ("tool_result", f"tool {i}") if i % 2 == 0 else f"message {i}" for i in range(90)))
        browser = await self.start()
        await self.keys("\t\t\r")
        await self.wait(lambda: browser.active is not None and not browser.loading)
        await self.keys("\x1b[18~")
        await self.wait(lambda: browser.page.offset == 30 and not browser.loading)
        self.assertEqual(browser.page.messages[0].id, ids[30])
        await self.keys("\x1bOR")
        await self.wait(lambda: not browser.show_tools and not browser.loading)
        self.assertEqual(browser.page.messages[0].id, ids[31])
        for tools in (True, False, True):
            await self.keys("\x1bOR")
            await self.wait(lambda: browser.show_tools == tools and not browser.loading)
            self.assertEqual(browser.page.messages[0].id, ids[31])

    async def test_wrapped_paragraph_can_be_read_without_skipping_any_screenful(self):
        paragraph = " ".join(f"word{i:05}" for i in range(2000))
        self.session(texts=(paragraph,))
        browser = await self.start()
        await self.keys("\t\t\r")
        await self.wait(lambda: browser.active is not None and not browser.loading
                        and browser.body.window.render_info is not None
                        and browser.body.window.render_info.ui_content.line_count > 1)

        def visible_span():
            info = browser.body.window.render_info
            positions = sorted(info.visible_line_to_row_col.items())
            first, last = positions[0][1], positions[-1][1]
            doc = browser.body.document
            start = doc.translate_row_col_to_index(*first)
            end = doc.translate_row_col_to_index(last[0], last[1] + info.window_width)
            return start, end

        start, end = visible_span()
        self.assertIn("word00000", browser.body.text[start:end])
        for _ in range(30):
            if "word01999" in browser.body.text[start:end]:
                break
            previous_end = end
            render = browser.app.render_counter
            await self.keys("\x1b[6~")  # Page Down, through real wrapped screen lines.
            await self.wait(lambda: browser.app.render_counter > render)
            start, end = visible_span()
            self.assertLessEqual(start, previous_end, "Page Down skipped unread paragraph text")
            self.assertGreater(end, previous_end, "Page Down did not reveal more paragraph text")
        else:
            self.fail("The end of a wrapped paragraph was not reachable")
        for _ in range(30):
            if "word00000" in browser.body.text[start:end]:
                break
            previous_start = start
            render = browser.app.render_counter
            await self.keys("\x1b[5~")  # Page Up must work within the same logical line too.
            await self.wait(lambda: browser.app.render_counter > render)
            start, end = visible_span()
            self.assertGreaterEqual(end, previous_start, "Page Up skipped paragraph text")
            self.assertLess(start, previous_start)
        else:
            self.fail("The start of a wrapped paragraph was not reachable")

    async def test_load_more_and_empty_enter_are_safe(self):
        browser = await self.start()
        await self.keys("\t\t\r\x1b[B")
        await self.wait(lambda: browser.app.layout.has_focus(browser.result_control))
        self.assertIsNone(browser.active)
        self.assertEqual(browser.selected, 0)
        for i in range(102):
            self.session(sid=str(i), title=f"Conversation {i}", updated=1000 + i)
        await self.keys("\x1b[15~")
        await self.wait(lambda: len(browser.rows) == 100 and not browser.loading)
        self.assertTrue(browser.has_more)
        await self.keys("m")
        await self.wait(lambda: len(browser.rows) == 102 and not browser.loading)
        self.assertFalse(browser.has_more)

    async def test_database_error_is_visible_and_refresh_recovers(self):
        self.session()
        browser = await self.start()
        with mock.patch.object(browser.reader, "results", side_effect=sqlite3.OperationalError("locked")):
            await self.keys("\x1b[15~")
            await self.wait(lambda: bool(browser.error) and not browser.loading)
            self.assertIn("locked", browser.status())
            self.assertIn("F5", browser.status())
        await self.keys("\x1b[15~")
        await self.wait(lambda: not browser.error and not browser.loading)
        self.assertEqual(len(browser.rows), 1)

    async def test_slow_old_search_cannot_replace_new_results_or_block_input(self):
        self.session(sid="slow", texts=("slow",))
        self.session(sid="fast", texts=("fast",))
        browser = await self.start()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = browser.reader.results

        def delayed(text, *args, **kwargs):
            if text == "slow":
                started.set()
                release.wait(timeout=5)
                try:
                    return original(text, *args, **kwargs)
                finally:
                    finished.set()
            return original(text, *args, **kwargs)

        with mock.patch.object(browser.reader, "results", side_effect=delayed):
            try:
                await self.keys("slow\r")
                await self.wait(started.is_set)
                await self.keys("/\x01\x0bfast\r")  # Replace the query while the worker is blocked.
                await self.wait(lambda: browser.applied_text == "fast" and not browser.loading)
                self.assertEqual([r.session_id for r in browser.rows], ["fast"])
            finally:
                release.set()
            await self.wait(finished.is_set)
            self.assertEqual([r.session_id for r in browser.rows], ["fast"])


class CliTest(IndexFixture, unittest.TestCase):
    def test_noninteractive_calls_never_launch_tui(self):
        for args in ([], ["tui"]):
            result = subprocess.run([sys.executable, "-m", "brain", *args],
                                    input="", capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires a terminal", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("\x1b", result.stderr)

    def test_json_cli_does_not_import_tui_library(self):
        self.session(title="Headless still works")
        code = ("import sys; from brain.cli import main; "
                "main(['--db', sys.argv[1], 'list', '--json']); "
                "assert 'prompt_toolkit' not in sys.modules")
        result = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["title"], "Headless still works")

    def test_terminal_default_and_explicit_tui_forward_options(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), mock.patch("brain.cli.tui.run") as run:
            cli.main(["--db", str(self.path)])
            self.assertEqual(run.call_args.args, (self.path,))
            cli.main(["--db", str(self.path), "tui", "rate", "limit", "--agent", "pi",
                      "--project", "api", "--since", "2026-01-01", "--all"])
            self.assertEqual(run.call_args.kwargs["text"], "rate limit")
            kwargs = run.call_args.kwargs
            self.assertEqual((kwargs["agent"], kwargs["project"]), ("pi", "api"))
            self.assertIsInstance(kwargs["since"], int)
            self.assertTrue(run.call_args.kwargs["subagents"])
            run.reset_mock()
            with self.assertRaisesRegex(SystemExit, "invalid --since"):
                cli.main(["tui", "--since", "yesterdayish"])
            run.assert_not_called()


@unittest.skipUnless(os.name == "posix", "PTY integration requires a POSIX terminal")
class TerminalTest(IndexFixture, unittest.TestCase):
    def test_bare_command_search_and_exit_restore_terminal(self):
        import select
        import termios
        import time

        self.session(title="PTY conversation", texts=("terminal needle",))
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        termios.tcsetwinsize(slave, (24, 100))
        original = termios.tcgetattr(slave)
        process = subprocess.Popen(
            [sys.executable, "-m", "brain", "--db", str(self.path)],
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        output = bytearray()

        def expect(text):
            start = len(output)
            deadline = time.monotonic() + 5
            while text not in output[start:]:
                self.assertLess(time.monotonic(), deadline, repr(bytes(output)))
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    chunk = os.read(master, 65536)
                    output.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        os.write(master, b"\x1b[1;1R")  # Terminal cursor-position response.
                if process.poll() is not None:
                    self.fail(f"TUI exited early: {bytes(output)!r}")

        try:
            expect(b"PTY conversation")
            os.write(master, b"needle\r")
            expect(b"[needle]")
            os.write(master, b"\r")
            expect(b"MATCH")
            os.write(master, b"\x11")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(termios.tcgetattr(slave), original)
            self.assertNotIn(b"Traceback", output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
