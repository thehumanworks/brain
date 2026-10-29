"""What the SDK returns: plain dataclasses. `dataclasses.asdict` turns one into the JSON the
`brain` command prints, so fields follow the index's columns and keep their order. Times are
unix milliseconds."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Session:
    agent: str  # claude | codex | pi
    session_id: str
    parent_id: str | None  # the session that spawned this one (subagents)
    cwd: str | None
    title: str | None  # set by the user or the agent, or else the first prompt
    started_at: int | None
    updated_at: int | None
    messages: int  # how many it holds
    path: str  # the transcript it was read from


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    ts: int | None
    role: str  # user | assistant | tool | system
    kind: str  # text | thinking | tool_call | tool_result | summary | context
    text: str


@dataclass(frozen=True, slots=True)
class Hit:
    """A message that matched a search, with its session's details."""
    id: int  # the message: read around it with `Brain.messages(hit, at=hit.id)`
    agent: str
    session_id: str
    ts: int | None
    role: str
    kind: str
    score: float  # BM25, weighted by kind; lower is better
    nth: int  # 1 for the session's best hit, 2 for its next
    cwd: str | None
    title: str | None
    parent_id: str | None
    snippet: str  # the matching words in [brackets], with some context


@dataclass(frozen=True, slots=True)
class Commit:
    hash: str  # abbreviated, as `git commit` printed it
    branch: str
    subject: str


@dataclass(frozen=True, slots=True)
class Brief:
    """A session in a few lines: what was asked, how it ended and what it changed."""
    session: Session
    prompts: int  # how many the user typed
    last_prompt: str | None
    outcome: str | None  # the last reply that ended a turn rather than calling tools
    finished: bool  # false if work carried on after `outcome`: interrupted or still going
    files: list[str]  # edited or written, most often first; absolute paths
    commits: list[Commit]  # made with `git commit`, in order


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: list[Message]
    offset: int  # position of the first in the (filtered) session
    total: int  # messages in the (filtered) session


@dataclass
class IndexResult:
    files: int = 0  # transcripts with something new
    messages: int = 0  # messages added
    rereads: int = 0  # transcripts rewritten underneath us and read again
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class AgentStats:
    agent: str
    sessions: int
    messages: int
    first: int | None  # when the oldest session started
    last: int | None  # when the newest was updated


@dataclass(frozen=True, slots=True)
class Stats:
    path: Path
    size: int  # bytes, including SQLite's write-ahead log
    agents: list[AgentStats]
