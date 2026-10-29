"""Briefs: what the latest sessions in a project were asked, how they ended and what they
changed, short enough to start a new session with."""

import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path

from brain.models import Brief, Commit, Session
from brain.query import SESSION_COLUMNS

# Where each agent tells the commands it runs which session they run in.
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "PI_SESSION_ID")

# Scratch directories: a session there is never a project that has since moved.
TEMP = ("/tmp/", "/private/", "/var/folders/")

# User messages the agent wrote rather than the user: command output, notifications, context,
# interruptions, a summary carried over from an earlier session, and slash commands like /model.
NOT_PROMPT = re.compile(r"\s*(<[a-z][\w-]*[ >]|\[Request interrupted|This session is being continued "
                        r"from a previous conversation|/[a-z][\w:-]*(\s|$))")
CODEX_REQUEST = "## My request for Codex:"  # Codex Desktop puts attachments before the prompt

# Pi's claude-bridge provider runs Pi sessions through Claude Code, which keeps transcripts of
# its own: copies of the Pi session, which call Pi's tools through an MCP server.
BRIDGE_TOOL = r"mcp\_\_custom-tools\_\_%"

# Writing a file: Claude Code's and Pi's tools, with the file among their first arguments, and
# Codex's patches (inside its `exec` scripts, their newlines are still escaped).
EDIT = re.compile(r"(?:Edit|MultiEdit|Write|NotebookEdit|edit|write)\n(?:[^\n]*\n){0,3}?"
                  r"(?:file_path|notebook_path|path): ([^\n]+)")
PATCH = re.compile(r"\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+?)(?=\\n|\"|$)", re.M)

# `git commit` output: "[main 1a2b3c4] Subject" over " 3 files changed, …".
COMMIT = re.compile(r"\[([^\]\n]+?)(?: \(root-commit\))? ([0-9a-f]{7,40})\] ([^\n]+?)"
                    r"(?:\n|\\n)\s*\d+ files? changed")
# A quiet `git commit -q` prints nothing, so its subject comes from the call: the first -m, or
# the first line of a message read from a heredoc (-F -). Only shell calls count; a file being
# written can hold `git commit` too.
SHELL_TOOLS = ("Bash", "bash", "exec", "exec_command", "shell", "local_shell_call")
COMMIT_CALL = re.compile(r"git (?:-C \S+ )?commit\b(?:[^\n]*?-m ?(['\"])(.+?)(?<!\\)\1|[^\n]*?"
                         r"<<-? ?['\"]?(\w+)['\"]?(?:\n|\\n)(.+?)(?=\n|\\n))")


def checkout(path: str | Path) -> tuple[Path, Path]:
    """The git checkout `path` is in and its main checkout (the same, unless it is a worktree);
    outside git, `path` itself twice. The search stops below the home directory, which is no
    project even when it holds a repository of dotfiles."""
    path = Path(path).expanduser().resolve()
    home = Path.home()
    for d in (path, *path.parents):
        if d != path and (d == home or d in home.parents):
            break
        git = d / ".git"
        if git.is_dir():
            return d, d
        if git.is_file():
            m = re.match(r"gitdir: (.+)", git.read_text(errors="replace").strip())
            gitdir = (d / m.group(1)).resolve() if m else d
            # A worktree's points into the main checkout's .git/worktrees; a submodule's doesn't.
            return d, gitdir.parents[2] if gitdir.parent.name == "worktrees" else d
    return path, path


def project_cwds(db: sqlite3.Connection, here: Path, root: Path) -> list[str]:
    """The working directories of the sessions in a project, from its checkout `here` and main
    checkout `root`: the checkouts and what is below them, such as worktrees, and directories of
    its name that no longer exist, which is where a project that moved used to be or a removed
    worktree was. The home directory and above match only themselves."""
    tops = {str(here), str(root)}
    home = Path.home()
    exact = root == home or root in home.parents
    found = []
    for (cwd,) in db.execute("SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL"):
        if cwd in tops:
            found.append(cwd)
        elif exact:
            continue
        elif any(cwd.startswith(top + "/") for top in tops):
            found.append(cwd)
        elif (root.name in Path(cwd).parts and not cwd.startswith(TEMP)
              and not os.path.isdir(cwd)):
            found.append(cwd)
    return found


def current_sessions() -> list[str]:
    return [v for name in SESSION_ENV if (v := os.environ.get(name))]


def recent(db: sqlite3.Connection, path: str | Path, limit: int,
           exclude: list[str]) -> list[Brief]:
    """Briefs of the `limit` latest sessions in `path`'s project, other than `exclude`, Claude
    Code's copies of Pi sessions and sessions without a prompt."""
    here, root = checkout(path)
    cwds = project_cwds(db, here, root)
    rows = db.execute(
        f"""SELECT {SESSION_COLUMNS} FROM sessions s
            WHERE parent_id IS NULL AND cwd IN (SELECT value FROM json_each(?))
            ORDER BY updated_at DESC""",
        (json.dumps(cwds),),
    )
    briefs, titles = [], set()
    for session in (Session(**dict(r)) for r in rows.fetchall()):
        if len(briefs) >= limit:
            break
        # A title seen already is a rerun, such as a scheduled task's: only the latest counts.
        if (session.session_id in exclude or (session.title and session.title in titles)
                or bridged(db, session)):
            continue
        found = brief(db, session, root)
        if found.prompts and (found.outcome or found.files or found.commits):
            briefs.append(found)
            titles.add(session.title)
    return briefs


def bridged(db: sqlite3.Connection, s: Session) -> bool:
    """Whether `s` is Claude Code's copy of a Pi session: it called Pi's tools."""
    return s.agent == "claude" and db.execute(
        "SELECT 1 FROM messages WHERE agent = 'claude' AND session_id = ? AND kind = 'tool_call' "
        "AND text LIKE ? ESCAPE '\\' LIMIT 1",
        (s.session_id, BRIDGE_TOOL),
    ).fetchone() is not None


def brief(db: sqlite3.Connection, s: Session, repo: Path | None = None) -> Brief:
    """`s` in brief; with `repo`, its commits are those the repository there has."""
    key = (s.agent, s.session_id)
    rows = db.execute(
        "SELECT id, role, kind, CASE WHEN role = 'user' THEN substr(text, 1, 200) END AS head "
        "FROM messages WHERE agent = ? AND session_id = ? ORDER BY ts, id", key,
    ).fetchall()
    prompts = [r["id"] for r in rows if is_prompt(r)]

    # The outcome is the last reply that ended its turn: walking back from the end, a reply
    # counts until a tool call or result says the agent carried on after it.
    answer, after = None, len(rows)
    clean = True
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        if r["kind"] in ("tool_call", "tool_result"):
            clean = False
        elif r["role"] == "user" and r["kind"] == "text":
            clean = True
        elif clean and r["role"] == "assistant" and r["kind"] == "text":
            answer, after = r["id"], i + 1
            break
    finished = answer is not None and not any(
        r["kind"] in ("tool_call", "tool_result") or is_prompt(r) for r in rows[after:])

    text = lambda id_: db.execute("SELECT text FROM messages WHERE id = ?", (id_,)).fetchone()[0]
    last_prompt = text(prompts[-1]) if prompts else None
    if last_prompt and CODEX_REQUEST in last_prompt:
        last_prompt = last_prompt.split(CODEX_REQUEST, 1)[1].strip()
    found = commits(db, s)
    if repo:
        found = in_repo(found, repo)
    return Brief(s, len(prompts), last_prompt, text(answer) if answer else None, finished,
                 changed_files(db, s), found)


def is_prompt(row) -> bool:
    return row["role"] == "user" and row["kind"] == "text" and not NOT_PROMPT.match(row["head"])


def changed_files(db: sqlite3.Connection, s: Session) -> list[str]:
    counts = Counter()
    for (call,) in db.execute(
            "SELECT text FROM messages WHERE agent = ? AND session_id = ? AND kind = 'tool_call' "
            "ORDER BY ts, id", (s.agent, s.session_id)):
        if m := EDIT.match(call):
            paths = [m.group(1)]
        else:
            paths = PATCH.findall(call)
        for p in paths:
            p = p.strip()
            if p:
                counts[os.path.normpath(os.path.join(s.cwd or "/", os.path.expanduser(p)))] += 1
    return [p for p, _ in counts.most_common()]  # ties keep the order they were first seen


def commits(db: sqlite3.Connection, s: Session) -> list[Commit]:
    """Commits as `git commit` printed them, and quiet ones by the subject they were given,
    with the hash `git log --oneline` later showed for it, if it did."""
    key = (s.agent, s.session_id)
    found = {}
    for (output,) in db.execute(
            "SELECT text FROM messages WHERE agent = ? AND session_id = ? AND kind = 'tool_result' "
            "AND text LIKE '%changed%' ORDER BY ts, id", key):
        for branch, sha, subject in COMMIT.findall(output):
            found[unescape(subject)] = Commit(sha, branch, unescape(subject))  # amended: the last
    for (call,) in db.execute(
            "SELECT text FROM messages WHERE agent = ? AND session_id = ? AND kind = 'tool_call' "
            "AND text LIKE '%git %commit%' ORDER BY ts, id", key):
        if call.split("\n", 1)[0] not in SHELL_TOOLS:
            continue
        for m in COMMIT_CALL.finditer(call):
            subject = unescape(m.group(2) or m.group(4)).split("\n")[0]
            if subject and subject not in found and subject != m.group(3):
                found[subject] = Commit(logged_hash(db, s, subject), "", subject)
    return list(found.values())


def in_repo(commits: list[Commit], repo: Path) -> list[Commit]:
    """The commits the git repository at `repo` has, and those without a hash; all of them if
    git can't tell. The output of commands in other repositories, or of searching old
    transcripts, holds commit lines too."""
    hashes = [c.hash for c in commits if c.hash]
    if not hashes or not (repo / ".git").exists():
        return commits
    try:
        out = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check"],
                             input="\n".join(hashes) + "\n", capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return commits
    if out.returncode:
        return commits
    known = {h for h, line in zip(hashes, out.stdout.splitlines()) if line.split()[1:2] == ["commit"]}
    return [c for c in commits if not c.hash or c.hash in known]


def logged_hash(db: sqlite3.Connection, s: Session, subject: str) -> str:
    for (output,) in db.execute(
            "SELECT text FROM messages WHERE agent = ? AND session_id = ? AND kind = 'tool_result' "
            "AND instr(text, ?) ORDER BY ts, id", (s.agent, s.session_id, subject)):
        if m := re.search(r"\b([0-9a-f]{7,12}) (?:\([^)]*\) )?" + re.escape(subject), output):
            return m.group(1)
    return ""


def unescape(text: str) -> str:
    return text.replace('\\"', '"').replace("\\n", "\n").strip()
