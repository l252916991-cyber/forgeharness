"""Inline and Redis/ARQ ingestion queues behind one small protocol."""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any, cast

from forgeharness.knowledge.models import IngestJob, JobStatus
from forgeharness.knowledge.protocols import DocumentStore, JobStore
from forgeharness.knowledge.service import KnowledgeService


class DocumentIngestionProcessor:
    """Execute one durable job from application-owned document bytes."""

    def __init__(
        self, *, jobs: JobStore, documents: DocumentStore, knowledge: KnowledgeService
    ) -> None:
        self._jobs = jobs
        self._documents = documents
        self._knowledge = knowledge

    async def process(self, job_id: str) -> None:
        job = self._jobs.get_job(job_id)
        if job is None:
            raise ValueError(f"unknown ingestion job: {job_id}")
        if job.status == JobStatus.SUCCEEDED:
            return
        self._jobs.update_job(job.id, status=JobStatus.RUNNING)
        try:
            loaded = self._documents.get(job.document_id)
            if loaded is None:
                self._jobs.update_job(
                    job.id, status=JobStatus.FAILED, error="document bytes are missing"
                )
                return
            document, data = loaded
            await self._knowledge.ingest(
                filename=document.source,
                data=data,
                mime_type=document.mime_type,
            )
        except Exception as exc:
            self._jobs.update_job(
                job.id,
                status=JobStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}"[:2_000],
            )
            return
        self._jobs.update_job(job.id, status=JobStatus.SUCCEEDED)


class InlineJobQueue:
    """Deterministic no-Redis queue for tests and the beginner profile."""

    def __init__(self, handler: Callable[[str], Awaitable[None]]) -> None:
        self._handler = handler

    async def submit(self, job: IngestJob) -> None:
        await self._handler(job.id)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class ARQJobQueue:
    """Redis-backed ARQ producer; the worker reconstructs dependencies independently."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._pool: Any | None = None

    async def submit(self, job: IngestJob) -> None:
        pool = await self._get_pool()
        await pool.enqueue_job("ingest_document", job.id, _job_id=job.id)

    async def ping(self) -> bool:
        pool = await self._get_pool()
        return bool(await pool.ping())

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                connections = cast(Any, importlib.import_module("arq.connections"))
            except ImportError as exc:
                raise RuntimeError("Redis queue requires the 'platform' dependencies") from exc
            settings = connections.RedisSettings.from_dsn(self._redis_url)
            self._pool = await connections.create_pool(settings)
        return self._pool
