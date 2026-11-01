"""Deterministic compression for long tool observations.

Compression here is a projection, not a summary: it keeps the lines a reader would
have looked for (errors, file locations, the head and tail of the output) and drops
the rest. That keeps it reproducible — the same observation always compresses to the
same bytes, which is what makes replay and progress detection trustworthy.

Deterministic projection alone would be lossy, though. The line that mattered may be
"case 47 asserted 4, expected 3" — no error token, no ``file:line`` — and a lossy
compressor makes the model re-run the tool to recover it, which costs the tokens the
compression was supposed to save. So when a spill directory is configured the original
bytes are written to disk first and the projection carries a content-addressed
reference. Nothing is destroyed; it is moved out of the context window, and
``read_spilled`` can page it back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from forgeharness.context.spill import (
    SPILL_DIRECTORY_NAME,
    BlobStore,
    SpillCorrupt,
    SpillError,
    SpillNotFound,
    SpillUnavailable,
    blob_reference,
)

_HIGH_SIGNAL = re.compile(
    r"(?i)(error|failed|failure|traceback|exception|assert|\b[a-zA-Z0-9_./-]+:\d+\b)"
)

SPILL_SUBDIRECTORY_NAME = "observations"


def observation_spill_dir(workspace: Path) -> Path:
    """Return the one directory both the compressor and the reader must agree on."""
    return workspace / SPILL_DIRECTORY_NAME / SPILL_SUBDIRECTORY_NAME


@dataclass(frozen=True)
class CompressionResult:
    """What compression did, for hosts that need the reference as data."""

    text: str
    reference: str | None
    original_chars: int
    original_lines: int

    @property
    def compressed(self) -> bool:
        """True when the text is a projection rather than the original content."""
        return len(self.text) != self.original_chars


class ObservationCompressor:
    """Preserve errors and file locations while bounding long observations."""

    def __init__(self, *, max_chars: int = 8_000, spill_dir: Path | None = None) -> None:
        """Configure the ceiling and, optionally, where originals are kept."""
        if max_chars < 200:
            raise ValueError("max_chars must be at least 200")
        self._max_chars = max_chars
        self._store = None if spill_dir is None else BlobStore(spill_dir, prefix="obs")

    @classmethod
    def for_workspace(cls, workspace: Path, *, max_chars: int = 8_000) -> ObservationCompressor:
        """Spill into the workspace so ``read_observation`` can find the same files."""
        return cls(max_chars=max_chars, spill_dir=observation_spill_dir(workspace))

    @property
    def spill_dir(self) -> Path | None:
        """The directory originals are written to, or ``None`` when disabled."""
        return None if self._store is None else self._store.directory

    def bind_spill_dir(self, spill_dir: Path | None) -> None:
        """Point at the directory for the run that is starting.

        The workspace is not known when a runtime is composed — it arrives with the
        run — so the binding happens here rather than in the constructor.
        """
        self._store = None if spill_dir is None else BlobStore(spill_dir, prefix="obs")

    @staticmethod
    def reference_for(content: str) -> str:
        """Return the stable reference a given observation is stored under."""
        return blob_reference("obs", content)

    def compress(self, content: str) -> str:
        """Return short content unchanged or a labeled high-signal projection."""
        return self.compress_detailed(content).text

    def compress_detailed(self, content: str) -> CompressionResult:
        """Compress and report the reference the original was stored under."""
        lines = content.splitlines()
        if len(content) <= self._max_chars:
            return CompressionResult(
                text=content,
                reference=None,
                original_chars=len(content),
                original_lines=len(lines),
            )
        reference = self._spill(content)
        signal = [line for line in lines if _HIGH_SIGNAL.search(line)]
        candidates = [*lines[:8], *signal, *lines[-8:]]
        unique: list[str] = []
        seen: set[str] = set()
        for line in candidates:
            if line not in seen:
                unique.append(line)
                seen.add(line)
        header = f"[compressed observation: {len(content)} chars, {len(lines)} lines"
        header += f", full: {reference}]" if reference else "]"
        prefix = header + "\n"
        available = self._max_chars - len(prefix)
        body = "\n".join(unique)
        if len(body) > available:
            body = body[: max(0, available - 16)] + "\n[truncated]"
        return CompressionResult(
            text=prefix + body,
            reference=reference,
            original_chars=len(content),
            original_lines=len(lines),
        )

    def read_spilled(self, reference: str, *, offset: int = 0, limit: int | None = None) -> str:
        """Return the stored original, optionally one window of it.

        ``offset``/``limit`` are character offsets so a caller can page through a
        large observation without pulling all of it back into the context window —
        reading everything back would undo the compression that made room for it.
        """
        if self._store is None:
            raise SpillUnavailable("no spill directory is configured for this compressor")
        return self._store.read(reference, offset=offset, limit=limit)

    def _spill(self, content: str) -> str | None:
        """Write the original under its content digest, returning the reference."""
        if self._store is None:
            return None
        return self._store.put(content)


__all__ = [
    "SPILL_DIRECTORY_NAME",
    "SPILL_SUBDIRECTORY_NAME",
    "CompressionResult",
    "ObservationCompressor",
    "SpillCorrupt",
    "SpillError",
    "SpillNotFound",
    "SpillUnavailable",
    "observation_spill_dir",
]
