"""Local full-text search over Claude Code, Codex and Pi conversation history.

This package is the SDK; the `brain` command (`brain.cli`) is built on it. Importing `brain`
loads neither the command line nor its terminal UI.

    from brain import Brain

    with Brain() as brain:  # the index the `brain` command uses
        brain.index()  # add what the agents wrote since the last run
        for hit in brain.search("rate limiting", agent="codex", since="7d"):
            print(hit.title, hit.snippet)
            for message in brain.messages(hit, at=hit.id, context=2, tools=False):
                print(message.role, message.text)
"""

from brain.api import Brain, SessionRef, Since
from brain.db import default_path
from brain.errors import AmbiguousSession, BrainError, IndexFormatError, SessionNotFound
from brain.models import AgentStats, Hit, IndexResult, Message, MessagePage, Session, Stats
from brain.query import parse_since, short_id
from brain.sources import AGENTS

__all__ = [
    "AGENTS",
    "AgentStats",
    "AmbiguousSession",
    "Brain",
    "BrainError",
    "Hit",
    "IndexFormatError",
    "IndexResult",
    "Message",
    "MessagePage",
    "Session",
    "SessionNotFound",
    "SessionRef",
    "Since",
    "Stats",
    "default_path",
    "parse_since",
    "short_id",
]
