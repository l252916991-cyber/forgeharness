"""Deterministic compression for long tool observations."""

from __future__ import annotations

import re

_HIGH_SIGNAL = re.compile(
    r"(?i)(error|failed|failure|traceback|exception|assert|\b[a-zA-Z0-9_./-]+:\d+\b)"
)


class ObservationCompressor:
    """Preserve errors and file locations while bounding long observations."""

    def __init__(self, *, max_chars: int = 8_000) -> None:
        if max_chars < 200:
            raise ValueError("max_chars must be at least 200")
        self._max_chars = max_chars

    def compress(self, content: str) -> str:
        """Return short content unchanged or a labeled high-signal projection."""
        if len(content) <= self._max_chars:
            return content
        lines = content.splitlines()
        signal = [line for line in lines if _HIGH_SIGNAL.search(line)]
        candidates = [*lines[:8], *signal, *lines[-8:]]
        unique: list[str] = []
        seen: set[str] = set()
        for line in candidates:
            if line not in seen:
                unique.append(line)
                seen.add(line)
        prefix = f"[compressed observation: {len(content)} chars, {len(lines)} lines]\n"
        available = self._max_chars - len(prefix)
        body = "\n".join(unique)
        if len(body) > available:
            body = body[: max(0, available - 16)] + "\n[truncated]"
        return prefix + body
