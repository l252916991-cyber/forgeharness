"""Tests for the LangChain implementation layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("langchain_core")
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from forgeharness.langchain_impl.rag_chain import HybridRetriever, LangChainRAGChain


@dataclass
class _FakeHit:
    chunk: object
    score: float


@dataclass
class _FakeChunk:
    content: str
    source: str
    id: str


@dataclass
class _FakeReport:
    final: list[_FakeHit] = field(default_factory=list)


def _make_retriever_with_docs(docs: list[Document]) -> MagicMock:
    retriever = MagicMock()
    retriever.ainvoke = AsyncMock(return_value=docs)
    return retriever


class TestHybridRetriever:
    @pytest.mark.asyncio
    async def test_wraps_native_search_into_documents(self) -> None:
        service = MagicMock()
        service.search = AsyncMock(
            return_value=_FakeReport(
                final=[_FakeHit(_FakeChunk("content", "doc.md", "chunk-1"), 0.95)]
            )
        )
        retriever = HybridRetriever(knowledge_service=service)

        docs = await retriever.ainvoke("test query")

        assert len(docs) == 1
        assert docs[0].page_content == "content"
        assert docs[0].metadata["source"] == "doc.md"
        assert docs[0].metadata["score"] == 0.95
        service.search.assert_awaited_once_with("test query")

    def test_sync_interface_is_rejected(self) -> None:
        service = MagicMock()
        retriever = HybridRetriever(knowledge_service=service)
        with pytest.raises(NotImplementedError):
            retriever.invoke("q")


class TestLangChainRAGChain:
    def _chain(self, docs: list[Document]) -> tuple[LangChainRAGChain, MagicMock]:
        llm = FakeListChatModel(responses=["Mocked answer", "Second answer"])
        retriever = _make_retriever_with_docs(docs)
        return LangChainRAGChain(llm=llm, retriever=retriever), retriever

    @pytest.mark.asyncio
    async def test_ask_runs_one_retrieval_and_one_generation(self) -> None:
        docs = [Document(page_content="text", metadata={"source": "doc.md"})]
        chain, retriever = self._chain(docs)

        result = await chain.ask("What is ForgeHarness?")

        assert result.answer == "Mocked answer"
        assert result.sources == ["doc.md"]
        assert result.chunks_used == 1
        retriever.ainvoke.assert_awaited_once_with("What is ForgeHarness?")

    @pytest.mark.asyncio
    async def test_conversation_memory_accumulates_and_clears(self) -> None:
        docs = [Document(page_content="text", metadata={"source": "doc.md"})]
        chain, _ = self._chain(docs)

        await chain.ask("Question 1")
        await chain.ask("Question 2")

        assert len(chain.conversation_history) == 4
        assert isinstance(chain.conversation_history[0], HumanMessage)
        assert isinstance(chain.conversation_history[1], AIMessage)

        chain.clear_history()
        assert chain.conversation_history == []

    @pytest.mark.asyncio
    async def test_empty_retrieval_yields_sources_empty(self) -> None:
        chain, _ = self._chain([])
        result = await chain.ask("anything")
        assert result.sources == []
        assert result.chunks_used == 0

    @pytest.mark.asyncio
    async def test_close_releases_application_once(self) -> None:
        chain, _ = self._chain([])
        application = MagicMock()
        application.close = AsyncMock()
        chain.application = application

        await chain.close()
        await chain.close()

        application.close.assert_awaited_once()
        assert chain.application is None


class TestFrameworkBenchmark:
    @pytest.mark.asyncio
    async def test_comparison_records_skipped_langchain_arm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force the no-frameworks path so the LangChain arm is skipped, not fabricated."""
        import sys

        from forgeharness.langchain_impl import benchmark as bench

        class _FakeKnowledge:
            async def search(self, query: str) -> _FakeReport:
                return _FakeReport(final=[_FakeHit(_FakeChunk("c", "s.md", "1"), 0.5)])

        class _FakeApp:
            knowledge = _FakeKnowledge()

            async def close(self) -> None:
                return None

        def _fake_build(_data_dir: Path, *args: object, **kwargs: object) -> _FakeApp:
            return _FakeApp()

        import forgeharness.knowledge.application as app_module

        monkeypatch.setattr(app_module, "build_knowledge_application", _fake_build)
        monkeypatch.setitem(sys.modules, "forgeharness.langchain_impl.rag_chain", None)

        report = await bench.run_comparison(
            data_dir=tmp_path,
            output_path=tmp_path / "report.json",
            query_count=2,
            queries=["q1", "q2"],
        )

        assert report["native"] is not None
        assert report["native"]["count"] == 2
        assert report["langchain"]["skipped"] is True
        assert "frameworks extra not installed" in report["langchain"]["reason"]
        assert (tmp_path / "report.json").exists()
