# brain

Local full-text search over your Claude Code, Codex and Pi conversation history, for you and
for your agents. Everything stays on your machine: one SQLite file, no server, no sync.

```sh
uv tool install --editable ~/brain   # puts `brain` on your PATH
brain index                          # first run reads every transcript (a few minutes)
brain watch                          # keep the index current while you work (Ctrl-C to stop)
```

## Searching

```sh
brain search rate limiting                  # every word must match, best first
brain search '"connection refused"' --since 7d
brain search deploy* --agent codex --project api
brain search oomkilled --role tool          # only tool output
brain list                                  # the 20 most recent sessions (-n for more, --all for subagents)
brain show 3f2a91c0 --at 81234              # open a hit: session id + message id from search
brain show 3f2a91c0 --no-tools              # just the conversation
brain stats
```

Add `--json` to `search`, `list` or `show` for machine-readable output.

Queries are words, `"quoted phrases"` and `prefix*`; punctuation is taken literally, so
`brain search foo.py` works (put `--` before a query that starts with a dash:
`brain search -- --force-with-lease`). `--any` matches any word
instead of all of them, and `--raw` passes the query to
[SQLite FTS5](https://sqlite.org/fts5.html#full_text_query_syntax) untouched (`NEAR`, `OR`,
column filters). Results are ranked with BM25, with tool output and injected context ranked
below what you and the agent actually said.

## Letting agents use it

Add this to `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` and Pi's `~/.pi/agent/AGENTS.md`:

```markdown
## Past conversations
`brain` searches every past Claude Code, Codex and Pi session on this machine. Use it when the
user refers to earlier work ("like we did last week", "that bug from before"), or before
redoing an investigation that may already have been done.
- `brain search <words> [--project <dir>] [--since 7d] [--agent claude|codex|pi]`
- `brain list [-n 20] [--project <dir>]` for the most recent sessions
- `brain show <session> --at <msg> -C 5` to read around a hit; `--no-tools` for just the talk
```

## What it reads

| Agent | Transcripts | Moved with |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl`, plus `*/subagents/` | `CLAUDE_CONFIG_DIR` |
| Codex | `~/.codex/sessions/`, `~/.codex/archived_sessions/` (`.jsonl` and `.jsonl.zst`) | `CODEX_HOME` |
| Pi | `~/.pi/agent/sessions/`, `~/.pi/agent/bots/*/sessions/` | `PI_CODING_AGENT_DIR` |

User prompts, assistant replies, thinking, tool calls and tool output are all indexed; each
message is clipped to 64 KiB. Codex's encrypted reasoning and its repeated system prompt are
skipped. The index lives at `~/.local/share/brain/index.db` (`BRAIN_DB` or `--db` to move it)
and can be deleted and rebuilt at any time.

## How it keeps up

Each transcript has a checkpoint: how many bytes were read and a fingerprint of the last line.
A run skips files whose size and modification time haven't changed, reads only the new lines
of the rest, and starts a file over if the bytes at its checkpoint no longer match (the
transcript was rewritten). A line still being written is left for the next run. Codex
transcripts are tracked by thread id, so a rollout that Codex compresses or archives carries on
from where it was. Messages are keyed by their time and content, so nothing is stored twice,
even when an agent writes a message out again under a new id.

Indexing never changes or deletes a transcript, and nothing is removed from the index when an
agent deletes its own old sessions: Claude Code's cleanup no longer loses your history. A
rebuild from scratch only finds the transcripts still on disk, so keep the index once it holds
sessions whose transcripts are gone.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

Requires Python 3.14+ (for `compression.zstd`); no other dependencies.
