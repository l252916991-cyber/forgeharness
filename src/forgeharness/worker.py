"""ARQ worker entry point for durable document ingestion."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar

from arq import Retry
from arq.connections import RedisSettings

from forgeharness.knowledge.application import KnowledgeApplication, build_knowledge_application
from forgeharness.knowledge.models import JobStatus
from forgeharness.knowledge.queue import DocumentIngestionProcessor


async def startup(ctx: dict[str, Any]) -> None:
    """Build worker-owned connections instead of sharing API process state."""
    configured_path = Path(os.getenv("FORGE_DATA_DIR", ".forgeharness"))
    data_dir = await asyncio.to_thread(configured_path.resolve)
    application = build_knowledge_application(data_dir)
    ctx["application"] = application
    ctx["processor"] = DocumentIngestionProcessor(
        jobs=application.store,
        documents=application.store,
        knowledge=application.knowledge,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    application = ctx.get("application")
    if application is not None:
        await application.close()


async def ingest_document(ctx: dict[str, Any], job_id: str) -> None:
    """ARQ function name is a stable external queue contract."""
    processor = ctx.get("processor")
    if not isinstance(processor, DocumentIngestionProcessor):
        raise RuntimeError("worker ingestion processor is not initialized")
    await processor.process(job_id)
    application = ctx.get("application")
    if not isinstance(application, KnowledgeApplication):
        raise RuntimeError("worker application is not initialized")
    job = application.store.get_job(job_id)
    if job is not None and job.status == JobStatus.FAILED:
        # The processor persists diagnostics; ARQ owns bounded scheduling retries.
        raise Retry(defer=min(2 ** int(ctx.get("job_try", 1)), 30))


class WorkerSettings:
    """Configuration discovered by ``arq forgeharness.worker.WorkerSettings``."""

    functions: ClassVar[list[Any]] = [ingest_document]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = int(os.getenv("FORGE_WORKER_CONCURRENCY", "2"))
    job_timeout = int(os.getenv("FORGE_WORKER_TIMEOUT_SECONDS", "600"))
    max_tries = int(os.getenv("FORGE_WORKER_MAX_TRIES", "3"))
    # Durable outcomes live in JobStore; a Redis result key must not block a
    # later manual retry of the same application-level idempotent job id.
    keep_result = 0
    redis_settings = RedisSettings.from_dsn(
        os.getenv("FORGE_REDIS_URL", "redis://127.0.0.1:6379/0")
    )
