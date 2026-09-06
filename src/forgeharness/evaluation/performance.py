"""Local deterministic retrieval benchmark with a fixed 5,000-chunk corpus."""

from __future__ import annotations

import asyncio
import hashlib
import statistics
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.knowledge.indexes import HybridRetriever, InMemoryVectorStore, SQLiteLexicalIndex
from forgeharness.knowledge.models import Chunk
from forgeharness.knowledge.testing import DeterministicEmbeddingModel, DeterministicReranker


class PerformanceReport(FrozenModel):
    created_at: datetime
    revision: str
    chunks: int
    requests: int
    concurrency: int
    warmup_requests: int
    retrieval_p50_ms: float = Field(ge=0)
    retrieval_p95_ms: float = Field(ge=0)
    target_p95_ms: float = 500
    qualified: bool


async def run_retrieval_benchmark(
    *, output_path: Path, project_root: Path, chunk_count: int = 5_000
) -> PerformanceReport:
    embedding = DeterministicEmbeddingModel()
    reranker = DeterministicReranker()
    vector = InMemoryVectorStore()
    with tempfile.TemporaryDirectory(prefix="forgeharness-bench-") as temporary:
        lexical = SQLiteLexicalIndex(Path(temporary) / "fts.sqlite3")
        chunks = tuple(_benchmark_chunk(index) for index in range(chunk_count))
        vectors = await embedding.embed([chunk.content for chunk in chunks])
        await vector.ensure_space(model_name=embedding.model_name, dimension=len(vectors[0]))
        lexical.upsert(chunks)
        await vector.upsert(chunks, vectors)
        retriever = HybridRetriever(
            lexical=lexical,
            vector=vector,
            embedding=embedding,
            reranker=reranker,
        )
        for index in range(10):
            await retriever.retrieve(f"benchmark_token_{index}")
        latencies = await _concurrent_queries(retriever, request_count=100, concurrency=10)
    p50 = statistics.median(latencies)
    p95 = _percentile(latencies, 0.95)
    report = PerformanceReport(
        created_at=datetime.now(UTC),
        revision=_revision(project_root),
        chunks=chunk_count,
        requests=len(latencies),
        concurrency=10,
        warmup_requests=10,
        retrieval_p50_ms=p50,
        retrieval_p95_ms=p95,
        qualified=chunk_count >= 5_000 and p95 < 500,
    )
    await asyncio.to_thread(_write_report, output_path, report)
    return report


async def _concurrent_queries(
    retriever: HybridRetriever, *, request_count: int, concurrency: int
) -> list[float]:
    semaphore = asyncio.Semaphore(concurrency)

    async def one(index: int) -> float:
        async with semaphore:
            started = time.perf_counter()
            await retriever.retrieve(f"benchmark_token_{index % 100}")
            return (time.perf_counter() - started) * 1_000

    return list(await asyncio.gather(*(one(index) for index in range(request_count))))


def _benchmark_chunk(index: int) -> Chunk:
    content = (
        f"benchmark_token_{index} document chunk {index} describes agent approval, "
        "context budgets, retrieval, checkpoints, and trace verification."
    )
    document_id = hashlib.sha256(f"document:{index}".encode()).hexdigest()
    return Chunk(
        id=hashlib.sha256(content.encode()).hexdigest(),
        document_id=document_id,
        source=f"benchmark-{index}.md",
        content=content,
        start_line=1,
        end_line=1,
    )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * quantile) - 1))
    return ordered[index]


def _revision(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _write_report(path: Path, report: PerformanceReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
