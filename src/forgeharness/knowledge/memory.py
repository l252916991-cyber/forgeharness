"""Review-gated semantic long-term memory."""

from __future__ import annotations

import hashlib

from forgeharness.knowledge.models import Chunk, RetrievalReport, SearchHit
from forgeharness.knowledge.protocols import EmbeddingModel, LexicalIndex, Retriever, VectorStore
from forgeharness.state.memory import (
    MemoryRecord,
    MemoryStatus,
    MemoryStoreError,
    SQLiteMemoryStore,
)


class SemanticMemoryService:
    """Index only approved records in a vector space isolated from documents."""

    def __init__(
        self,
        *,
        records: SQLiteMemoryStore,
        lexical: LexicalIndex,
        vector: VectorStore,
        embedding: EmbeddingModel,
        retriever: Retriever,
    ) -> None:
        self.records = records
        self._lexical = lexical
        self._vector = vector
        self._embedding = embedding
        self._retriever = retriever

    async def review(self, record_id: str, *, approve: bool) -> MemoryRecord:
        if not approve:
            return await self.revoke(record_id)
        current = self.records.get(record_id)
        if current is None:
            raise MemoryStoreError(f"unknown memory record: {record_id}")
        if current.status in {MemoryStatus.REJECTED, MemoryStatus.DELETED}:
            raise MemoryStoreError(f"memory record is already {current.status.value}")
        if current.status == MemoryStatus.APPROVED and self.records.is_indexed(record_id):
            raise MemoryStoreError("memory record is already approved")
        record = (
            self.records.review(record_id, approve=True)
            if current.status == MemoryStatus.CANDIDATE
            else current
        )
        chunk = _memory_chunk(record)
        vectors = await self._embedding.embed([chunk.content])
        await self._vector.ensure_space(
            model_name=self._embedding.model_name, dimension=len(vectors[0])
        )
        self._lexical.upsert([chunk])
        await self._vector.upsert([chunk], vectors)
        self.records.mark_indexed(record_id)
        return record

    async def search(self, query: str, *, limit: int = 5) -> RetrievalReport:
        return await self.retrieve(query, limit=limit)

    async def retrieve(self, query: str, *, limit: int = 5) -> RetrievalReport:
        """Expose the authoritative filter through the public Retriever protocol."""
        report = await self._retriever.retrieve(query, limit=limit)

        def approved(hits: tuple[SearchHit, ...]) -> tuple[SearchHit, ...]:
            accepted: list[SearchHit] = []
            for hit in hits:
                record_id = hit.chunk.source.removeprefix("memory:")
                record = self.records.get(record_id)
                if (
                    record is not None
                    and record.status == MemoryStatus.APPROVED
                    and self.records.is_indexed(record_id)
                ):
                    accepted.append(hit.model_copy(update={"rank": len(accepted) + 1}))
            return tuple(accepted)

        # The record is the authority even if a remote deletion fails or races
        # an approval. Stale vector payloads must not resurrect revoked memory.
        return report.model_copy(
            update={
                "lexical": approved(report.lexical),
                "vector": approved(report.vector),
                "fused": approved(report.fused),
                "final": approved(report.final),
            }
        )

    async def revoke(self, record_id: str, *, deleted: bool = False) -> MemoryRecord:
        record = self.records.revoke(record_id, deleted=deleted)
        chunk_id = _memory_chunk(record).id
        self._lexical.delete([chunk_id])
        await self._vector.delete([chunk_id])
        return record


def _memory_chunk(record: MemoryRecord) -> Chunk:
    document_id = hashlib.sha256(f"memory:{record.id}".encode()).hexdigest()
    locator = f"{record.id}:{record.content}:{record.evidence_ref}"
    return Chunk(
        id=hashlib.sha256(locator.encode()).hexdigest(),
        document_id=document_id,
        source=f"memory:{record.id}",
        content=record.content,
        kind="approved_memory",
    )
