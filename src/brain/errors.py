"""What the SDK raises on purpose. SQLite and file errors (a locked or missing index) pass through
as `sqlite3.Error` and `OSError`."""


class BrainError(Exception):
    """Base class for brain's own errors."""


class IndexFormatError(BrainError):
    """The index was written by a version of brain with a different format."""


class SessionNotFound(BrainError):
    def __init__(self, ref: str):
        super().__init__(f"no session matches {ref!r}")
        self.ref = ref


class AmbiguousSession(BrainError):
    def __init__(self, ref: str, matches: list):
        super().__init__(f"{ref!r} matches more than one session")
        self.ref = ref
        self.matches = matches  # Sessions, most recently updated first
