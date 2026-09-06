"""Tests for review-only multi-agent boundaries and durable ingestion jobs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forgeharness.knowledge.application import build_knowledge_application
from forgeharness.knowledge.models import Citation, IngestJob, JobStatus
from forgeharness.knowledge.queue import DocumentIngestionProcessor
from forgeharness.knowledge.reviewer import CitationReviewer, ReviewVerdict
from forgeharness.knowledge.testing import DeterministicChatModel


async def test_reviewer_accepts_bound_citations_and_rejects_tampering(tmp_path: Path) -> None:
    application = build_knowledge_application(tmp_path)
    await application.knowledge.ingest(
        filename="guide.md", data=b"# Approval\nExact arguments require human approval."
    )
    report = await application.knowledge.search("human approval")
    chunk = report.final[0].chunk
    reviewer = CitationReviewer(DeterministicChatModel())
    valid = Citation(
        chunk_id=chunk.id,
        source=chunk.source,
        quote=chunk.content,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        page=chunk.page,
    )
    accepted = await reviewer.review(
        question="How is approval handled?",
        answer="It requires human approval.",
        citations=(valid,),
        report=report,
    )
    assert accepted.verdict == ReviewVerdict.ACCEPT
    rejected = await reviewer.review(
        question="How is approval handled?",
        answer="It is automatic.",
        citations=(valid.model_copy(update={"source": "invented.md"}),),
        report=report,
    )
    assert rejected.verdict == ReviewVerdict.REJECT
    await application.close()


async def test_ingestion_processor_is_idempotent_and_records_failure(tmp_path: Path) -> None:
    application = build_knowledge_application(tmp_path)
    document, _ = application.parser.parse(filename="guide.md", data=b"agent approval")
    application.store.put(document, b"agent approval")
    job = application.store.create_job(IngestJob(document_id=document.id), idempotency_key="one")
    processor = DocumentIngestionProcessor(
        jobs=application.store,
        documents=application.store,
        knowledge=application.knowledge,
    )
    await processor.process(job.id)
    await processor.process(job.id)
    completed = application.store.get_job(job.id)
    assert completed is not None and completed.status == JobStatus.SUCCEEDED

    missing = application.store.create_job(
        IngestJob(document_id="0" * 64), idempotency_key="missing"
    )
    await processor.process(missing.id)
    failed = application.store.get_job(missing.id)
    assert failed is not None and failed.status == JobStatus.FAILED
    assert failed.error == "document bytes are missing"
    await application.close()


async def test_arq_worker_retries_failed_jobs_and_allows_later_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arq = pytest.importorskip("arq")
    from forgeharness.worker import WorkerSettings, ingest_document, shutdown, startup

    monkeypatch.setenv("FORGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FORGE_ENABLE_OMLX", "false")
    for name in ("FORGE_REDIS_URL", "FORGE_POSTGRES_DSN", "FORGE_QDRANT_URL"):
        monkeypatch.delenv(name, raising=False)
    ctx: dict[str, Any] = {"job_try": 2}
    await startup(ctx)
    application = ctx["application"]
    document, _ = application.parser.parse(filename="retry.md", data=b"agent approval")
    job = application.store.create_job(IngestJob(document_id=document.id), idempotency_key="retry")
    with pytest.raises(arq.Retry):
        await ingest_document(ctx, job.id)
    assert application.store.get_job(job.id).status == JobStatus.FAILED
    # Repair the transient failure; application idempotency remains the same.
    application.store.put(document, b"agent approval")
    await ingest_document(ctx, job.id)
    await ingest_document(ctx, job.id)
    assert application.store.get_job(job.id).status == JobStatus.SUCCEEDED
    assert WorkerSettings.keep_result == 0
    with pytest.raises(RuntimeError, match="processor is not initialized"):
        await ingest_document({}, job.id)
    with pytest.raises(RuntimeError, match="application is not initialized"):
        await ingest_document({"processor": ctx["processor"]}, job.id)
    await shutdown(ctx)
    await shutdown({})


async def test_processor_records_storage_errors_and_can_replay_interrupted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = build_knowledge_application(tmp_path)
    document, _ = application.parser.parse(filename="job.txt", data=b"job evidence")
    application.store.put(document, b"job evidence")
    job = application.store.create_job(IngestJob(document_id=document.id), idempotency_key="retry")
    processor = DocumentIngestionProcessor(
        jobs=application.store, documents=application.store, knowledge=application.knowledge
    )
    original_get = application.store.get

    def broken_get(document_id: str) -> None:
        raise OSError("media unavailable")

    monkeypatch.setattr(application.store, "get", broken_get)
    await processor.process(job.id)
    assert application.store.get_job(job.id).status == JobStatus.FAILED  # type: ignore[union-attr]
    monkeypatch.setattr(application.store, "get", original_get)
    original_ingest = application.knowledge.ingest
    entered = asyncio.Event()

    async def interrupted(**kwargs: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(application.knowledge, "ingest", interrupted)
    task = asyncio.create_task(processor.process(job.id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert application.store.get_job(job.id).status == JobStatus.RUNNING  # type: ignore[union-attr]
    monkeypatch.setattr(application.knowledge, "ingest", original_ingest)
    await processor.process(job.id)
    assert application.store.get_job(job.id).status == JobStatus.SUCCEEDED  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="unknown ingestion"):
        await processor.process("unknown")
    await application.close()


async def test_arq_producer_reuses_pool_and_application_job_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forgeharness.knowledge import queue

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class Pool:
        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            calls.append((args, kwargs))

        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            calls.append((("closed",), {}))

    async def connect(settings: str) -> Pool:
        assert settings == "redis://local-test"
        return Pool()

    connections = SimpleNamespace(
        create_pool=connect, RedisSettings=SimpleNamespace(from_dsn=lambda url: url)
    )
    monkeypatch.setattr(queue.importlib, "import_module", lambda name: connections)
    producer = queue.ARQJobQueue("redis://local-test")
    job = IngestJob(document_id="0" * 64)
    await producer.submit(job)
    await producer.submit(job)
    assert calls[0] == (("ingest_document", job.id), {"_job_id": job.id})
    assert calls[1] == calls[0]
    assert await producer.ping()
    await producer.close()
    await producer.close()

    def unavailable(name: str) -> None:
        raise ImportError("arq is optional")

    monkeypatch.setattr(queue.importlib, "import_module", unavailable)
    with pytest.raises(RuntimeError, match="platform"):
        await producer.ping()
