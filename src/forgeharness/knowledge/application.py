"""Composition root for keyless and OMLX-backed knowledge applications."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from forgeharness.coding.api import APICodingHandler
from forgeharness.knowledge.conversation import (
    ConversationService,
    ModelIntentRouter,
    RuleIntentRouter,
)
from forgeharness.knowledge.indexes import (
    HybridRetriever,
    InMemoryVectorStore,
    QdrantVectorStore,
    SQLiteLexicalIndex,
)
from forgeharness.knowledge.memory import SemanticMemoryService
from forgeharness.knowledge.parsers import LocalDocumentParser
from forgeharness.knowledge.protocols import (
    ChatModel,
    EmbeddingModel,
    IntentRouter,
    JobQueue,
    Reranker,
    SessionStore,
    VectorStore,
)
from forgeharness.knowledge.queue import ARQJobQueue, DocumentIngestionProcessor, InlineJobQueue
from forgeharness.knowledge.reviewer import CitationReviewer
from forgeharness.knowledge.runs import KnowledgeRunStore
from forgeharness.knowledge.service import KnowledgeService
from forgeharness.knowledge.storage import (
    PostgresSessionStore,
    SQLiteApplicationStore,
    SQLiteSessionStore,
)
from forgeharness.knowledge.testing import (
    DeterministicChatModel,
    DeterministicEmbeddingModel,
    DeterministicReranker,
)
from forgeharness.models.omlx import (
    OMLXChatModel,
    OMLXClient,
    OMLXConfig,
    OMLXEmbeddingModel,
    OMLXReranker,
)
from forgeharness.state.memory import SQLiteMemoryStore


class KnowledgeSettings(BaseSettings):
    """Environment-selectable dependencies with a safe keyless default."""

    model_config = SettingsConfigDict(env_prefix="FORGE_", extra="ignore")

    enable_omlx: bool = False
    omlx_base_url: str = "http://127.0.0.1:8000/v1"
    omlx_api_key: str | None = None
    chat_model: str = "Qwen3.5-9B-4bit"
    embedding_model: str = "Qwen3-Embedding-4B-4bit-DWQ"
    reranker_model: str = "bge-reranker-v2-m3-mlx"
    model_timeout_seconds: float = Field(default=120, gt=0, le=600)
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    postgres_dsn: str | None = None
    redis_url: str | None = None
    enable_reviewer: bool = False
    coding_workspace_root: Path | None = None


@dataclass(frozen=True)
class KnowledgeApplication:
    """Explicit dependency graph exposed to API and worker surfaces."""

    store: SQLiteApplicationStore
    runs: KnowledgeRunStore
    sessions: SessionStore
    memories: SQLiteMemoryStore
    semantic_memories: SemanticMemoryService
    parser: LocalDocumentParser
    knowledge: KnowledgeService
    conversations: ConversationService
    omlx: OMLXClient | None
    vector: VectorStore
    memory_vector: VectorStore
    queue: JobQueue
    coding: APICodingHandler | None
    mode: str

    async def close(self) -> None:
        if self.omlx is not None:
            await self.omlx.close()
        await self.queue.close()
        if self.coding is not None:
            await self.coding.close()
        session_close = getattr(self.sessions, "close", None)
        if session_close is not None:
            await session_close()
        close = getattr(self.vector, "close", None)
        if close is not None:
            await close()
        memory_close = getattr(self.memory_vector, "close", None)
        if memory_close is not None:
            await memory_close()


def build_knowledge_application(
    data_dir: Path, settings: KnowledgeSettings | None = None
) -> KnowledgeApplication:
    """Build one deterministic or local-model application without global state."""
    configured = settings or KnowledgeSettings()
    store = SQLiteApplicationStore(data_dir / "knowledge.sqlite3", data_dir / "media")
    runs = KnowledgeRunStore(data_dir / "knowledge.sqlite3", data_dir / "traces")
    sessions: SessionStore = (
        PostgresSessionStore(configured.postgres_dsn)
        if configured.postgres_dsn
        else SQLiteSessionStore(store)
    )
    memories = SQLiteMemoryStore(data_dir / "memories.sqlite3")
    parser = LocalDocumentParser()
    lexical = SQLiteLexicalIndex(data_dir / "knowledge-fts.sqlite3")
    memory_lexical = SQLiteLexicalIndex(data_dir / "memory-fts.sqlite3")
    vector: VectorStore
    memory_vector: VectorStore
    if configured.qdrant_url:
        vector = QdrantVectorStore(
            base_url=configured.qdrant_url,
            api_key=configured.qdrant_api_key,
        )
        memory_vector = QdrantVectorStore(
            base_url=configured.qdrant_url,
            collection="forgeharness_memories",
            api_key=configured.qdrant_api_key,
        )
    else:
        vector = InMemoryVectorStore()
        memory_vector = InMemoryVectorStore()
    omlx: OMLXClient | None = None
    chat: ChatModel
    embedding: EmbeddingModel
    reranker: Reranker
    router: IntentRouter
    if configured.enable_omlx:
        omlx = OMLXClient(
            OMLXConfig(
                base_url=configured.omlx_base_url,
                api_key=configured.omlx_api_key,
                chat_model=configured.chat_model,
                embedding_model=configured.embedding_model,
                reranker_model=configured.reranker_model,
                timeout_seconds=configured.model_timeout_seconds,
            )
        )
        chat = OMLXChatModel(omlx)
        embedding = OMLXEmbeddingModel(omlx)
        reranker = OMLXReranker(omlx)
        router = ModelIntentRouter(chat, RuleIntentRouter())
        mode = "omlx"
    else:
        chat = DeterministicChatModel()
        embedding = DeterministicEmbeddingModel()
        reranker = DeterministicReranker()
        router = RuleIntentRouter()
        mode = "keyless"
    retriever = HybridRetriever(
        lexical=lexical,
        vector=vector,
        embedding=embedding,
        reranker=reranker,
    )
    memory_retriever = HybridRetriever(
        lexical=memory_lexical,
        vector=memory_vector,
        embedding=embedding,
        reranker=reranker,
    )
    semantic_memories = SemanticMemoryService(
        records=memories,
        lexical=memory_lexical,
        vector=memory_vector,
        embedding=embedding,
        retriever=memory_retriever,
    )
    knowledge = KnowledgeService(
        parser=parser,
        documents=store,
        lexical=lexical,
        vector=vector,
        embedding=embedding,
        retriever=retriever,
        chat=chat,
        memory_retriever=semantic_memories,
        reviewer=CitationReviewer(chat) if configured.enable_reviewer else None,
        runs=runs,
    )
    processor = DocumentIngestionProcessor(
        jobs=store,
        documents=store,
        knowledge=knowledge,
    )
    queue: JobQueue = (
        ARQJobQueue(configured.redis_url)
        if configured.redis_url
        else InlineJobQueue(processor.process)
    )
    coding = (
        APICodingHandler(
            data_dir=data_dir,
            bindings=store,
            base_url=configured.omlx_base_url,
            model_name=configured.chat_model,
            api_key=configured.omlx_api_key,
            timeout_seconds=configured.model_timeout_seconds,
        )
        if configured.enable_omlx and configured.coding_workspace_root is not None
        else None
    )
    conversations = ConversationService(
        sessions=sessions,
        router=router,
        knowledge=knowledge,
        chat=chat,
        memories=memories,
        coding=coding,
    )
    return KnowledgeApplication(
        store=store,
        runs=runs,
        sessions=sessions,
        memories=memories,
        semantic_memories=semantic_memories,
        parser=parser,
        knowledge=knowledge,
        conversations=conversations,
        omlx=omlx,
        vector=vector,
        memory_vector=memory_vector,
        queue=queue,
        coding=coding,
        mode=mode,
    )
