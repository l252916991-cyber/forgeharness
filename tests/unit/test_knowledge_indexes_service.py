"""Tests for hybrid retrieval, Qdrant boundaries, and grounded generation."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from forgeharness.knowledge.indexes import (
    HybridRetriever,
    InMemoryVectorStore,
    QdrantVectorStore,
    SQLiteLexicalIndex,
    VectorSpaceMismatch,
)
from forgeharness.knowledge.models import Chunk
from forgeharness.knowledge.parsers import LocalDocumentParser
from forgeharness.knowledge.service import KnowledgeService
from forgeharness.knowledge.storage import SQLiteApplicationStore
from forgeharness.knowledge.testing import (
    DeterministicChatModel,
    DeterministicEmbeddingModel,
    DeterministicReranker,
)


def _chunk(identifier: str, content: str) -> Chunk:
    return Chunk(
        id=identifier * 64,
        document_id=("f" if identifier != "f" else "e") * 64,
        source=f"{identifier}.md",
        content=content,
        start_line=1,
        end_line=1,
    )


async def test_qdrant_memory_deletion_covers_versions_but_not_knowledge() -> None:
    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "collections": [
                            {"name": "memory_v1_2560"},
                            {"name": "memory_v2_2560"},
                            {"name": "knowledge_v1_2560"},
                        ]
                    }
                },
            )
        deleted_paths.append(request.url.path)
        assert request.url.params["wait"] == "true"
        assert len(json.loads(request.content)["points"]) == 1
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vector = QdrantVectorStore(collection="memory", client=client)
        await vector.delete([])
        await vector.delete(["a" * 64])
    assert deleted_paths == [
        "/collections/memory_v1_2560/points/delete",
        "/collections/memory_v2_2560/points/delete",
    ]


def test_sqlite_lexical_index_upserts_and_validates(tmp_path: Path) -> None:
    index = SQLiteLexicalIndex(tmp_path / "fts.sqlite3")
    first = _chunk("a", "agent tools schema")
    second = _chunk("b", "vector retrieval")
    index.upsert([first, second])
    hits = index.search("agent tools", limit=5)
    assert hits[0].chunk == first
    assert hits[0].stages == ("lexical",)
    changed = first.model_copy(update={"content": "approval policy"})
    index.upsert([changed])
    assert index.search("agent", limit=5) == ()
    assert index.search("approval", limit=5)[0].chunk.content == "approval policy"
    with pytest.raises(ValueError, match="must not be empty"):
        index.search(" ", limit=5)
    with pytest.raises(ValueError, match="between 1 and 100"):
        index.search("agent", limit=101)
    assert index.search("!!!", limit=5) == ()


async def test_in_memory_vector_store_guards_space_and_ranks() -> None:
    store = InMemoryVectorStore()
    with pytest.raises(VectorSpaceMismatch, match="ensure_space"):
        await store.upsert([], [])
    await store.ensure_space(model_name="embed", dimension=2)
    await store.ensure_space(model_name="embed", dimension=2)
    with pytest.raises(VectorSpaceMismatch, match="reindex"):
        await store.ensure_space(model_name="other", dimension=2)
    with pytest.raises(ValueError, match="counts differ"):
        await store.upsert([_chunk("a", "a")], [])
    with pytest.raises(VectorSpaceMismatch, match="dimension"):
        await store.upsert([_chunk("a", "a")], [[1.0]])
    await store.upsert(
        [_chunk("a", "agent"), _chunk("b", "vector")],
        [[1.0, 0.0], [0.0, 1.0]],
    )
    hits = await store.search([0.9, 0.1], limit=2)
    assert [hit.chunk.source for hit in hits] == ["a.md", "b.md"]
    with pytest.raises(VectorSpaceMismatch, match="query vector"):
        await store.search([1.0], limit=1)


async def test_hybrid_service_ingests_searches_answers_and_retries(tmp_path: Path) -> None:
    application_store = SQLiteApplicationStore(tmp_path / "app.db", tmp_path / "media")
    lexical = SQLiteLexicalIndex(tmp_path / "fts.db")
    vector = InMemoryVectorStore()
    embedding = DeterministicEmbeddingModel()
    reranker = DeterministicReranker()
    retriever = HybridRetriever(
        lexical=lexical, vector=vector, embedding=embedding, reranker=reranker
    )
    service = KnowledgeService(
        parser=LocalDocumentParser(),
        documents=application_store,
        lexical=lexical,
        vector=vector,
        embedding=embedding,
        retriever=retriever,
        chat=DeterministicChatModel(),
    )
    document, chunks, inserted = await service.ingest(
        filename="guide.md", data=b"# Tools\nagent tools validate schema"
    )
    assert inserted is True and document.id == chunks[0].document_id
    _, _, inserted_again = await service.ingest(
        filename="guide.md", data=b"# Tools\nagent tools validate schema"
    )
    assert inserted_again is False
    report = await service.search("agent tools")
    assert report.lexical and report.vector and report.fused and report.final
    assert "rerank" in report.final[0].stages
    response = await service.answer("agent tools")
    assert response.citations[0].source == "guide.md"
    assert "检索证据" in response.answer


class _FailingReranker:
    @property
    def model_name(self) -> str:
        return "broken"

    async def rerank(
        self, query: str, documents: list[str], *, limit: int
    ) -> tuple[tuple[int, float], ...]:
        del query, documents, limit
        raise RuntimeError("offline")


async def test_hybrid_retrieval_falls_back_when_reranker_fails(tmp_path: Path) -> None:
    lexical = SQLiteLexicalIndex(tmp_path / "fts.db")
    vector = InMemoryVectorStore()
    embedding = DeterministicEmbeddingModel()
    chunk = _chunk("a", "agent approval")
    lexical.upsert([chunk])
    vector_value = (await embedding.embed([chunk.content]))[0]
    await vector.ensure_space(model_name=embedding.model_name, dimension=len(vector_value))
    await vector.upsert([chunk], [vector_value])
    retriever = HybridRetriever(
        lexical=lexical,
        vector=vector,
        embedding=embedding,
        reranker=_FailingReranker(),
    )
    report = await retriever.retrieve("agent")
    assert report.final == report.fused[:5]
    with pytest.raises(ValueError, match="must not be empty"):
        await retriever.retrieve(" ")


async def test_grounded_service_refuses_without_evidence(tmp_path: Path) -> None:
    lexical = SQLiteLexicalIndex(tmp_path / "fts.db")
    vector = InMemoryVectorStore()
    embedding = DeterministicEmbeddingModel()
    probe = (await embedding.embed(["probe"]))[0]
    await vector.ensure_space(model_name=embedding.model_name, dimension=len(probe))
    retriever = HybridRetriever(lexical=lexical, vector=vector, embedding=embedding)
    service = KnowledgeService(
        parser=LocalDocumentParser(),
        documents=SQLiteApplicationStore(tmp_path / "app.db", tmp_path / "media"),
        lexical=lexical,
        vector=vector,
        embedding=embedding,
        retriever=retriever,
        chat=DeterministicChatModel(),
    )
    response = await service.answer("missing")
    assert response.answer.startswith("不知道")
    assert response.citations == ()


async def test_qdrant_adapter_creates_space_upserts_and_searches() -> None:
    requests: list[httpx.Request] = []
    chunk = _chunk("a", "agent tools")

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/collections/"):
            return httpx.Response(404, request=request)
        if request.method == "PUT" and "/points" not in path:
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.method == "PUT" and path.endswith("/points"):
            body = json.loads(request.content)
            assert body["points"][0]["payload"]["embedding_model"] == "embed"
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.method == "POST" and path.endswith("/points/search"):
            return httpx.Response(
                200,
                json={"result": [{"score": 0.9, "payload": chunk.model_dump(mode="json")}]},
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = QdrantVectorStore(base_url="http://qdrant", client=client)
    await store.ensure_space(model_name="embed", dimension=2)
    await store.upsert([chunk], [[1.0, 0.0]])
    hits = await store.search([1.0, 0.0], limit=1)
    assert hits[0].chunk == chunk
    assert any("_" in request.url.path for request in requests)
    await store.close()
    await client.aclose()


async def test_qdrant_adapter_rejects_existing_dimension_and_bad_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"config": {"params": {"vectors": {"size": 3}}}}},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = QdrantVectorStore(base_url="http://qdrant", client=client)
    with pytest.raises(VectorSpaceMismatch, match="dimension is 3"):
        await store.ensure_space(model_name="embed", dimension=2)
    with pytest.raises(VectorSpaceMismatch, match="ensure_space"):
        await store.upsert([], [])
    with pytest.raises(VectorSpaceMismatch, match="query vector"):
        await store.search([1.0], limit=1)
    await client.aclose()


async def test_hybrid_retriever_recovers_qdrant_space_in_a_fresh_process(
    tmp_path: Path,
) -> None:
    chunk = _chunk("a", "agent tools")
    embedding = DeterministicEmbeddingModel(dimension=8)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/collections/"):
            return httpx.Response(
                200,
                json={"result": {"config": {"params": {"vectors": {"size": 8}}}}},
                request=request,
            )
        if request.method == "POST" and request.url.path.endswith("/points/search"):
            return httpx.Response(
                200,
                json={"result": [{"score": 0.9, "payload": chunk.model_dump(mode="json")}]},
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fresh_store = QdrantVectorStore(base_url="http://qdrant", client=client)
    retriever = HybridRetriever(
        lexical=SQLiteLexicalIndex(tmp_path / "fresh-fts.db"),
        vector=fresh_store,
        embedding=embedding,
    )
    report = await retriever.retrieve("agent tools")
    assert report.vector[0].chunk == chunk
    await client.aclose()
