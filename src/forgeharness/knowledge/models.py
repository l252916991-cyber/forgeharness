"""Validated contracts for documents, retrieval, conversations, and jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from forgeharness.domain.models import FrozenModel


class Intent(StrEnum):
    """Application workflows selected before the Harness executes."""

    KNOWLEDGE_QUERY = "knowledge_query"
    CODING_TASK = "coding_task"
    MEMORY_COMMAND = "memory_command"
    GENERAL_CHAT = "general_chat"


class ContentPartType(StrEnum):
    """User-visible input kinds accepted by the local application."""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"


class ContentPart(FrozenModel):
    """A safe reference to text or application-owned uploaded content."""

    type: ContentPartType
    text: str | None = Field(default=None, max_length=50_000)
    media_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    mime_type: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_payload(self) -> ContentPart:
        if self.type == ContentPartType.TEXT:
            if not self.text or self.media_id is not None:
                raise ValueError("text parts require text and cannot reference media")
        elif self.media_id is None or self.text is not None:
            raise ValueError("media parts require media_id and cannot contain text")
        return self


class Document(FrozenModel):
    """One immutable uploaded source identified by its content digest."""

    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(min_length=1, max_length=500)
    mime_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=1, le=10 * 1024 * 1024)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Chunk(FrozenModel):
    """A citable unit produced by deterministic document parsing."""

    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=20_000)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    page: int | None = Field(default=None, ge=1)
    kind: str = Field(default="text", pattern=r"^[a-z][a-z0-9_.-]{0,31}$")

    @model_validator(mode="after")
    def validate_location(self) -> Chunk:
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError("chunk end_line cannot precede start_line")
        return self


class SearchHit(FrozenModel):
    """A ranked chunk with stage-specific evidence."""

    chunk: Chunk
    score: float
    rank: int = Field(ge=1)
    stages: tuple[str, ...] = ()


class Citation(FrozenModel):
    """A stable source location returned separately from generated prose."""

    chunk_id: str
    source: str
    quote: str = Field(min_length=1, max_length=1_000)
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None


class RetrievalReport(FrozenModel):
    """Inspectable output for every retrieval stage."""

    query: str
    lexical: tuple[SearchHit, ...] = ()
    vector: tuple[SearchHit, ...] = ()
    fused: tuple[SearchHit, ...] = ()
    final: tuple[SearchHit, ...] = ()
    latency_ms: float = Field(ge=0)
    timings_ms: dict[str, float] = Field(default_factory=dict)


class Session(FrozenModel):
    """A durable application conversation."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChatMessage(FrozenModel):
    """One durable application message."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    role: Literal["user", "assistant"]
    parts: tuple[ContentPart, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentResponse(FrozenModel):
    """Stable response contract shared by API, UI, and evaluation."""

    intent: Intent
    answer: str
    citations: tuple[Citation, ...] = ()
    run_id: str
    trace_id: str
    latency_ms: float = Field(ge=0)
    model: str
    checkpoint_revision: int = Field(default=0, ge=0)


class JobStatus(StrEnum):
    """Lifecycle for asynchronous ingestion."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IngestJob(FrozenModel):
    """Durable status for an idempotent document-ingestion request."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    document_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: JobStatus = JobStatus.QUEUED
    error: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ModelCapability(FrozenModel):
    """Observed OMLX model inventory used by readiness checks."""

    model_id: str
    role: Literal["chat", "embedding", "reranker"]
    available: bool


class EvaluationCase(FrozenModel):
    """One immutable labeled retrieval example."""

    id: str
    query: str
    relevant_document_ids: tuple[str, ...]
    answerable: bool = True


class EvaluationResult(FrozenModel):
    """Aggregate retrieval evidence tied to a fixed case set."""

    total: int = Field(ge=0)
    recall_at_5: float = Field(ge=0, le=1)
    mrr_at_10: float = Field(ge=0, le=1)
    citation_precision: float = Field(ge=0, le=1)
    unsupported_answer_rate: float = Field(ge=0, le=1)
