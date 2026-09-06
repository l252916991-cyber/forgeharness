"""LangChain-based RAG implementation over the ForgeHarness retrieval backend.

The hybrid retrieval (FTS5 + vector + RRF + rerank) stays native; this module
only re-exposes it through LangChain LCEL primitives so both implementations
can be compared on the same backend.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableLambda, RunnablePassthrough
from pydantic import SecretStr


@dataclass
class RetrievalResult:
    """Structured retrieval result with citations."""

    answer: str
    sources: list[str]
    chunks_used: int
    retrieval_time_ms: float
    generation_time_ms: float


class HybridRetriever(BaseRetriever):
    """Custom retriever wrapping the native ForgeHarness hybrid search.

    `knowledge_service` must be a declared field: BaseRetriever is a Pydantic
    model and rejects undeclared attribute assignment.
    """

    knowledge_service: Any

    def __init__(self, knowledge_service: Any, **kwargs: Any) -> None:
        super().__init__(knowledge_service=knowledge_service, **kwargs)  # type: ignore[call-arg]

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        """Retrieve documents using the native hybrid search + rerank pipeline."""
        report = await self.knowledge_service.search(query)
        return [
            Document(
                page_content=hit.chunk.content,
                metadata={
                    "source": hit.chunk.source,
                    "chunk_id": hit.chunk.id,
                    "score": hit.score,
                },
            )
            for hit in report.final
        ]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """Native retrieval is async-only; callers must use the async interface."""
        raise NotImplementedError("HybridRetriever supports only the async interface")


class LangChainRAGChain:
    """LCEL-composed RAG chain: retrieve -> format -> generate, with memory."""

    def __init__(
        self,
        llm: BaseChatModel,
        retriever: HybridRetriever,
        memory_window: int = 5,
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.memory_window = memory_window
        self.conversation_history: list[BaseMessage] = []
        self.application: Any = None  # set by the factory for lifecycle management
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a technical assistant answering questions based on retrieved "
                    "documentation.\n\nRULES:\n"
                    "1. Answer ONLY using information from the provided context\n"
                    '2. If the context is insufficient, say "I cannot answer based on the '
                    'available documentation"\n'
                    "3. Cite sources using [source: filename] format\n"
                    "4. Never fabricate information\n\nContext:\n{context}",
                ),
                MessagesPlaceholder(variable_name="history"),
                ("human", "{question}"),
            ]
        )
        self.generation = self.llm | StrOutputParser()
        self.chain = self._build_chain()

    def _build_chain(self) -> Runnable[Any, str]:
        """Declarative LCEL composition (used by invoke-style callers)."""

        async def retrieve(question: str) -> str:
            return self._format_docs(await self.retriever.ainvoke(question))

        return (
            {
                "context": RunnableLambda(retrieve),
                "question": RunnablePassthrough(),
                "history": lambda _: self.conversation_history[-self.memory_window :],
            }
            | self.prompt
            | self.generation
        )

    @staticmethod
    def _format_docs(docs: list[Document]) -> str:
        if not docs:
            return "No relevant documentation found."
        return "\n\n".join(
            f"[{i}] Source: {doc.metadata.get('source', 'unknown')}\n{doc.page_content}"
            for i, doc in enumerate(docs, 1)
        )

    async def ask(self, question: str) -> RetrievalResult:
        """Run one retrieval and one generation; no stage runs twice."""
        started = time.perf_counter()
        docs = await self.retriever.ainvoke(question)
        retrieval_ms = (time.perf_counter() - started) * 1_000

        gen_started = time.perf_counter()
        messages = self.prompt.format_messages(
            context=self._format_docs(docs),
            question=question,
            history=self.conversation_history[-self.memory_window :],
        )
        answer = await self.generation.ainvoke(messages)
        generation_ms = (time.perf_counter() - gen_started) * 1_000

        self.conversation_history.extend(
            [HumanMessage(content=question), AIMessage(content=answer)]
        )
        sources = list(dict.fromkeys(doc.metadata["source"] for doc in docs))

        return RetrievalResult(
            answer=answer,
            sources=sources,
            chunks_used=len(docs),
            retrieval_time_ms=retrieval_ms,
            generation_time_ms=generation_ms,
        )

    def clear_history(self) -> None:
        """Clear conversation memory."""
        self.conversation_history.clear()

    async def close(self) -> None:
        """Release the underlying knowledge application."""
        if self.application is not None:
            await self.application.close()
            self.application = None


async def create_langchain_rag_app(
    data_dir: Path,
    model_base_url: str = "http://127.0.0.1:8000/v1",
    model_name: str = "Qwythos-9B-v2-8bit-mlx",
) -> LangChainRAGChain:
    """Compose the LangChain RAG chain over the native knowledge service."""
    from langchain_openai import ChatOpenAI

    from forgeharness.knowledge.application import build_knowledge_application

    application = build_knowledge_application(data_dir)
    retriever = HybridRetriever(knowledge_service=application.knowledge)
    # OMLX is loopback-only; bypass desktop proxies exactly like the native adapter.
    # ChatOpenAI keeps separate clients for sync and async paths.
    llm = ChatOpenAI(
        base_url=model_base_url,
        model=model_name,
        temperature=0.1,
        api_key=SecretStr("not-needed"),
        http_client=httpx.Client(trust_env=False, timeout=120.0),
        http_async_client=httpx.AsyncClient(trust_env=False, timeout=120.0),
    )
    chain = LangChainRAGChain(llm=llm, retriever=retriever)
    chain.application = application
    return chain


async def demo() -> None:
    """Demonstrate the LangChain RAG chain."""
    chain = await create_langchain_rag_app(Path(".forgeharness"))
    try:
        result = await chain.ask("How does approval work?")
        print(result.answer)
        print(f"sources={result.sources} chunks={result.chunks_used}")
    finally:
        await chain.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(demo())
