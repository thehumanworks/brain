import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from compression import zstd
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import brain
from brain import AmbiguousSession, Brain, IndexFormatError, IndexResult, SessionNotFound, query


def jsonl(*records) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


def claude(uuid, kind, content, ts="2026-10-28T10:00:00Z", **extra):
    return {"type": kind, "uuid": uuid, "timestamp": ts, "cwd": "/work/app",
            "message": {"role": kind, "content": content}, **extra}


def codex(ptype, ts="2026-10-28T10:00:00Z", **payload):
    return {"type": "response_item", "timestamp": ts, "payload": {"type": ptype, **payload}}


class Fixture(unittest.TestCase):
    """Transcript directories and an index of their own, never your real ones."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = {
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "PI_CODING_AGENT_DIR": str(self.root / "pi"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.brain = Brain(self.root / "index.db")
        self.addCleanup(self.brain.close)
        self.db = self.brain.db

    def write(self, rel: str, data: bytes, append: bool = False) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab" if append else "wb") as f:
            f.write(data)
        # Size and mtime are what tell the indexer a file changed; make sure mtime moves.
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return path

    def texts(self, session_id=None) -> list[str]:
        sql = "SELECT text FROM messages" + (" WHERE session_id = ?" if session_id else "") + " ORDER BY id"
        return [r[0] for r in self.db.execute(sql, (session_id,) if session_id else ())]

    def hits(self, q: str, **filters) -> list:
        return self.brain.search(q, **filters)


class BrainTest(Fixture):
    def test_claude_session_is_searchable(self):
        self.write("claude/projects/-work-app/s1.jsonl", jsonl(
            claude("u1", "user", "why does the deploy fail on staging?"),
            claude("a1", "assistant", [
                {"type": "thinking", "thinking": "look at the pipeline"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "kubectl logs deploy/api"}},
            ]),
            claude("u2", "user", [{"type": "tool_result", "content": "OOMKilled"}]),
            {"type": "custom-title", "customTitle": "Staging deploy", "sessionId": "s1"},
        ))
        result = self.brain.index()
        self.assertEqual(result.messages, 3)
        roles = [r[0] for r in self.db.execute("SELECT role || '/' || kind FROM messages ORDER BY id")]
        self.assertEqual(roles, ["user/text", "assistant/thinking", "tool/tool_result"])
        session = self.db.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual((session["cwd"], session["custom_title"]), ("/work/app", "Staging deploy"))
        self.assertEqual(self.hits("oomkilled")[0].role, "tool")
        self.assertEqual(self.hits("deploy staging")[0].role, "user")
        self.assertEqual(self.hits("kubectl", role="assistant")[0].kind, "thinking")

    def test_claude_slash_command_reads_as_typed(self):
        self.write("claude/projects/-work-app/s1.jsonl", jsonl(claude("u1", "user",
            "<command-name>/plugin</command-name>\n  <command-message>plugin</command-message>\n"
            "  <command-args>marketplace add ~/plugins</command-args>")))
        self.brain.index()
        self.assertEqual(self.texts(), ["/plugin marketplace add ~/plugins"])

    def test_claude_subagent_links_to_parent(self):
        self.write("claude/projects/-work-app/s1/subagents/agent-x.jsonl",
                   jsonl(claude("u1", "user", "investigate flaky test", isSidechain=True)))
        self.brain.index()
        row = self.db.execute("SELECT session_id, parent_id FROM sessions").fetchone()
        self.assertEqual(tuple(row), ("agent-x", "s1"))

    def test_list_is_newest_first_without_subagents(self):
        for sid, ts in (("old", "2026-10-01T10:00:00Z"), ("new", "2026-10-28T10:00:00Z"),
                        ("mid", "2026-10-15T10:00:00Z")):
            self.write(f"claude/projects/-work-app/{sid}.jsonl",
                       jsonl(claude(f"u-{sid}", "user", f"question {sid}", ts=ts)))
        self.write("claude/projects/-work-app/new/subagents/agent-x.jsonl",
                   jsonl(claude("u-x", "user", "subtask", ts="2026-10-28T11:00:00Z")))
        self.brain.index()
        ids = lambda rows: [r.session_id for r in rows]
        self.assertEqual(ids(self.brain.sessions(limit=2)), ["new", "mid"])
        self.assertEqual(ids(self.brain.sessions(subagents=True))[0], "agent-x")
        self.assertEqual(self.brain.sessions(limit=1)[0].title, "question new")

    def test_only_new_lines_are_read(self):
        rel = "claude/projects/-work-app/s1.jsonl"
        self.write(rel, jsonl(claude("u1", "user", "first question")))
        self.brain.index()
        self.write(rel, jsonl(claude("a1", "assistant", "first answer")), append=True)
        result = self.brain.index()
        self.assertEqual(result.messages, 1)
        self.assertEqual(result.rereads, 0)
        self.assertEqual(self.texts(), ["first question", "first answer"])
        self.assertEqual(self.brain.index().messages, 0)

    def test_half_written_line_waits(self):
        rel = "claude/projects/-work-app/s1.jsonl"
        line = jsonl(claude("u1", "user", "complete me"))
        self.write(rel, line[:20])
        self.assertEqual(self.brain.index().messages, 0)
        self.write(rel, line[20:], append=True)
        self.assertEqual(self.brain.index().messages, 1)
        self.assertEqual(self.texts(), ["complete me"])

    def test_rewritten_file_is_read_again(self):
        rel = "claude/projects/-work-app/s1.jsonl"
        self.write(rel, jsonl(claude("u1", "user", "original"), claude("a1", "assistant", "reply")))
        self.brain.index()
        # Same length, different bytes at the checkpoint: only the fingerprint can tell.
        self.write(rel, jsonl(claude("u1", "user", "original"), claude("a2", "assistant", "other")))
        result = self.brain.index()
        self.assertEqual(result.rereads, 1)
        self.assertEqual(result.messages, 1)
        self.assertIn("other", self.texts())

    def test_message_rewritten_under_new_id_is_stored_once(self):
        self.write("claude/projects/-work-app/s1.jsonl", jsonl(
            claude("a1", "assistant", "the fix is in limiter.rs"),
            claude("a1-copy", "assistant", "the fix is in limiter.rs"),
            claude("a2", "assistant", "the fix is in limiter.rs", ts="2026-10-28T10:05:00Z"),
        ))
        self.assertEqual(self.brain.index().messages, 2)

    def test_codex_compression_neither_loses_nor_repeats(self):
        sid = "01a1660f-8cda-7791-a841-fb4a30195445"
        lines = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": "/work/api"}},
            codex("message", role="user", content=[{"type": "input_text", "text": "add rate limiting"}]),
            codex("message", role="user", content=[{"type": "input_text", "text": "<environment_context>x"}]),
            codex("function_call", name="shell", arguments=json.dumps({"cmd": "rg limiter"})),
            codex("function_call_output", call_id="c1", output="src/limiter.rs"),
        ]
        live = f"codex/sessions/2026/10/28/rollout-2026-10-28T10-00-00-{sid}.jsonl"
        path = self.write(live, jsonl(*lines))
        self.assertEqual(self.brain.index().messages, 4)

        # Codex later compresses the rollout in place, sometimes after one last line.
        lines.append(codex("message", role="assistant", content=[{"type": "output_text", "text": "done"}]))
        self.write(live + ".zst", zstd.compress(jsonl(*lines)))
        path.unlink()
        result = self.brain.index()
        self.assertEqual((result.messages, result.rereads), (1, 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 5)
        kinds = [r[0] for r in self.db.execute("SELECT role || '/' || kind FROM messages ORDER BY id")]
        self.assertEqual(kinds, ["user/text", "system/context", "assistant/tool_call", "tool/tool_result",
                                 "assistant/text"])
        self.assertEqual(self.hits("limiter", agent="codex")[0].cwd, "/work/api")

    def test_codex_titles_come_from_the_session_index(self):
        sid = "01a1660f-8cda-7791-a841-fb4a30195445"
        self.write(f"codex/sessions/2026/10/28/rollout-2026-10-28T10-00-00-{sid}.jsonl",
                   jsonl(codex("message", role="user", content=[{"type": "input_text", "text": "hi"}])))
        self.write("codex/session_index.jsonl", jsonl({"id": sid, "thread_name": "Rate limiting"}))
        self.brain.index()
        self.assertEqual(self.db.execute("SELECT title FROM sessions").fetchone()[0], "Rate limiting")

    def test_pi_session(self):
        self.write("pi/sessions/--work-site--/2026-10-28T10-00-00-000Z_01a0d077.jsonl", jsonl(
            {"type": "session", "version": 3, "id": "01a0d077", "cwd": "/work/site"},
            {"type": "message", "id": "e1", "message": {"role": "user", "content": [{"type": "text", "text": "resize the hero image"}]}},
            {"type": "message", "id": "e2", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "name": "bash", "arguments": {"command": "magick hero.png -resize 50% out.png"}}]}},
            {"type": "message", "id": "e3", "message": {"role": "toolResult", "content": [{"type": "text", "text": "ok"}]}},
        ))
        self.assertEqual(self.brain.index().messages, 3)
        hit = self.hits("hero.png")[0]
        self.assertEqual((hit.agent, hit.kind, hit.cwd), ("pi", "tool_call", "/work/site"))

    def test_query_punctuation_is_literal(self):
        self.assertEqual(query.fts_query("git push --force-with-lease"), '"git" "push" "--force-with-lease"')
        self.assertEqual(query.fts_query('"rate limit" deploy*'), '"rate limit" "deploy"*')
        self.assertEqual(query.fts_query("a OR b", any_term=False), '"a" "OR" "b"')
        self.assertEqual(query.fts_query("- ' \""), "")


class SdkTest(Fixture):
    """The scripting API, as the `brain` command and other scripts use it."""

    FIRST = "aaaa1111-0000-4000-8000-000000000001"
    SECOND = "aaaa1111-0000-4000-8000-000000000002"

    def setUp(self):
        super().setUp()
        self.write(f"claude/projects/-work-app/{self.FIRST}.jsonl", jsonl(
            claude("u1", "user", "deploy question", ts="2026-10-01T10:00:00Z"),
            claude("a1", "assistant", [{"type": "tool_use", "name": "Bash",
                                        "input": {"command": "make deploy"}}], ts="2026-10-01T10:00:01Z"),
            claude("a2", "assistant", "deployed", ts="2026-10-01T10:00:02Z"),
        ))
        self.write(f"claude/projects/-work-app/{self.SECOND}.jsonl",
                   jsonl(claude("u1", "user", "another question", ts="2026-10-02T10:00:00Z")))
        self.brain.index()

    def test_session_by_full_id_start_or_end(self):
        self.assertEqual(self.brain.session(self.FIRST).session_id, self.FIRST)
        self.assertEqual(self.brain.session("00000001").session_id, self.FIRST)  # as `list` shows it
        with self.assertRaises(AmbiguousSession) as caught:
            self.brain.session("aaaa1111")
        self.assertEqual([s.session_id for s in caught.exception.matches], [self.SECOND, self.FIRST])
        with self.assertRaisesRegex(SessionNotFound, "no session matches 'nothing'"):
            self.brain.session("nothing")

    def test_messages_by_id_around_a_hit_without_tools_and_paged(self):
        texts = lambda rows: [m.text for m in rows]
        hit = self.hits("deployed")[0]
        self.assertEqual(texts(self.brain.messages("00000001")),
                         ["deploy question", "Bash\ncommand: make deploy", "deployed"])
        self.assertEqual(texts(self.brain.messages(hit, at=hit.id, context=1)),
                         ["Bash\ncommand: make deploy", "deployed"])
        self.assertEqual(texts(self.brain.messages(hit, tools=False)), ["deploy question", "deployed"])
        page = self.brain.page(hit, limit=1, anchor=hit.id, context=0)
        self.assertEqual((texts(page.messages), page.offset, page.total), (["deployed"], 2, 3))

    def test_since_takes_dates_datetimes_ms_and_durations(self):
        cutoff = datetime(2026, 10, 2, tzinfo=timezone.utc)
        for since in (cutoff, int(cutoff.timestamp() * 1000), "2026-10-02T00:00:00+00:00"):
            with self.subTest(since=since):
                self.assertEqual([s.session_id for s in self.brain.sessions(since=since)], [self.SECOND])
                self.assertEqual([h.session_id for h in self.hits("question", since=since)], [self.SECOND])
        self.assertEqual(len(self.brain.sessions(since="1000w")), 2)
        with self.assertRaises(ValueError):
            self.brain.sessions(since="yesterdayish")

    def test_readonly_never_writes_and_index_format_is_checked(self):
        missing = self.root / "absent" / "index.db"
        with self.assertRaises(sqlite3.OperationalError):
            Brain(missing, readonly=True)
        self.assertFalse(missing.parent.exists())
        self.write("claude/projects/-work-app/new.jsonl", jsonl(claude("u1", "user", "new")))
        with Brain(self.brain.path, readonly=True) as reader:
            self.assertEqual(len(reader.sessions()), 2)
            with self.assertRaises(sqlite3.OperationalError):
                reader.index()
        self.db.execute("PRAGMA user_version = 999")
        for readonly in (False, True):
            with self.subTest(readonly=readonly), self.assertRaisesRegex(IndexFormatError, "format 999"):
                Brain(self.brain.path, readonly=readonly)

    def test_stats_and_watch(self):
        stats = self.brain.stats()
        self.assertEqual([(a.agent, a.sessions, a.messages) for a in stats.agents], [("claude", 2, 4)])
        self.assertEqual(stats.path, self.root / "index.db")
        self.assertGreater(stats.size, 0)
        self.write("pi/sessions/--work--/2026-10-03T10-00-00-000Z_p1.jsonl", jsonl(
            {"type": "message", "id": "e1", "timestamp": "2026-10-03T10:00:00Z",
             "message": {"role": "user", "content": "watched"}}))
        runs = self.brain.watch(interval=0)
        self.addCleanup(runs.close)
        first = next(runs)
        self.assertIsInstance(first, IndexResult)
        self.assertEqual((first.files, first.messages), (1, 1))
        self.assertEqual(next(runs).messages, 0)

    def test_results_serialize_to_the_cli_json_fields_in_order(self):
        hit = self.hits("deployed")[0]
        self.assertEqual(list(asdict(hit)), ["id", "agent", "session_id", "ts", "role", "kind", "score",
                                             "nth", "cwd", "title", "parent_id", "snippet"])
        self.assertEqual(list(asdict(self.brain.session(hit.session_id))),
                         ["agent", "session_id", "parent_id", "cwd", "title", "started_at", "updated_at",
                          "messages", "path"])
        self.assertEqual(list(asdict(self.brain.messages(hit)[0])), ["id", "ts", "role", "kind", "text"])


class LayeringTest(unittest.TestCase):
    """The SDK stands alone, and the command line is built on its public API only."""

    def test_sdk_loads_neither_the_cli_nor_the_terminal_library(self):
        code = ("import sys, brain; "
                "assert not {'brain.cli', 'prompt_toolkit'} & set(sys.modules), 'loaded'")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_imports_only_the_public_sdk(self):
        for path in (Path(brain.__file__).parent / "cli").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    imports = [(a.name, None) for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports = [(node.module, a.name) for a in node.names]
                else:
                    continue
                for module, name in imports:
                    with self.subTest(file=path.name, module=module, name=name):
                        if module == "brain":
                            self.assertTrue(name is None or name in brain.__all__)
                        elif module.startswith("brain."):
                            self.assertTrue(module.startswith("brain.cli"))


if __name__ == "__main__":
    unittest.main()
