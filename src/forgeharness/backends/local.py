"""Workspace file operations through held no-follow POSIX directory descriptors.

Symlinks are refused, including aliases inside the workspace. Commands run on the
host; use ContainerBackend to isolate repository code from the host environment.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import os
import stat
import threading
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from forgeharness.backends.protocol import (
    FileConflict,
    FileEntry,
    FileTooLarge,
    NotRegularFile,
    ReadResult,
    WriteResult,
)
from forgeharness.tools.paths import WorkspacePathError
from forgeharness.tools.process import CommandResult, run_command

DEFAULT_SKIP_DIRECTORIES = frozenset({".git", ".forgeharness"})
MAX_SEARCH_FILE_BYTES = 1_000_000


class LocalBackend:
    """Serve real workspace files and execute configured commands on the host."""

    name = "local"

    def __init__(self, root: Path) -> None:
        self._root = Path(os.path.abspath(root))

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def _parts(path: str) -> tuple[str, ...]:
        if "\x00" in path:
            raise WorkspacePathError("NUL is not allowed in workspace paths")
        requested = Path(path)
        if requested.is_absolute():
            raise WorkspacePathError("absolute paths are not allowed")
        result: list[str] = []
        for part in requested.parts:
            if part == "..":
                if not result:
                    raise WorkspacePathError("path escapes the task workspace")
                result.pop()
            elif part != ".":
                result.append(part)
        return tuple(result)

    @contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
        descriptor = os.open(self._root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            root_parts = self._root.parts[1:]
            for index, part in enumerate((*root_parts, *parts)):
                if create and index >= len(root_parts):
                    try:
                        os.mkdir(part, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise WorkspacePathError(
                            "workspace path must contain only real directories"
                        ) from None
                    raise
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _parent(self, path: str, *, create: bool = False) -> Iterator[tuple[int, str, str]]:
        parts = self._parts(path)
        if not parts:
            raise NotRegularFile("workspace root is not a regular file")
        with self._directory(parts[:-1], create=create) as parent:
            yield parent, parts[-1], "/".join(parts)

    @staticmethod
    def _file(parent: int, name: str) -> int:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise NotRegularFile("refusing to read or hash a symlink") from None
            raise
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise NotRegularFile("target is not a regular file")
        if metadata.st_nlink != 1:
            os.close(descriptor)
            raise NotRegularFile("refusing a multiply linked file")
        return descriptor

    def _walk(
        self,
        directory: int,
        prefix: tuple[str, ...],
        skip: frozenset[str],
        *,
        hidden: bool = True,
        cancelled: threading.Event | None = None,
    ) -> Generator[tuple[int, str, FileEntry], None, None]:
        if cancelled is not None and cancelled.is_set():
            return
        with os.scandir(directory) as scan:
            names = []
            for scanned in scan:
                if cancelled is not None and cancelled.is_set():
                    return
                names.append(scanned.name)
        names.sort()
        for name in names:
            if cancelled is not None and cancelled.is_set():
                return
            if name in skip or (not hidden and name.startswith(".")):
                continue
            try:
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(entry.st_mode):
                try:
                    child = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                except OSError:
                    continue
                try:
                    yield from self._walk(
                        child, (*prefix, name), skip, hidden=hidden, cancelled=cancelled
                    )
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry.st_mode):
                yield directory, name, FileEntry(path="/".join((*prefix, name)), size=entry.st_size)

    def iter_files(
        self, *, path: str = ".", skip_directories: frozenset[str] = frozenset()
    ) -> Generator[FileEntry, None, None]:
        """Yield workspace-relative entries without exposing descriptor internals.

        Traversal holds the no-follow directory chain for the generator lifetime.
        Callers that stop early must close the generator, for example with
        ``contextlib.closing``. Symlinks are skipped and content reads still
        revalidate the file through ``read_text`` or ``digest_of``.
        """
        parts = self._parts(path)
        try:
            with self._directory(parts) as directory:
                iterator = self._walk(
                    directory, parts, skip_directories or DEFAULT_SKIP_DIRECTORIES
                )
                with closing(iterator) as walk:
                    for _, _, entry in walk:
                        yield entry
        except WorkspacePathError:
            if parts:
                with self._directory(parts[:-1]) as parent:
                    mode = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
                    if stat.S_ISREG(mode):
                        raise NotRegularFile("not a directory") from None
            raise

    def list_files(
        self, *, path: str = ".", limit: int = 200, skip_directories: frozenset[str] = frozenset()
    ) -> tuple[FileEntry, ...]:
        if limit < 1:
            raise ValueError("file limit must be positive")
        entries: list[FileEntry] = []
        with closing(self.iter_files(path=path, skip_directories=skip_directories)) as files:
            for entry in files:
                entries.append(entry)
                if len(entries) > limit:
                    break
        return tuple(entries)

    def read_text(self, path: str, *, max_bytes: int = 1_000_000) -> ReadResult:
        if max_bytes < 0:
            raise ValueError("read limit must be nonnegative")
        with self._parent(path) as (parent, name, relative):
            with os.fdopen(self._file(parent, name), "rb") as handle:
                raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise FileTooLarge(f"file exceeds {max_bytes} byte read limit")
        return ReadResult(
            path=relative,
            text=raw.decode("utf-8"),
            digest=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
        )

    def digest_of(self, path: str) -> str:
        with self._parent(path) as (parent, name, _):
            with os.fdopen(self._file(parent, name), "rb") as handle:
                return hashlib.file_digest(handle, "sha256").hexdigest()

    def write_text(
        self, path: str, text: str, *, expected_digest: str | None = None
    ) -> WriteResult:
        encoded = text.encode("utf-8")
        try:
            with self._parent(path, create=expected_digest is None) as (parent, name, relative):
                try:
                    fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise FileConflict("workspace directory is already being modified") from None
                try:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    exists, mode = False, 0o644
                    if expected_digest is not None:
                        raise FileConflict("new file must not set expected_sha256") from None
                else:
                    exists = True
                    if stat.S_ISLNK(current.st_mode):
                        raise WorkspacePathError("refusing to replace a symlink")
                    with os.fdopen(self._file(parent, name), "rb") as handle:
                        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    if expected_digest is None:
                        raise FileConflict("existing file requires expected_sha256")
                    if digest != expected_digest:
                        raise FileConflict("file changed since it was read")
                _atomic_write_at(parent, name, encoded, mode)
        except FileNotFoundError:
            if expected_digest is not None:
                raise FileConflict("new file must not set expected_sha256") from None
            raise
        return WriteResult(
            path=relative,
            digest=hashlib.sha256(encoded).hexdigest(),
            size=len(encoded),
            created=not exists,
        )

    def _search(
        self, query: str, path: str, limit: int, cancelled: threading.Event
    ) -> CommandResult:
        parts = self._parts(path)
        output = bytearray()
        truncated, matched = False, False

        def scan(parent: int, name: str, relative: str) -> bool:
            nonlocal matched, truncated
            try:
                with os.fdopen(self._file(parent, name), "rb") as handle:
                    raw = handle.read(MAX_SEARCH_FILE_BYTES + 1)
            except (FileNotFoundError, NotRegularFile):
                return False
            if len(raw) > MAX_SEARCH_FILE_BYTES:
                truncated = True
                return False
            if b"\0" in raw:
                return False
            for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                column = line.find(query)
                if column < 0:
                    continue
                matched = True
                encoded = f"{relative}:{number}:{column + 1}:{line}\n".encode()
                remaining = limit - len(output)
                output.extend(encoded[:remaining])
                if len(encoded) > remaining:
                    truncated = True
                    return True
            return False

        def result() -> CommandResult:
            return CommandResult(
                exit_code=0 if matched else 1,
                output=output.decode("utf-8", errors="ignore"),
                truncated=truncated,
            )

        if parts:
            with self._directory(parts[:-1]) as parent:
                mode = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
                if stat.S_ISREG(mode):
                    scan(parent, parts[-1], "/".join(parts))
                    return result()
                if not stat.S_ISDIR(mode):
                    raise WorkspacePathError("search target must be a real file or directory")
        with self._directory(parts) as directory:
            iterator = self._walk(
                directory, parts, DEFAULT_SKIP_DIRECTORIES, hidden=False, cancelled=cancelled
            )
            with closing(iterator) as walk:
                for parent, name, entry in walk:
                    if cancelled.is_set() or scan(parent, name, entry.path):
                        break
        return result()

    async def search(
        self, query: str, *, path: str = ".", max_output_bytes: int = 30_000
    ) -> CommandResult:
        """Literal search with no-follow traversal and bounded regular-file reads.

        Hidden entries and binary files are skipped. Files over the read ceiling
        are skipped and cause ``truncated=True``; output uses path:line:column.
        """
        if max_output_bytes < 1:
            raise ValueError("output limit must be positive")
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(self._search, query, path, max_output_bytes, cancelled)
        finally:
            cancelled.set()

    async def run(
        self, command: tuple[str, ...], *, max_output_bytes: int = 50_000
    ) -> CommandResult:
        if not command:
            raise ValueError("command must not be empty")
        return await run_command(command, workspace=self._root, max_output_bytes=max_output_bytes)


def local_backend(root: Path) -> LocalBackend:
    return LocalBackend(root)


def _atomic_write_at(directory: int, name: str, content: bytes, mode: int) -> None:
    temporary = f".forgeharness-write-{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


async def write_atomically(path: Path, content: bytes, mode: int) -> None:
    """Write an explicit trusted host path through a no-follow directory chain."""
    backend = LocalBackend(path.parent)

    def write() -> None:
        with backend._directory(()) as directory:
            _atomic_write_at(directory, path.name, content, mode)

    await asyncio.to_thread(write)


__all__ = ["DEFAULT_SKIP_DIRECTORIES", "LocalBackend", "local_backend", "write_atomically"]
