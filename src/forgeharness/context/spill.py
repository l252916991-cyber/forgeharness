"""Content-addressed storage for anything the runtime deliberately left behind.

Two different things get dropped from a model request and must survive it:

* a long tool observation, replaced by a deterministic projection (``obs://``);
* whole conversation turns, replaced by a summary (``ctx://``).

Both are the same problem — write the original bytes somewhere addressable, and let a
reader page them back — so both use one store here. The reference is the SHA-256 of
the stored bytes, which makes it a *proof*: a reader can verify it got the bytes the
runtime actually wrote, and a corrupted or substituted file is refused rather than
silently trusted.

Paging is by character offset for observations and by line for messages, because a
message history is a list of lines and "give me messages 3-7" is the useful verb.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from forgeharness.domain.models import Message

#: Characters of the content digest used as the reference. 96 bits keeps a
#: collision out of reach while staying short enough to sit in a context header.
REFERENCE_CHARS = 24
MAX_BLOB_BYTES = 16 * 1024 * 1024

#: Spill files live under this name inside the workspace the run owns.
SPILL_DIRECTORY_NAME = ".forgeharness"

BlobPrefix = Literal["obs", "ctx"]


class SpillError(RuntimeError):
    """A spilled blob could not be served."""


class SpillUnavailable(SpillError):
    """No spill directory was configured, so nothing was ever written."""


class SpillNotFound(SpillError):
    """The reference is well formed but names no file this run wrote."""


class SpillCorrupt(SpillError):
    """The stored bytes no longer hash to the reference that names them."""


def _article(prefix: BlobPrefix) -> str:
    """Name the kind of reference in the caller's language, articles included."""
    return "an observation" if prefix == "obs" else "a compacted-message"


def blob_reference(prefix: BlobPrefix, content: str) -> str:
    """Reference for ``content`` under ``prefix``, without needing a store instance."""
    return f"{prefix}://{BlobStore.digest_of(content)[:REFERENCE_CHARS]}"


class BlobStore:
    """One directory of content-addressed text blobs behind a typed reference."""

    def __init__(self, directory: Path, *, prefix: BlobPrefix) -> None:
        if prefix not in ("obs", "ctx"):
            raise ValueError(f"unsupported blob prefix: {prefix!r}")
        self._directory = directory
        self._prefix = prefix
        self._pattern = re.compile(rf"^{prefix}://([0-9a-f]{{{REFERENCE_CHARS}}})$")

    @property
    def directory(self) -> Path:
        """Where blobs are written; callers bind it per run, not per process."""
        return self._directory

    @property
    def prefix(self) -> BlobPrefix:
        """The scheme this store answers to."""
        return self._prefix

    @staticmethod
    def digest_of(content: str) -> str:
        """Full SHA-256 of the stored text, so a reader can verify what it got."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def reference_for(self, content: str) -> str:
        """Return the stable reference a given text is stored under."""
        return blob_reference(self._prefix, content)

    def put(self, content: str) -> str:
        """Write text under its digest and return the reference."""
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_BLOB_BYTES:
            raise SpillError("spill content exceeds its byte limit")
        reference = self.reference_for(content)
        digest = reference.removeprefix(f"{self._prefix}://")
        name = f"{digest}.{self._suffix}"
        with self._open_directory(create=True) as directory:
            try:
                existing = self._read_file(directory, name)
            except SpillNotFound:
                pass
            else:
                if existing != content:
                    raise SpillCorrupt("existing spill content differs from its digest")
                return reference
            # Exclusive unpredictable names prevent a workspace's prepared symlink
            # from becoming a write destination. All operations stay relative to an
            # open, no-follow directory chain; replacing the final entry never
            # follows a concurrently inserted symlink.
            temporary = f".{digest}.{uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return reference

    def read(self, reference: str, *, offset: int = 0, limit: int | None = None) -> str:
        """Return the stored text, optionally one window of it, digest-checked."""
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        expected = self._identifier(reference)
        with self._open_directory(create=False) as directory:
            content = self._read_file(directory, f"{expected}.{self._suffix}")
        if self.digest_of(content)[:REFERENCE_CHARS] != expected:
            raise SpillCorrupt(f"{reference} does not match the bytes stored under it")
        if limit is not None:
            return content[offset : offset + limit]
        return content[offset:]

    def resolve(self, reference: str) -> tuple[Path, str]:
        """Turn a reference into a path, refusing anything that is not one of ours."""
        expected = self._identifier(reference)
        # This compatibility method returns a path; library reads still open it
        # through the protected descriptor chain instead of trusting this check.
        self.read(reference, limit=0)
        return self._directory / f"{expected}.{self._suffix}", expected

    def _identifier(self, reference: str) -> str:
        match = self._pattern.fullmatch(reference)
        if match is None:
            raise ValueError(f"not {_article(self._prefix)} reference: {reference!r}")
        return match.group(1)

    @contextmanager
    def _open_directory(self, *, create: bool) -> Iterator[int]:
        """Walk POSIX path components without resolving workspace symlinks."""
        absolute = Path(os.path.abspath(self._directory))
        descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in absolute.parts[1:]:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                except FileNotFoundError:
                    raise SpillNotFound("spill directory is unavailable") from None
                except OSError:
                    raise SpillError("spill path must contain only real directories") from None
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_file(directory: int, name: str) -> str:
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            raise SpillNotFound("stored blob is unavailable") from None
        except OSError:
            raise SpillCorrupt("stored blob must be a regular file") from None
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            # A concurrent atomic promotion can unlink the old inode after our
            # no-follow open. Its descriptor remains safe and digest-verified;
            # only multiple links create an outside-alias concern.
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                raise SpillCorrupt("stored blob must be a single-link regular file")
            if metadata.st_size > MAX_BLOB_BYTES:
                raise SpillCorrupt("stored blob exceeds its byte limit")
            encoded = source.read(MAX_BLOB_BYTES + 1)
            if len(encoded) > MAX_BLOB_BYTES:
                raise SpillCorrupt("stored blob exceeds its byte limit")
            try:
                return encoded.decode("utf-8")
            except UnicodeDecodeError:
                raise SpillCorrupt("stored blob is not UTF-8") from None

    @property
    def _suffix(self) -> str:
        return "txt" if self._prefix == "obs" else "jsonl"


class MessageSpill:
    """Keep the exact messages a compaction folded away, addressable by ``ctx://``.

    The summary that replaces them is a projection: it truncates each message to a
    preview and states its size. That is enough to remember that a tool ran, and not
    enough to remember what it said. Without this store, recovering a dropped detail
    means re-running the tool — which is precisely the cost compaction was meant to
    avoid. With it, the summary carries a reference and the model can page the
    original back.
    """

    def __init__(self, workspace: Path) -> None:
        self._store = BlobStore(workspace / SPILL_DIRECTORY_NAME / "context", prefix="ctx")

    @property
    def directory(self) -> Path:
        """Where conversation blobs are written for this workspace."""
        return self._store.directory

    def store(self, messages: tuple[Message, ...]) -> str:
        """Write the dropped messages as JSON lines and return the reference."""
        body = "\n".join(
            json.dumps(message.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for message in messages
        )
        if body:
            body += "\n"
        return self._store.put(body)

    def read(self, reference: str, *, offset: int = 0, limit: int | None = None) -> str:
        """Return a window of stored messages, ``offset``/``limit`` in lines."""
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        body = self._store.read(reference)
        lines = body.splitlines()
        window = lines[offset:] if limit is None else lines[offset : offset + limit]
        if not window:
            return f"[no stored messages at offset {offset}]"
        return "\n".join(window)

    def count(self, reference: str) -> int:
        """How many messages are stored behind a reference."""
        return len(self._store.read(reference).splitlines())


def context_spill_dir(workspace: Path) -> Path:
    """The one directory the assembler and the reader must agree on."""
    return workspace / SPILL_DIRECTORY_NAME / "context"


__all__ = [
    "REFERENCE_CHARS",
    "SPILL_DIRECTORY_NAME",
    "BlobPrefix",
    "BlobStore",
    "MessageSpill",
    "SpillCorrupt",
    "SpillError",
    "SpillNotFound",
    "SpillUnavailable",
    "blob_reference",
    "context_spill_dir",
]
