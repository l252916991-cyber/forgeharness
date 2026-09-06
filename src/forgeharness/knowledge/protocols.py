"""Framework-independent ports for the knowledge-agent application."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from forgeharness.knowledge.models import (
    AgentResponse,
    ChatMessage,
    Chunk,
    Document,
    IngestJob,
    Intent,
    JobStatus,
    RetrievalReport,
    SearchHit,
    Session,
)


class ChatModel(Protocol):
    """Generate application prose without owning workflow state."""

    @property
    def model_name(self) -> str: ...

    async def complete(self, *, system: str, user: str) -> str: ...

    async def describe_image(self, *, data: bytes, mime_type: str) -> str: ...


@runtime_checkable
class StructuredChatModel(Protocol):
    """Optional capability for provider-enforced JSON output."""

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> str: ...


class EmbeddingModel(Protocol):
    """Map text to a stable, model-identified vector space."""

    @property
    def model_name(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class Reranker(Protocol):
    """Reorder candidate text for one query."""

    @property
    def model_name(self) -> str: ...

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> tuple[tuple[int, float], ...]: ...


class DocumentParser(Protocol):
    """Validate and split an immutable upload."""

    def parse(
        self, *, filename: str, data: bytes, mime_type: str | None = None
    ) -> tuple[Document, tuple[Chunk, ...]]: ...


class VectorStore(Protocol):
    """Persist and query one embedding space."""

    async def ensure_space(self, *, model_name: str, dimension: int) -> None: ...

    async def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None: ...

    async def search(self, vector: Sequence[float], *, limit: int) -> tuple[SearchHit, ...]: ...

    async def delete(self, chunk_ids: Sequence[str]) -> None: ...

    async def ping(self) -> bool: ...


class LexicalIndex(Protocol):
    """Index and search literal source text."""

    def upsert(self, chunks: Sequence[Chunk]) -> None: ...

    def search(self, query: str, *, limit: int) -> tuple[SearchHit, ...]: ...

    def delete(self, chunk_ids: Sequence[str]) -> None: ...

    def document_chunks(self, document_id: str, *, limit: int) -> tuple[Chunk, ...]: ...


class SessionStore(Protocol):
    """Persist isolated application conversations."""

    async def create(self) -> Session: ...

    async def get(self, session_id: str) -> Session | None: ...

    async def add_message(self, message: ChatMessage) -> None: ...

    async def messages(self, session_id: str, *, limit: int = 20) -> tuple[ChatMessage, ...]: ...

    async def ping(self) -> bool: ...


class DocumentStore(Protocol):
    """Persist source metadata and application-owned bytes by digest."""

    def put(self, document: Document, data: bytes) -> bool: ...

    def get(self, document_id: str) -> tuple[Document, bytes] | None: ...


class JobStore(Protocol):
    """Persist ingestion status and request idempotency."""

    def create_job(self, job: IngestJob, *, idempotency_key: str) -> IngestJob: ...

    def get_job(self, job_id: str) -> IngestJob | None: ...

    def update_job(
        self, job_id: str, *, status: JobStatus, error: str | None = None
    ) -> IngestJob: ...


class JobQueue(Protocol):
    """Submit document work without coupling the API to a queue library."""

    async def submit(self, job: IngestJob) -> None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class IntentRouter(Protocol):
    """Choose one explicit workflow for user-controlled text."""

    async def classify(self, text: str) -> Intent: ...


class Retriever(Protocol):
    """Return inspectable evidence from all configured indexes."""

    async def retrieve(self, query: str, *, limit: int = 5) -> RetrievalReport: ...


class CodingTaskHandler(Protocol):
    """Route coding work to the approval-gated Harness vertical."""

    async def handle(self, *, session_id: str, task: str) -> AgentResponse: ...
