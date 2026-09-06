"""Lexical, vector, fusion, and reranking implementations."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx

from forgeharness.knowledge.models import Chunk, RetrievalReport, SearchHit
from forgeharness.knowledge.protocols import EmbeddingModel, LexicalIndex, Reranker, VectorStore
from forgeharness.state.sqlite import connect_wal


class VectorSpaceMismatch(RuntimeError):
    """Stored vectors cannot be mixed with the configured embedding model."""


class SQLiteLexicalIndex:
    """Small, inspectable FTS5 baseline with source-preserving payload storage."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    chunk_id UNINDEXED,
                    document_id UNINDEXED,
                    source UNINDEXED,
                    content,
                    start_line UNINDEXED,
                    end_line UNINDEXED,
                    page UNINDEXED,
                    kind UNINDEXED,
                    tokenize='unicode61'
                )
                """
            )

    def upsert(self, chunks: Sequence[Chunk]) -> None:
        with self._connect() as connection:
            for chunk in chunks:
                connection.execute("DELETE FROM knowledge_fts WHERE chunk_id = ?", (chunk.id,))
                connection.execute(
                    """
                    INSERT INTO knowledge_fts(
                        chunk_id, document_id, source, content,
                        start_line, end_line, page, kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.source,
                        chunk.content,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.page,
                        chunk.kind,
                    ),
                )

    def delete(self, chunk_ids: Sequence[str]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM knowledge_fts WHERE chunk_id = ?", [(item,) for item in chunk_ids]
            )

    def search(self, query: str, *, limit: int) -> tuple[SearchHit, ...]:
        if not query.strip():
            raise ValueError("lexical query must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("lexical limit must be between 1 and 100")
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", query.lower())[:20]
        if not tokens:
            return ()
        expression = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chunk_id, document_id, source, content,
                       start_line, end_line, page, kind
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ?
                ORDER BY bm25(knowledge_fts)
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
        hits = []
        for rank, row in enumerate(rows, start=1):
            chunk = Chunk(
                id=row[0],
                document_id=row[1],
                source=row[2],
                content=row[3],
                start_line=_optional_int(row[4]),
                end_line=_optional_int(row[5]),
                page=_optional_int(row[6]),
                kind=row[7],
            )
            hits.append(SearchHit(chunk=chunk, score=1.0 / rank, rank=rank, stages=("lexical",)))
        return tuple(hits)

    def _connect(self) -> sqlite3.Connection:
        return connect_wal(self._path)

    def document_chunks(self, document_id: str, *, limit: int) -> tuple[Chunk, ...]:
        """Resolve explicit attachments by identity, never by a hash-as-query trick."""
        if not 1 <= limit <= 100:
            raise ValueError("document chunk limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chunk_id, document_id, source, content,
                       start_line, end_line, page, kind
                FROM knowledge_fts WHERE document_id = ?
                ORDER BY CAST(page AS INTEGER), CAST(start_line AS INTEGER), chunk_id
                LIMIT ?
                """,
                (document_id, limit),
            ).fetchall()
        return tuple(
            Chunk(
                id=row[0],
                document_id=row[1],
                source=row[2],
                content=row[3],
                start_line=_optional_int(row[4]),
                end_line=_optional_int(row[5]),
                page=_optional_int(row[6]),
                kind=row[7],
            )
            for row in rows
        )


class InMemoryVectorStore:
    """Cosine vector store used by tests and the no-Docker learning profile."""

    def __init__(self) -> None:
        self._space: tuple[str, int] | None = None
        self._items: dict[str, tuple[Chunk, tuple[float, ...]]] = {}

    async def ensure_space(self, *, model_name: str, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError("vector dimension must be positive")
        requested = (model_name, dimension)
        if self._space is not None and self._space != requested:
            raise VectorSpaceMismatch(
                f"vector space is {self._space}, requested {requested}; reindex required"
            )
        self._space = requested

    async def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunk and vector counts differ")
        if self._space is None:
            raise VectorSpaceMismatch("ensure_space must run before vector upsert")
        dimension = self._space[1]
        for chunk, vector in zip(chunks, vectors, strict=True):
            normalized = tuple(float(value) for value in vector)
            if len(normalized) != dimension:
                raise VectorSpaceMismatch("vector dimension differs from configured space")
            self._items[chunk.id] = (chunk, normalized)

    async def search(self, vector: Sequence[float], *, limit: int) -> tuple[SearchHit, ...]:
        if self._space is None or len(vector) != self._space[1]:
            raise VectorSpaceMismatch("query vector differs from configured space")
        scored = [(chunk, _cosine(vector, stored)) for chunk, stored in self._items.values()]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda item: (-item[1], item[0].id))
        return tuple(
            SearchHit(chunk=chunk, score=score, rank=rank, stages=("vector",))
            for rank, (chunk, score) in enumerate(scored[:limit], start=1)
        )

    async def ping(self) -> bool:
        return True

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        for chunk_id in chunk_ids:
            self._items.pop(chunk_id, None)


class QdrantVectorStore:
    """Qdrant REST adapter with embedding-model and dimension guardrails."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:6333",
        collection: str = "forgeharness_knowledge",
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._collection_base = collection
        self._collection = collection
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30, trust_env=False)
        self._space: tuple[str, int] | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ping(self) -> bool:
        response = await self._request("GET", "/collections")
        return response.status_code == 200

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        """Remove revoked memory from all versions of this isolated collection family."""
        if not chunk_ids:
            return
        response = await self._request("GET", "/collections")
        for collection in response.json()["result"]["collections"]:
            name = str(collection["name"])
            if not name.startswith(f"{self._collection_base}_"):
                continue
            await self._request(
                "POST",
                f"/collections/{name}/points/delete",
                params={"wait": "true"},
                json={"points": [str(uuid5(NAMESPACE_URL, item)) for item in chunk_ids]},
            )

    async def ensure_space(self, *, model_name: str, dimension: int) -> None:
        fingerprint = hashlib.sha256(model_name.encode()).hexdigest()[:10]
        self._collection = f"{self._collection_base}_{fingerprint}_{dimension}"
        response = await self._request("GET", f"/collections/{self._collection}", allow_404=True)
        if response.status_code == 404:
            created = await self._request(
                "PUT",
                f"/collections/{self._collection}",
                json={
                    "vectors": {"size": dimension, "distance": "Cosine"},
                    "on_disk_payload": True,
                },
            )
            if not created.json().get("status") == "ok":
                raise RuntimeError("Qdrant did not acknowledge collection creation")
        else:
            payload = response.json()
            try:
                actual = int(payload["result"]["config"]["params"]["vectors"]["size"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Qdrant collection metadata is invalid") from exc
            if actual != dimension:
                raise VectorSpaceMismatch(
                    f"Qdrant dimension is {actual}, configured embedding dimension is {dimension}"
                )
        self._space = (model_name, dimension)

    async def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        if self._space is None:
            raise VectorSpaceMismatch("ensure_space must run before Qdrant upsert")
        if len(chunks) != len(vectors):
            raise ValueError("chunk and vector counts differ")
        points = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            if len(vector) != self._space[1]:
                raise VectorSpaceMismatch("vector dimension differs from Qdrant space")
            payload = chunk.model_dump(mode="json")
            payload["embedding_model"] = self._space[0]
            points.append(
                {
                    "id": str(uuid5(NAMESPACE_URL, chunk.id)),
                    "vector": list(vector),
                    "payload": payload,
                }
            )
        await self._request(
            "PUT",
            f"/collections/{self._collection}/points",
            params={"wait": "true"},
            json={"points": points},
        )

    async def search(self, vector: Sequence[float], *, limit: int) -> tuple[SearchHit, ...]:
        if self._space is None or len(vector) != self._space[1]:
            raise VectorSpaceMismatch("query vector differs from Qdrant space")
        response = await self._request(
            "POST",
            f"/collections/{self._collection}/points/search",
            json={
                "vector": list(vector),
                "limit": limit,
                "score_threshold": 0.15,
                "with_payload": True,
                "filter": {
                    "must": [
                        {
                            "key": "embedding_model",
                            "match": {"value": self._space[0]},
                        }
                    ]
                },
            },
        )
        result = response.json().get("result", [])
        hits = []
        for rank, item in enumerate(result, start=1):
            payload = dict(item.get("payload", {}))
            payload.pop("embedding_model", None)
            hits.append(
                SearchHit(
                    chunk=Chunk.model_validate(payload),
                    score=float(item["score"]),
                    rank=rank,
                    stages=("vector",),
                )
            )
        return tuple(hits)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_404: bool = False,
        json: object | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {"api-key": self._api_key} if self._api_key else None
        response = await self._client.request(
            method,
            f"{self._base_url}{path}",
            headers=headers,
            json=json,
            params=params,
        )
        if not (allow_404 and response.status_code == 404):
            response.raise_for_status()
        return response


class HybridRetriever:
    """Fuse lexical and semantic ranks, then optionally cross-encode candidates."""

    def __init__(
        self,
        *,
        lexical: LexicalIndex,
        vector: VectorStore,
        embedding: EmbeddingModel,
        reranker: Reranker | None = None,
        candidate_limit: int = 20,
        min_rerank_score: float = 0.0,
    ) -> None:
        self._lexical = lexical
        self._vector = vector
        self._embedding = embedding
        self._reranker = reranker
        self._candidate_limit = candidate_limit
        self._min_rerank_score = min_rerank_score

    async def retrieve(self, query: str, *, limit: int = 5) -> RetrievalReport:
        if not query.strip():
            raise ValueError("retrieval query must not be empty")
        started = time.perf_counter()
        embedding_started = time.perf_counter()
        vectors = await self._embedding.embed([query])
        timings = {"embedding": (time.perf_counter() - embedding_started) * 1_000}
        if not vectors:
            raise RuntimeError("embedding model returned no query vector")
        await self._vector.ensure_space(
            model_name=self._embedding.model_name,
            dimension=len(vectors[0]),
        )
        lexical_task = _timed_lexical(self._lexical, query, self._candidate_limit)
        vector_task = _timed_vector(self._vector, vectors[0], self._candidate_limit)
        (lexical, lexical_ms), (vector, vector_ms) = await asyncio.gather(lexical_task, vector_task)
        timings["lexical"] = lexical_ms
        timings["vector"] = vector_ms
        fusion_started = time.perf_counter()
        fused = reciprocal_rank_fusion(lexical, vector)
        timings["fusion"] = (time.perf_counter() - fusion_started) * 1_000
        final = fused[:limit]
        if self._reranker is not None and fused:
            try:
                rerank_started = time.perf_counter()
                ranking = await self._reranker.rerank(
                    query,
                    [hit.chunk.content for hit in fused],
                    limit=min(limit, len(fused)),
                )
                timings["reranker"] = (time.perf_counter() - rerank_started) * 1_000
                accepted = tuple(
                    (index, score) for index, score in ranking if score > self._min_rerank_score
                )
                final = tuple(
                    SearchHit(
                        chunk=fused[index].chunk,
                        score=score,
                        rank=rank,
                        stages=(*fused[index].stages, "rerank"),
                    )
                    for rank, (index, score) in enumerate(accepted, start=1)
                )
            except Exception:
                # Retrieval remains available when an optional cross-encoder is unhealthy.
                timings["reranker_fallback"] = (time.perf_counter() - rerank_started) * 1_000
                final = fused[:limit]
        return RetrievalReport(
            query=query,
            lexical=lexical,
            vector=vector,
            fused=fused,
            final=final,
            latency_ms=(time.perf_counter() - started) * 1_000,
            timings_ms=timings,
        )


def reciprocal_rank_fusion(
    *rankings: Sequence[SearchHit], constant: int = 60
) -> tuple[SearchHit, ...]:
    """Combine rank positions without assuming comparable backend scores."""
    scores: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    stages: dict[str, list[str]] = {}
    for ranking in rankings:
        for hit in ranking:
            scores[hit.chunk.id] = scores.get(hit.chunk.id, 0.0) + 1.0 / (constant + hit.rank)
            chunks[hit.chunk.id] = hit.chunk
            stages.setdefault(hit.chunk.id, []).extend(hit.stages)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return tuple(
        SearchHit(
            chunk=chunks[chunk_id],
            score=scores[chunk_id],
            rank=rank,
            stages=tuple(dict.fromkeys((*stages[chunk_id], "fusion"))),
        )
        for rank, chunk_id in enumerate(ordered, start=1)
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _optional_int(value: object) -> int | None:
    return None if value in (None, "") else int(str(value))


async def _timed_lexical(
    index: LexicalIndex, query: str, limit: int
) -> tuple[tuple[SearchHit, ...], float]:
    started = time.perf_counter()
    result = await asyncio.to_thread(index.search, query, limit=limit)
    return result, (time.perf_counter() - started) * 1_000


async def _timed_vector(
    store: VectorStore, vector: Sequence[float], limit: int
) -> tuple[tuple[SearchHit, ...], float]:
    started = time.perf_counter()
    result = await store.search(vector, limit=limit)
    return result, (time.perf_counter() - started) * 1_000
