"""The `brain` command."""

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from brain import db as dbmod
from brain import query, service
from brain.indexer import index_all

HOME = str(Path.home())
TOOL_KINDS = ("tool_call", "tool_result")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="brain",
        description="Search your Claude Code, Codex and Pi conversation history.",
    )
    parser.add_argument("--db", type=Path, help="index file (default: $BRAIN_DB or "
                        "~/.local/share/brain/index.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="add new transcript lines to the index, once")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("watch", help="keep the index up to date, in the foreground")
    p.add_argument("--interval", type=float, default=5, help="seconds between scans (default 5)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("search", help="full-text search over every message")
    p.add_argument("query", nargs="+", help='words that must all match; "a phrase"; prefix*')
    add_filters(p, role=True)
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--per-session", type=int, default=2, help="most hits shown per session")
    p.add_argument("--any", action="store_true", help="match any word rather than all")
    p.add_argument("--raw", action="store_true", help="pass the query to SQLite FTS5 as is")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("list", help="the most recently updated sessions")
    add_filters(p)
    p.add_argument("-n", "--limit", type=int, default=20, help="how many (default 20)")
    p.add_argument("--all", action="store_true", help="include subagents")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="print a session's messages")
    p.add_argument("session", help="session id, or its start or last 8 characters")
    p.add_argument("--at", type=int, help="centre on this message id (from search)")
    p.add_argument("-C", "--context", type=int, default=10, help="messages either side of --at")
    p.add_argument("--full", action="store_true", help="don't shorten long messages")
    p.add_argument("--no-tools", action="store_true", help="hide tool calls and results")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("stats", help="what the index holds")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("service", help="run `brain watch` in the background, from login on "
                       "(launchd on macOS, systemd on Linux and WSL)")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.set_defaults(func=cmd_service)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.stderr.close()


def add_filters(p: argparse.ArgumentParser, role: bool = False) -> None:
    p.add_argument("--agent", choices=["claude", "codex", "pi"])
    p.add_argument("--project", help="substring of the session's working directory")
    p.add_argument("--since", help="7d, 12h, 2w or a date like 2026-10-01")
    if role:
        p.add_argument("--role", choices=["user", "assistant", "tool", "system"])


def filters(args) -> query.Filters:
    return query.Filters(
        agent=args.agent,
        project=args.project,
        since=query.parse_since(args.since) if args.since else None,
        role=getattr(args, "role", None),
    )


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def when(ms: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ms / 1000).strftime(fmt) if ms else "?"


def ago(ms: int | None) -> str:
    """'5 minutes ago', '3 days ago', or the date once it's over a month."""
    if not ms:
        return "?"
    secs = time.time() - ms / 1000
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = int(secs // size)
            if unit == "day" and n > 30:
                return when(ms, "%Y-%m-%d")
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def oneline(text: str | None, width: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def tilde(path: str | None) -> str:
    if not path:
        return "?"
    return "~" + path[len(HOME):] if path.startswith(HOME) else path


# --- Commands ---------------------------------------------------------------------------------


def cmd_index(args) -> None:
    db = dbmod.connect(args.db)
    started = time.monotonic()

    def progress(done, total, result):
        if time.monotonic() - started > 2:
            log(f"  {done}/{total} transcripts, {result.messages} new messages")

    result = index_all(db, progress)
    for err in result.errors:
        log(f"error: {err}")
    log(f"indexed {result.messages} new messages from {result.files} transcripts "
        f"in {time.monotonic() - started:.1f}s"
        + (f" ({result.rereads} re-read after being rewritten)" if result.rereads else ""))


def cmd_watch(args) -> None:
    db = dbmod.connect(args.db)
    log(f"watching Claude Code, Codex and Pi transcripts every {args.interval:g}s "
        "(Ctrl-C to stop)")
    while True:
        started = time.monotonic()
        result = index_all(db)
        took = time.monotonic() - started
        for err in result.errors:
            log(f"error: {err}")
        if result.messages:
            log(f"{datetime.now():%H:%M:%S} +{result.messages} messages from "
                f"{result.files} transcripts ({took:.1f}s)")
        time.sleep(max(0.0, args.interval - took))


def cmd_search(args) -> None:
    db = dbmod.connect(args.db)
    hits = query.search(db, " ".join(args.query), filters(args), limit=args.limit,
                        per_session=args.per_session, raw=args.raw, any_term=args.any)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return
    if not hits:
        log("no matches" + ("" if args.any else " (try --any to match any word)"))
        return
    for hit in hits:
        title = f'  "{oneline(hit["title"])}"' if hit["title"] else ""
        print(f"{hit['agent']} {when(hit['ts'])}  {tilde(hit['cwd'])}{title}")
        print(f"  session {query.short_id(hit['session_id'])}  msg {hit['id']}  "
              f"{hit['role']}/{hit['kind']}")
        print("  " + " ".join(hit["snippet"].split()))
        print()
    log("open one with: brain show <session> --at <msg>")


def cmd_list(args) -> None:
    db = dbmod.connect(args.db)
    rows = query.sessions(db, filters(args), args.limit, subagents=args.all)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    head = f"{'ID':8}  {'AGENT':6}  {'UPDATED':15}  {'MSGS':>5}  {'PROJECT':16}  "
    room = max(20, shutil.get_terminal_size((120, 24)).columns - len(head))
    print(head + "TITLE")
    for s in rows:
        project = oneline(Path(s["cwd"]).name if s["cwd"] else "?", 16)
        title = ("↳ " if s["parent_id"] else "") + (s["title"] or "")
        print(f"{query.short_id(s['session_id']):8}  {s['agent']:6}  {ago(s['updated_at']):15}  "
              f"{s['messages']:5}  {project:16}  {oneline(title, room)}")


def cmd_show(args) -> None:
    db = dbmod.connect(args.db)
    found = query.find_session(db, args.session)
    if not found:
        sys.exit(f"no session matches {args.session!r}")
    if len(found) > 1 and found[0]["session_id"] != args.session:
        log(f"{args.session!r} matches more than one session:")
        for s in found:
            log(f"  {s['agent']} {s['session_id']}  {tilde(s['cwd'])}  {oneline(s['title'])}")
        sys.exit(1)
    s = found[0]
    rows = query.messages(db, s["agent"], s["session_id"], args.at, args.context,
                          tools=not args.no_tools)
    if args.json:
        print(json.dumps({"session": s, "messages": rows}, ensure_ascii=False, indent=1))
        return

    print(f"{s['agent']} session {s['session_id']}" + (f'  "{oneline(s["title"])}"' if s["title"] else ""))
    print(f"{tilde(s['cwd'])} · {when(s['started_at'])} → {when(s['updated_at'])} · "
          f"{s['messages']} messages")
    print(f"transcript {tilde(s['path'])}")
    if s["parent_id"]:
        print(f"subagent of {s['parent_id']}")
    for r in rows:
        text = r["text"]
        limit = None if args.full else (600 if r["kind"] in TOOL_KINDS else 4000)
        if limit and len(text) > limit:
            text = text[:limit] + f"\n… [{len(r['text']) - limit} more characters, --full shows all]"
        marker = " ◀" if r["id"] == args.at else ""
        print(f"\n── msg {r['id']} · {r['role']}/{r['kind']} · {when(r['ts'], '%Y-%m-%d %H:%M:%S')}{marker}")
        print(text)


def cmd_stats(args) -> None:
    db = dbmod.connect(args.db)
    path = args.db or dbmod.default_path()
    for row in db.execute(
        "SELECT agent, count(*) AS sessions, sum(messages) AS messages, "
        "min(started_at) AS first, max(updated_at) AS last FROM sessions GROUP BY agent"
    ):
        print(f"{row['agent']:6} {row['sessions']:6} sessions {row['messages'] or 0:9} messages  "
              f"{when(row['first'], '%Y-%m-%d')} → {when(row['last'])}")
    size = sum(p.stat().st_size for p in path.parent.glob(path.name + "*"))
    print(f"index  {tilde(str(path))}  {size / 1e6:.0f} MB")


def cmd_service(args) -> None:
    if args.action == "install":
        service.install(args.db)
    elif args.action == "uninstall":
        service.uninstall()
    else:
        service.status()
