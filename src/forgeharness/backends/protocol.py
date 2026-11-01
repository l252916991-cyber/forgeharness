"""The file-and-command boundary every tool goes through instead of touching ``Path``.

The harness already keeps its storage behind protocols -- checkpoints, the invocation
journal, vector stores -- but its *file tools* called ``Path`` directly. That worked
until the question stopped being "which directory" and became "which machine": a path
policy can refuse ``../../etc``, and it cannot stop a test command from running on the
host and deleting the operator's home directory. ``tools/policy.py`` says as much in
its own comment ("Defence in depth, not a sandbox.").

So the boundary is a protocol, not an adapter class. A backend answers five questions --
list, read, write, search, run -- and everything above it stops caring whether the
answer comes from the local disk, a bind-mounted container, or a remote store. That is
what makes a second execution environment a constructor argument instead of a fork of
the tool layer.

Two invariants every implementation must keep, because callers rely on them:

1. **A path argument is workspace-relative and can never escape.** Backends raise
   :class:`~forgeharness.tools.paths.WorkspacePathError`, which the dispatcher already
   maps to ``INVALID_ARGUMENTS`` -- the model gets a correctable error, not a crash.
2. **Writes are atomic and never follow a symlink.** A partially written file after a
   crash is worse than no write, and a symlink is a path that resolves somewhere the
   policy already approved.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.tools.process import CommandResult


class BackendError(RuntimeError):
    """A backend could not satisfy a request for reasons outside the model's control."""


class FileTooLarge(ValueError):
    """Raised when a file exceeds the caller's explicit read bound."""


class FileConflict(ValueError):
    """Raised when an optimistic write's expected digest no longer matches."""


class NotRegularFile(ValueError):
    """Raised when a path exists but is not a regular file."""


class FileEntry(FrozenModel):
    """One workspace-relative file in a listing."""

    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    is_symlink: bool = False


class ReadResult(FrozenModel):
    """A file's decoded text plus the digest of its bytes."""

    path: str = Field(min_length=1)
    text: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)


class WriteResult(FrozenModel):
    """The outcome of one write: what changed, and the digest a reader can pin."""

    path: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    created: bool


class FileBackend(Protocol):
    """The five operations a workspace needs, and nothing else."""

    #: Short, stable identifier recorded in tool metadata so a trace says which
    #: environment actually ran the work.
    name: str

    def list_files(
        self, *, path: str = ".", limit: int = 200, skip_directories: frozenset[str] = frozenset()
    ) -> tuple[FileEntry, ...]:
        """Return sorted real files, with at most one extra entry to signal truncation."""
        ...

    def read_text(self, path: str, *, max_bytes: int = 1_000_000) -> ReadResult:
        """Return UTF-8 text plus its digest, refusing files above ``max_bytes``."""
        ...

    def write_text(
        self, path: str, text: str, *, expected_digest: str | None = None
    ) -> WriteResult:
        """Atomically create or replace a file, enforcing the optimistic-lock contract."""
        ...

    def digest_of(self, path: str) -> str:
        """Return the sha256 of a file's bytes without decoding them."""
        ...

    async def search(
        self, query: str, *, path: str = ".", max_output_bytes: int = 30_000
    ) -> CommandResult:
        """Search literal text below ``path``, returning bounded matching-line output."""
        ...

    async def run(
        self, command: tuple[str, ...], *, max_output_bytes: int = 50_000
    ) -> CommandResult:
        """Run an argv vector in the workspace and return bounded output."""
        ...


#: How a tool obtains a backend for the workspace of the current call. Tools take a
#: factory rather than an instance because the workspace is only known per call.
BackendFactory = Callable[[Path], FileBackend]


__all__ = [
    "BackendError",
    "BackendFactory",
    "FileBackend",
    "FileConflict",
    "FileEntry",
    "FileTooLarge",
    "NotRegularFile",
    "ReadResult",
    "WriteResult",
]
