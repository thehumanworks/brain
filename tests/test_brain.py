import ast
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from compression import zstd
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import brain
from brain import (AmbiguousSession, Brain, Commit, IndexFormatError, IndexResult, SessionNotFound,
                   project_root, query)
from brain.cli import main

# The variables an agent session sets, which would change what a brief leaves out.
AGENT_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "PI_SESSION_ID", "PI_CODING_AGENT",
             "CLAUDE_CODE_ENTRYPOINT")


def jsonl(*records) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


def claude(uuid, kind, content, ts="2026-10-28T10:00:00Z", **extra):
    return {"type": kind, "uuid": uuid, "timestamp": ts, "cwd": "/work/app",
            "message": {"role": kind, "content": content}, **extra}


def codex(ptype, ts="2026-10-28T10:00:00Z", **payload):
    return {"type": "response_item", "timestamp": ts, "payload": {"type": ptype, **payload}}


PI_REPLY = {"type": "message", "id": "e1", "timestamp": "2026-10-28T10:00:00Z", "message": {
    "role": "assistant", "content": [
        {"type": "thinking", "thinking": "check the size"},
        {"type": "text", "text": "Resizing it now."},
        {"type": "toolCall", "name": "bash", "arguments": {"command": "magick hero.png"}}]}}


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
        for name in AGENT_ENV:
            os.environ.pop(name, None)
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

    def test_pi_reply_is_a_message_per_block_and_its_brief_is_not_indexed(self):
        self.write("pi/sessions/--work-site--/2026-10-28T10-00-00-000Z_p1.jsonl", jsonl(
            {"type": "session", "id": "p1", "cwd": "/work/site"},
            {"type": "custom_message", "id": "c1", "timestamp": "2026-10-28T10:00:00Z",
             "customType": "brain-brief", "content": "Earlier sessions in ~/site", "display": False},
            PI_REPLY,
        ))
        self.brain.index()
        rows = [tuple(r) for r in self.db.execute("SELECT kind, text FROM messages ORDER BY id")]
        self.assertEqual(rows, [("thinking", "check the size"), ("text", "Resizing it now."),
                                ("tool_call", "bash\ncommand: magick hero.png")])

    def test_upgrade_from_format_1_reads_pi_again_unless_its_transcript_is_gone(self):
        self.write("pi/sessions/--work--/2026-10-28T10-00-00-000Z_p1.jsonl", jsonl(PI_REPLY))
        gone = self.write("pi/sessions/--work--/2026-10-28T10-00-00-000Z_p2.jsonl", jsonl(PI_REPLY))
        self.brain.index()
        # As format 1 left it: a reply is one message, of its first block's kind.
        self.db.execute("DELETE FROM messages")
        for sid in ("p1", "p2"):
            self.db.execute("INSERT INTO messages (agent, session_id, source_id, ts, role, kind, text) "
                            "VALUES ('pi', ?, 'old', 0, 'assistant', 'thinking', 'all of it')", (sid,))
        self.db.execute("PRAGMA user_version = 1")
        gone.unlink()
        with Brain(self.brain.path, readonly=True) as reader:  # reads it as it is
            self.assertEqual(len(reader.sessions()), 2)
        with Brain(self.brain.path) as upgraded:
            upgraded.index()
            rows = [tuple(r) for r in upgraded.db.execute(
                "SELECT session_id, kind FROM messages ORDER BY session_id, id")]
            self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(rows, [("p1", "thinking"), ("p1", "text"), ("p1", "tool_call"),
                                ("p2", "thinking")])

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


class BriefTest(Fixture):
    """`brain brief`: the latest sessions in a project, for an agent starting work there."""

    def setUp(self):
        super().setUp()
        self.proj = self.root.resolve() / "proj"
        (self.proj / ".git").mkdir(parents=True)
        self.wt = self.proj / ".claude" / "worktrees" / "wt"
        self.wt.mkdir(parents=True)
        (self.wt / ".git").write_text(f"gitdir: {self.proj}/.git/worktrees/wt\n")
        edit = lambda path: {"type": "tool_use", "name": "Edit",
                             "input": {"file_path": str(path), "old_string": "a", "new_string": "b"}}
        bash = lambda cmd: {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        result = lambda out: ("user", [{"type": "tool_result", "content": out}])
        self.session("s-other", self.root.resolve() / "elsewhere", 0, ("user", "other work"),
                     ("assistant", "Done elsewhere."))
        self.session("s-old", self.proj, 1, ("user", "add the parser"),
                     ("assistant", [edit(self.proj / "src" / "parse.py")]), result("ok"),
                     ("assistant", [bash('git add -A && git commit -q -m "Add the parser"')]),
                     result(""), ("assistant", [bash("git log --oneline -1")]),
                     result("abc1234 Add the parser"), ("assistant", "The parser is in."))
        self.session("s-gone", self.root.resolve() / "old" / "proj", 2, ("user", "start the project"),
                     ("assistant", "Started."))
        self.session("s-wt", self.wt, 3, ("user", "first try"), ("assistant", "Tried."),
                     ("user", "[Request interrupted by user]"), ("user", "now the lexer"),
                     ("assistant", "Half done."), ("assistant", [edit(self.wt / "lex.py")]))
        sid = "01a1660f-8cda-7791-a841-fb4a30195445"
        self.write(f"codex/sessions/2026/10/28/rollout-2026-10-28T10-04-00-{sid}.jsonl", jsonl(
            {"type": "session_meta", "payload": {"id": sid, "cwd": str(self.proj)}},
            codex("message", ts="2026-10-28T10:04:00Z", role="user",
                  content=[{"type": "input_text", "text": "add the API"}]),
            codex("custom_tool_call", ts="2026-10-28T10:04:01Z", name="apply_patch",
                  input="*** Begin Patch\n*** Add File: src/api.rs\n+fn main() {}\n*** End Patch"),
            codex("function_call_output", ts="2026-10-28T10:04:02Z",
                  output="[main 1a2b3c4] Add the API\n 1 file changed, 1 insertion(+)"),
            codex("message", ts="2026-10-28T10:04:03Z", role="assistant",
                  content=[{"type": "output_text", "text": "Added the API."}]),
        ))
        self.codex_id = sid
        self.session("s-rerun-1", self.proj, 5, ("user", "nightly check"), ("assistant", "Fine."))
        self.session("s-rerun-2", self.proj, 6, ("user", "nightly check"), ("assistant", "Still fine."))
        self.session("s-empty", self.proj, 7, ("user", "Hi"))
        self.session("s-mirror", self.proj, 8, ("user", "from pi"), ("assistant", [
            {"type": "tool_use", "name": "mcp__custom-tools__bash", "input": {"command": "ls"}}]),
            ("assistant", "Listed."))
        self.session("s-current", self.proj, 9, ("user", "what happened here?"), ("assistant", "Lots."))
        self.brain.index()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s-current"

    def session(self, sid, cwd, minute, *records):
        lines = [claude(f"{sid}-{n}", kind, content, ts=f"2026-10-28T10:0{minute}:{n:02}Z", cwd=str(cwd))
                 for n, (kind, content) in enumerate(records)]
        self.write(f"claude/projects/x/{sid}.jsonl", jsonl(*lines))

    def recent(self, path=None, **kw):
        with mock.patch("brain.brief.TEMP", ("/nowhere/",)):  # the fixture itself is in one
            return self.brain.recent_work(path or self.proj, **kw)

    def test_latest_sessions_in_the_project_and_where_it_was(self):
        ids = [b.session.session_id for b in self.recent(limit=10)]
        # Not the current session, Claude Code's copy of a Pi one, one with nothing to report,
        # an earlier run of the same task, or another project's.
        self.assertEqual(ids, ["s-rerun-2", self.codex_id, "s-wt", "s-gone", "s-old"])
        self.assertEqual([b.session.session_id for b in self.recent(limit=2)],
                         ["s-rerun-2", self.codex_id])
        self.assertEqual(project_root(self.wt / "sub"), self.proj)
        self.assertEqual([b.session.session_id for b in self.recent(self.wt, limit=10)], ids)
        self.assertEqual(len(self.recent(limit=10, exclude=["s-old"])), 4)

    def test_brief_tells_what_was_asked_how_it_ended_and_what_changed(self):
        old, wt, api = (self.brain.brief(ref) for ref in ("s-old", "s-wt", self.codex_id))
        self.assertEqual((old.prompts, old.last_prompt, old.outcome, old.finished),
                         (1, "add the parser", "The parser is in.", True))
        self.assertEqual(old.files, [str(self.proj / "src" / "parse.py")])
        self.assertEqual(old.commits, [Commit("abc1234", "", "Add the parser")])
        # Interrupted mid-turn: "Half done." led to a tool call, so the outcome is the reply that
        # ended the turn before.
        self.assertEqual((wt.prompts, wt.last_prompt, wt.outcome, wt.finished),
                         (2, "now the lexer", "Tried.", False))
        self.assertEqual((api.files, api.commits),
                         ([str(self.proj / "src" / "api.rs")], [Commit("1a2b3c4", "main", "Add the API")]))
        self.assertEqual(list(asdict(old)), ["session", "prompts", "last_prompt", "outcome",
                                             "finished", "files", "commits"])

    def test_commits_are_those_the_repository_has(self):
        repo = self.root.resolve() / "repo"
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                                        check=True).stdout
        repo.mkdir()
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "Real")
        real = git("rev-parse", "--short", "HEAD").strip()
        # Commit lines from another repository, as when searching old transcripts, don't count.
        self.session("s-repo", repo, 0, ("user", "commit it"), ("assistant", [
            {"type": "tool_use", "name": "Bash", "input": {"command": "git commit -am Real"}}]),
            ("user", [{"type": "tool_result", "content": f"[main {real}] Real\n 1 file changed\n"
                                                      "[main 0123abc] Elsewhere\n 2 files changed"}]),
            ("assistant", "Committed."))
        self.brain.index()
        self.assertEqual(self.brain.brief("s-repo").commits, [Commit(real, "main", "Real")])

    def run_cli(self, *args, stdin=""):
        out = io.StringIO()
        with (mock.patch("brain.brief.TEMP", ("/nowhere/",)), mock.patch("sys.stdin", io.StringIO(stdin)),
              redirect_stdout(out)):
            main(["--db", str(self.brain.path), "brief", *args])
        return out.getvalue()

    def test_cli_and_hook(self):
        text = self.run_cli(str(self.proj), "-n", "5")
        self.assertIn("newest first", text.splitlines()[0])
        self.assertIn("in .claude/worktrees/wt", text)
        self.assertIn("which no longer exists", text)
        self.assertIn("  2 prompts, the last: now the lexer", text)
        self.assertIn("  unfinished; last turn-ending reply: Tried.", text)
        self.assertIn("  changed: src/parse.py", text)
        self.assertIn("  commits: abc1234 Add the parser", text)
        event = {"session_id": "s-rerun-2", "cwd": str(self.proj), "source": "startup"}
        hook = self.run_cli("--hook", stdin=json.dumps(event))
        # The session starting is left out, so the run of its task before it is the latest.
        self.assertNotIn("Still fine.", hook)
        self.assertIn("  outcome: Fine.", hook)
        self.assertEqual(self.run_cli("--hook", stdin=json.dumps({**event, "source": "resume"})), "")
        self.assertEqual(self.run_cli("--hook", stdin="not json"), "")
        with mock.patch.dict(os.environ, {"PI_CODING_AGENT": "true"}):  # Pi adds its own
            self.assertEqual(self.run_cli("--hook", stdin=json.dumps(event)), "")


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
