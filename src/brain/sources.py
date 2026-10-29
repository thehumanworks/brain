"""Where each agent keeps its transcripts, and how one transcript line becomes a message.

Every agent writes one JSONL file per session and only ever appends to it, so a file is the
unit we follow. Each parser turns one decoded line into at most one `Message` plus any session
metadata the line carries (working directory, title, parent session).
"""

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# The most of one message's text we keep, as Atuin does for tool payloads: enough for nearly
# every message whole, small enough that a tool dumping a log can't make a huge row.
CLIP = 64 * 1024


@dataclass(frozen=True)
class SourceFile:
    agent: str
    path: Path
    session_id: str
    parent_id: str | None = None

    @property
    def key(self) -> str:
        # Codex compresses cold rollouts to .jsonl.zst and moves archived ones to another
        # directory; keying its checkpoint by thread id lets reading carry on across both.
        if self.agent == "codex":
            return f"codex:{self.session_id}"
        return str(self.path)


@dataclass
class Message:
    source_id: str
    ts: int | None
    role: str  # user | assistant | tool | system
    kind: str  # text | thinking | tool_call | tool_result | summary | context
    text: str


@dataclass
class Parsed:
    message: Message | None = None
    meta: dict = field(default_factory=dict)


def iso_ms(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def clip(text: str, limit: int = CLIP) -> str:
    if len(text) <= limit:
        return text
    head, tail = limit * 3 // 4, limit // 4
    return f"{text[:head]}\n… [{len(text) - head - tail} characters clipped] …\n{text[-tail:]}"


def content_text(content) -> str:
    """The readable text of a message's content: a string, or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = block.get("type")
            if kind in ("text", "input_text", "output_text"):
                parts.append(block.get("text") or "")
            elif kind == "image" or kind == "input_image":
                parts.append("[image]")
            elif kind == "tool_result":
                parts.append(content_text(block.get("content")))
    return "\n".join(p for p in parts if p)


def call_text(name, args) -> str:
    """A tool call as searchable text. Arguments are spread over lines rather than left as
    JSON, whose escaped newlines would glue words together for the tokenizer."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return f"{name}\n{args}"
    if isinstance(args, dict):
        lines = [str(name)]
        for k, v in args.items():
            lines.append(f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}")
        return "\n".join(lines)
    return f"{name}\n{json.dumps(args, ensure_ascii=False)}"


def message(source_id, ts, role, kind, text) -> Parsed:
    text = text.strip()
    if not text:
        return Parsed()
    return Parsed(Message(source_id, ts, role, kind, clip(text)))


# --- Claude Code ------------------------------------------------------------------------------

CLAUDE_COMMAND = re.compile(r"<command-(name|message|args)>(.*?)</command-\1>", re.S)


def slash_command(text: str) -> str:
    """A slash command as typed (`/plugin marketplace add …`) rather than the tags Claude Code
    records it as."""
    tags = dict(CLAUDE_COMMAND.findall(text))
    if "name" not in tags or CLAUDE_COMMAND.sub("", text).strip():
        return text
    return f"{tags['name']} {tags.get('args', '')}".strip()


def parse_claude(rec: dict, offset: int) -> Parsed:
    kind = rec.get("type")
    if kind == "custom-title":
        return Parsed(meta={"custom_title": rec.get("customTitle")})
    if kind == "ai-title":
        return Parsed(meta={"title": rec.get("aiTitle")})
    if kind not in ("user", "assistant"):
        return Parsed()

    meta = {"cwd": rec["cwd"]} if rec.get("cwd") else {}
    source_id = rec.get("uuid") or f"@{offset}"
    ts = iso_ms(rec.get("timestamp"))
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": slash_command(content)}]
    blocks = content or []

    role, what, parts = None, None, []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            r, w, t = ("assistant" if kind == "assistant" else "user"), "text", block.get("text")
        elif btype == "thinking":
            r, w, t = "assistant", "thinking", block.get("thinking")
        elif btype == "tool_use":
            r, w, t = "assistant", "tool_call", call_text(block.get("name"), block.get("input"))
        elif btype == "tool_result":
            r, w, t = "tool", "tool_result", content_text(block.get("content"))
        elif btype == "image":
            r, w, t = ("assistant" if kind == "assistant" else "user"), "text", "[image]"
        else:
            continue
        if not t:
            continue
        role, what = role or r, what or w
        parts.append(t)

    if rec.get("isCompactSummary"):
        role, what = "system", "summary"
    elif rec.get("isMeta"):
        role, what = "system", "context"

    parsed = message(source_id, ts, role or "user", what or "text", "\n".join(parts))
    parsed.meta = meta
    return parsed


# --- Codex ------------------------------------------------------------------------------------

# User-role messages Codex writes itself: instructions files, environment and skill context.
CODEX_CONTEXT = re.compile(r"^\s*(<[a-z_]+[ >]|# AGENTS\.md instructions)")


def parse_codex(rec: dict, offset: int) -> Parsed:
    kind = rec.get("type")
    p = rec.get("payload") or {}
    if kind == "session_meta":
        return Parsed(meta={"cwd": p.get("cwd"), "parent_id": p.get("parent_thread_id")})
    if kind == "turn_context":
        return Parsed(meta={"cwd": p.get("cwd")} if p.get("cwd") else {})
    if kind != "response_item":
        return Parsed()

    # Codex lines carry no id of their own; the byte offset is stable because the file is
    # append-only, and survives compression since we offset into the decompressed stream.
    source_id = p.get("id") or f"@{offset}"
    ts = iso_ms(rec.get("timestamp"))
    ptype = p.get("type")

    if ptype == "message":
        role = p.get("role")
        text = content_text(p.get("content"))
        if role == "assistant":
            return message(source_id, ts, "assistant", "text", text)
        if role == "user":
            if CODEX_CONTEXT.match(text):
                return message(source_id, ts, "system", "context", text)
            return message(source_id, ts, "user", "text", text)
        return Parsed()  # developer: the system prompt, repeated in every session
    if ptype == "reasoning":
        summary = "\n".join(s.get("text", "") for s in p.get("summary") or [] if isinstance(s, dict))
        return message(source_id, ts, "assistant", "thinking", summary)
    if ptype in ("function_call", "custom_tool_call"):
        return message(source_id, ts, "assistant", "tool_call",
                       call_text(p.get("name"), p.get("arguments", p.get("input"))))
    if ptype in ("function_call_output", "custom_tool_call_output"):
        out = p.get("output")
        return message(source_id, ts, "tool", "tool_result",
                       out if isinstance(out, str) else content_text(out))
    if ptype == "agent_message":
        return message(source_id, ts, "system", "context", content_text(p.get("content")))
    if ptype in ("local_shell_call", "web_search_call"):
        return message(source_id, ts, "assistant", "tool_call", call_text(ptype, p.get("action")))
    return Parsed()


# --- Pi ---------------------------------------------------------------------------------------


def parse_pi(rec: dict, offset: int) -> Parsed:
    kind = rec.get("type")
    if kind == "session":
        return Parsed(meta={"cwd": rec.get("cwd")})
    source_id = rec.get("id") or f"@{offset}"
    ts = iso_ms(rec.get("timestamp"))
    if kind == "compaction":
        return message(source_id, ts, "system", "summary", rec.get("summary") or "")
    if kind == "custom_message":
        return message(source_id, ts, "system", "context", content_text(rec.get("content")))
    if kind != "message":
        return Parsed()

    m = rec.get("message") or {}
    role = m.get("role")
    content = m.get("content")
    if role == "user":
        return message(source_id, ts, "user", "text", content_text(content))
    if role == "toolResult":
        return message(source_id, ts, "tool", "tool_result", content_text(content))
    if role == "system":
        return message(source_id, ts, "system", "context", content_text(content))
    if role != "assistant":
        return Parsed()

    what, parts = None, []
    for block in content if isinstance(content, list) else [{"type": "text", "text": content}]:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            w, t = "text", block.get("text")
        elif btype == "thinking":
            w, t = "thinking", block.get("thinking")
        elif btype == "toolCall":
            w, t = "tool_call", call_text(block.get("name"), block.get("arguments"))
        else:
            continue
        if t:
            what = what or w
            parts.append(t)
    return message(source_id, ts, "assistant", what or "text", "\n".join(parts))


PARSERS = {"claude": parse_claude, "codex": parse_codex, "pi": parse_pi}
AGENTS = tuple(PARSERS)

# Cheap byte tests that let us skip decoding lines no parser wants. A false positive costs a
# json.loads; a false negative would lose data, so these only ever match too much.
WANTED = {
    "claude": (b'"user"', b'"assistant"', b'-title"'),
    "codex": (b'"response_item"', b'"session_meta"', b'"turn_context"'),
    "pi": None,
}


# --- Discovery --------------------------------------------------------------------------------


def claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def pi_root() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")


def discover() -> Iterator[SourceFile]:
    root = claude_root()
    for path in root.glob("*/*.jsonl"):
        yield SourceFile("claude", path, path.stem)
    # Subagents and workflow agents; workflows/*/journal.jsonl beside them is a run log. A
    # continued session copies its parent's subagents, which then merge as one session.
    for path in root.glob("*/*/subagents/**/agent-*.jsonl"):
        parent = path.relative_to(root).parts[1]
        yield SourceFile("claude", path, path.stem, parent)

    root = codex_root()
    seen = set()
    rollouts = sorted(
        [*root.glob("sessions/*/*/*/rollout-*.jsonl*"), *root.glob("archived_sessions/rollout-*.jsonl*")],
        key=lambda p: p.suffix == ".zst",  # a live .jsonl wins over a compressed twin
    )
    for path in rollouts:
        m = re.match(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+?)\.jsonl(\.zst)?$", path.name)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            yield SourceFile("codex", path, m.group(1))

    root = pi_root()
    for path in [*root.glob("sessions/*/*.jsonl"), *root.glob("bots/*/sessions/*.jsonl")]:
        yield SourceFile("pi", path, path.stem.rsplit("_", 1)[-1])
