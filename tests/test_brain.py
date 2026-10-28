import json
import os
import tempfile
import unittest
from compression import zstd
from pathlib import Path
from unittest import mock

from brain import db as dbmod
from brain import query
from brain.indexer import index_all


def jsonl(*records) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


def claude(uuid, kind, content, ts="2026-10-28T10:00:00Z", **extra):
    return {"type": kind, "uuid": uuid, "timestamp": ts, "cwd": "/work/app",
            "message": {"role": kind, "content": content}, **extra}


def codex(ptype, ts="2026-10-28T10:00:00Z", **payload):
    return {"type": "response_item", "timestamp": ts, "payload": {"type": ptype, **payload}}


class BrainTest(unittest.TestCase):
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
        self.db = dbmod.connect(self.root / "index.db")
        self.addCleanup(self.db.close)

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

    def hits(self, q: str, **filters) -> list[dict]:
        return query.search(self.db, q, query.Filters(**filters))

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
        result = index_all(self.db)
        self.assertEqual(result.messages, 3)
        roles = [r[0] for r in self.db.execute("SELECT role || '/' || kind FROM messages ORDER BY id")]
        self.assertEqual(roles, ["user/text", "assistant/thinking", "tool/tool_result"])
        session = self.db.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual((session["cwd"], session["custom_title"]), ("/work/app", "Staging deploy"))
        self.assertEqual(self.hits("oomkilled")[0]["role"], "tool")
        self.assertEqual(self.hits("deploy staging")[0]["role"], "user")
        self.assertEqual(self.hits("kubectl", role="assistant")[0]["kind"], "thinking")

    def test_claude_slash_command_reads_as_typed(self):
        self.write("claude/projects/-work-app/s1.jsonl", jsonl(claude("u1", "user",
            "<command-name>/plugin</command-name>\n  <command-message>plugin</command-message>\n"
            "  <command-args>marketplace add ~/plugins</command-args>")))
        index_all(self.db)
        self.assertEqual(self.texts(), ["/plugin marketplace add ~/plugins"])

    def test_claude_subagent_links_to_parent(self):
        self.write("claude/projects/-work-app/s1/subagents/agent-x.jsonl",
                   jsonl(claude("u1", "user", "investigate flaky test", isSidechain=True)))
        index_all(self.db)
        row = self.db.execute("SELECT session_id, parent_id FROM sessions").fetchone()
        self.assertEqual(tuple(row), ("agent-x", "s1"))

    def test_list_is_newest_first_without_subagents(self):
        for sid, ts in (("old", "2026-10-01T10:00:00Z"), ("new", "2026-10-28T10:00:00Z"),
                        ("mid", "2026-10-15T10:00:00Z")):
            self.write(f"claude/projects/-work-app/{sid}.jsonl",
                       jsonl(claude(f"u-{sid}", "user", f"question {sid}", ts=ts)))
        self.write("claude/projects/-work-app/new/subagents/agent-x.jsonl",
                   jsonl(claude("u-x", "user", "subtask", ts="2026-10-28T11:00:00Z")))
        index_all(self.db)
        ids = lambda rows: [r["session_id"] for r in rows]
        self.assertEqual(ids(query.sessions(self.db, query.Filters(), limit=2)), ["new", "mid"])
        self.assertEqual(ids(query.sessions(self.db, query.Filters(), subagents=True))[0], "agent-x")
        self.assertEqual(query.sessions(self.db, query.Filters(), limit=1)[0]["title"], "question new")

    def test_only_new_lines_are_read(self):
        rel = "claude/projects/-work-app/s1.jsonl"
        self.write(rel, jsonl(claude("u1", "user", "first question")))
        index_all(self.db)
        self.write(rel, jsonl(claude("a1", "assistant", "first answer")), append=True)
        result = index_all(self.db)
        self.assertEqual(result.messages, 1)
        self.assertEqual(result.rereads, 0)
        self.assertEqual(self.texts(), ["first question", "first answer"])
        self.assertEqual(index_all(self.db).messages, 0)

    def test_half_written_line_waits(self):
        rel = "claude/projects/-work-app/s1.jsonl"
        line = jsonl(claude("u1", "user", "complete me"))
        self.write(rel, line[:20])
        self.assertEqual(index_all(self.db).messages, 0)
        self.write(rel, line[20:], append=True)
        self.assertEqual(index_all(self.db).messages, 1)
        self.assertEqual(self.texts(), ["complete me"])

    def test_rewritten_file_is_read_again(self):
        rel = "claude/projects/-work-app/s1.jsonl"
        self.write(rel, jsonl(claude("u1", "user", "original"), claude("a1", "assistant", "reply")))
        index_all(self.db)
        # Same length, different bytes at the checkpoint: only the fingerprint can tell.
        self.write(rel, jsonl(claude("u1", "user", "original"), claude("a2", "assistant", "other")))
        result = index_all(self.db)
        self.assertEqual(result.rereads, 1)
        self.assertEqual(result.messages, 1)
        self.assertIn("other", self.texts())

    def test_message_rewritten_under_new_id_is_stored_once(self):
        self.write("claude/projects/-work-app/s1.jsonl", jsonl(
            claude("a1", "assistant", "the fix is in limiter.rs"),
            claude("a1-copy", "assistant", "the fix is in limiter.rs"),
            claude("a2", "assistant", "the fix is in limiter.rs", ts="2026-10-28T10:05:00Z"),
        ))
        self.assertEqual(index_all(self.db).messages, 2)

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
        self.assertEqual(index_all(self.db).messages, 4)

        # Codex later compresses the rollout in place, sometimes after one last line.
        lines.append(codex("message", role="assistant", content=[{"type": "output_text", "text": "done"}]))
        self.write(live + ".zst", zstd.compress(jsonl(*lines)))
        path.unlink()
        result = index_all(self.db)
        self.assertEqual((result.messages, result.rereads), (1, 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 5)
        kinds = [r[0] for r in self.db.execute("SELECT role || '/' || kind FROM messages ORDER BY id")]
        self.assertEqual(kinds, ["user/text", "system/context", "assistant/tool_call", "tool/tool_result",
                                 "assistant/text"])
        self.assertEqual(self.hits("limiter", agent="codex")[0]["cwd"], "/work/api")

    def test_codex_titles_come_from_the_session_index(self):
        sid = "01a1660f-8cda-7791-a841-fb4a30195445"
        self.write(f"codex/sessions/2026/10/28/rollout-2026-10-28T10-00-00-{sid}.jsonl",
                   jsonl(codex("message", role="user", content=[{"type": "input_text", "text": "hi"}])))
        self.write("codex/session_index.jsonl", jsonl({"id": sid, "thread_name": "Rate limiting"}))
        index_all(self.db)
        self.assertEqual(self.db.execute("SELECT title FROM sessions").fetchone()[0], "Rate limiting")

    def test_pi_session(self):
        self.write("pi/sessions/--work-site--/2026-10-28T10-00-00-000Z_01a0d077.jsonl", jsonl(
            {"type": "session", "version": 3, "id": "01a0d077", "cwd": "/work/site"},
            {"type": "message", "id": "e1", "message": {"role": "user", "content": [{"type": "text", "text": "resize the hero image"}]}},
            {"type": "message", "id": "e2", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "name": "bash", "arguments": {"command": "magick hero.png -resize 50% out.png"}}]}},
            {"type": "message", "id": "e3", "message": {"role": "toolResult", "content": [{"type": "text", "text": "ok"}]}},
        ))
        self.assertEqual(index_all(self.db).messages, 3)
        hit = self.hits("hero.png")[0]
        self.assertEqual((hit["agent"], hit["kind"], hit["cwd"]), ("pi", "tool_call", "/work/site"))

    def test_query_punctuation_is_literal(self):
        self.assertEqual(query.fts_query("git push --force-with-lease"), '"git" "push" "--force-with-lease"')
        self.assertEqual(query.fts_query('"rate limit" deploy*'), '"rate limit" "deploy"*')
        self.assertEqual(query.fts_query("a OR b", any_term=False), '"a" "OR" "b"')
        self.assertEqual(query.fts_query("- ' \""), "")


if __name__ == "__main__":
    unittest.main()
