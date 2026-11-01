"""Route path prefixes to different backends without teaching tools about either.

Two environments in one run is the normal case, not the exotic one: a repository checkout
whose ``docs/`` tree lives in an object store, a scratch directory that must stay on fast
local disk while the build runs confined, a read-only vendor mount. Deep Agents solves it
with a composite backend keyed by path prefix; the same idea is enough here, and it keeps
the tool layer free of routing rules.

Prefix matching is on whole path segments. ``docsx/a.md`` must not match the ``docs``
route: a prefix that matches inside a segment is how a routing bug turns into reading the
wrong file, and it is invisible in a short test.
"""

from __future__ import annotations

from pathlib import Path

from forgeharness.backends.protocol import (
    BackendFactory,
    FileBackend,
    FileEntry,
    ReadResult,
    WriteResult,
)
from forgeharness.tools.process import CommandResult


def _normalise(prefix: str) -> str:
    trimmed = prefix.strip("/")
    return "" if trimmed in ("", ".") else trimmed


class CompositeBackend:
    """Dispatch each operation to the backend whose prefix best matches the path."""

    name = "composite"

    def __init__(
        self, *, routes: tuple[tuple[str, FileBackend], ...], default: FileBackend
    ) -> None:
        if not routes:
            raise ValueError("a composite backend needs at least one route")
        # Longest prefix first so ``src/vendor`` wins over ``src`` regardless of the
        # order the caller happened to list them in.
        normalised = [(_normalise(prefix), backend) for prefix, backend in routes]
        self._routes = tuple(sorted(normalised, key=lambda route: -len(route[0])))
        self._default = default

    @property
    def routes(self) -> tuple[tuple[str, str], ...]:
        """``(prefix, backend name)`` in match order, for a trace or a health check."""
        return tuple((prefix, backend.name) for prefix, backend in self._routes)

    def backend_for(self, path: str) -> FileBackend:
        """Return the backend that owns ``path``; the default when nothing matches."""
        candidate = _normalise(path)
        for prefix, backend in self._routes:
            if prefix == "" or candidate == prefix or candidate.startswith(f"{prefix}/"):
                return backend
        return self._default

    def list_files(
        self, *, path: str = ".", limit: int = 200, skip_directories: frozenset[str] = frozenset()
    ) -> tuple[FileEntry, ...]:
        return self.backend_for(path).list_files(
            path=path, limit=limit, skip_directories=skip_directories
        )

    def read_text(self, path: str, *, max_bytes: int = 1_000_000) -> ReadResult:
        return self.backend_for(path).read_text(path, max_bytes=max_bytes)

    def write_text(
        self, path: str, text: str, *, expected_digest: str | None = None
    ) -> WriteResult:
        return self.backend_for(path).write_text(path, text, expected_digest=expected_digest)

    def digest_of(self, path: str) -> str:
        return self.backend_for(path).digest_of(path)

    async def search(
        self, query: str, *, path: str = ".", max_output_bytes: int = 30_000
    ) -> CommandResult:
        return await self.backend_for(path).search(
            query, path=path, max_output_bytes=max_output_bytes
        )

    async def run(
        self, command: tuple[str, ...], *, max_output_bytes: int = 50_000
    ) -> CommandResult:
        """Commands are not path-scoped, so they go to the default backend.

        Routing a command by its argv would be guesswork: ``pytest tests/`` touches the
        whole workspace. The default backend is therefore the one that executes, and a
        caller that wants a command confined constructs the composite with a container
        as its default.
        """
        return await self._default.run(command, max_output_bytes=max_output_bytes)


def composite_factory(
    *, routes: tuple[tuple[str, FileBackend], ...], default: FileBackend
) -> BackendFactory:
    """Bind fixed routes and return the factory shape the tools expect."""

    def factory(root: Path) -> CompositeBackend:
        return CompositeBackend(routes=routes, default=default)

    return factory


__all__ = ["CompositeBackend", "composite_factory"]
