"""Deterministic application adapters for keyless tests and demos."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence


class DeterministicChatModel:
    """Return evidence-focused text without contacting a model server."""

    @property
    def model_name(self) -> str:
        return "deterministic-chat"

    async def complete(self, *, system: str, user: str) -> str:
        del system
        evidence = user.split("EVIDENCE:\n", 1)[-1].strip()
        if not evidence:
            return "不知道: 没有检索到足够证据。"
        return f"根据检索证据: {evidence[:400]}"

    async def describe_image(self, *, data: bytes, mime_type: str) -> str:
        return f"Deterministic image evidence: mime={mime_type}, bytes={len(data)}"


class DeterministicEmbeddingModel:
    """Hash token features into a normalized vector for repeatable tests."""

    def __init__(self, dimension: int = 64) -> None:
        if dimension < 8:
            raise ValueError("deterministic embedding dimension must be at least 8")
        self.dimension = dimension

    @property
    def model_name(self) -> str:
        return f"deterministic-hash-{self.dimension}"

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._one(text) for text in texts)

    def _one(self, text: str) -> tuple[float, ...]:
        values = [0.0] * self.dimension
        for token in re.findall(r"[\w\u4e00-\u9fff]+", text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            values[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return tuple(value / norm for value in values)


class DeterministicReranker:
    """Rank by literal token overlap for deterministic control evidence."""

    @property
    def model_name(self) -> str:
        return "deterministic-overlap"

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> tuple[tuple[int, float], ...]:
        query_tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", query.lower()))
        ranked = []
        for index, document in enumerate(documents):
            tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", document.lower()))
            ranked.append((index, float(len(query_tokens & tokens))))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return tuple(ranked[:limit])
