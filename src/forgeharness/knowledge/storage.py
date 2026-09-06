"""Durable SQLite application stores for the keyless and teaching profiles."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from forgeharness.knowledge.models import (
    ChatMessage,
    ContentPart,
    Document,
    IngestJob,
    JobStatus,
    Session,
)
from forgeharness.state.sqlite import connect_wal


class SQLiteApplicationStore:
    """Own document, job, session, and message tables in one local database."""

    def __init__(self, path: Path, media_dir: Path) -> None:
        self._path = path
        self._media_dir = media_dir
        path.parent.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_jobs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES ingest_jobs(id) ON DELETE CASCADE
                );
                INSERT OR IGNORE INTO ingest_idempotency_keys(
                    idempotency_key, document_id, job_id
                )
                SELECT idempotency_key, document_id, id FROM ingest_jobs;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    parts_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS chat_messages_session_created
                ON chat_messages(session_id, created_at);
                CREATE TABLE IF NOT EXISTS session_workspaces (
                    session_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def put(self, document: Document, data: bytes) -> bool:
        destination = self._media_dir / document.id
        if not destination.exists():
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self._media_dir, prefix="upload-", delete=False
                ) as handle:
                    temporary = handle.name
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO documents(id, source, mime_type, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.source,
                    document.mime_type,
                    document.size_bytes,
                    document.created_at.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def get(self, document_id: str) -> tuple[Document, bytes] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, source, mime_type, size_bytes, created_at
                FROM documents WHERE id = ?
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        path = self._media_dir / document_id
        if not path.is_file():
            raise RuntimeError(f"document metadata exists without media bytes: {document_id}")
        return (
            Document(
                id=row[0],
                source=row[1],
                mime_type=row[2],
                size_bytes=row[3],
                created_at=datetime.fromisoformat(row[4]),
            ),
            path.read_bytes(),
        )

    def create(self) -> Session:
        session = Session()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions(id, created_at) VALUES (?, ?)",
                (session.id, session.created_at.isoformat()),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, created_at FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return (
            None if row is None else Session(id=row[0], created_at=datetime.fromisoformat(row[1]))
        )

    def add_message(self, message: ChatMessage) -> None:
        if self.get_session(message.session_id) is None:
            raise ValueError(f"unknown session: {message.session_id}")
        parts_json = "[" + ",".join(part.model_dump_json() for part in message.parts) + "]"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_messages(id, session_id, role, parts_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.session_id,
                    message.role,
                    parts_json,
                    message.created_at.isoformat(),
                ),
            )

    def messages(self, session_id: str, *, limit: int = 20) -> tuple[ChatMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("message limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, role, parts_json, created_at
                FROM chat_messages WHERE session_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        import json

        parsed = [
            ChatMessage(
                id=row[0],
                session_id=row[1],
                role=row[2],
                parts=tuple(ContentPart.model_validate(item) for item in json.loads(row[3])),
                created_at=datetime.fromisoformat(row[4]),
            )
            for row in reversed(rows)
        ]
        return tuple(parsed)

    def create_job(self, job: IngestJob, *, idempotency_key: str) -> IngestJob:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency key must contain 1-200 characters")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT jobs.id, jobs.document_id, jobs.status, jobs.error,
                       jobs.created_at, jobs.updated_at
                FROM ingest_idempotency_keys keys
                JOIN ingest_jobs jobs ON jobs.id = keys.job_id
                WHERE keys.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing[1] != job.document_id:
                    raise ValueError("idempotency key was already used for different content")
                return _job_from_row(existing)
            content_job = connection.execute(
                """
                SELECT id, document_id, status, error, created_at, updated_at
                FROM ingest_jobs WHERE document_id = ?
                ORDER BY created_at ASC, id ASC LIMIT 1
                """,
                (job.document_id,),
            ).fetchone()
            if content_job is not None:
                connection.execute(
                    """
                    INSERT INTO ingest_idempotency_keys(idempotency_key, document_id, job_id)
                    VALUES (?, ?, ?)
                    """,
                    (idempotency_key, job.document_id, content_job[0]),
                )
                return _job_from_row(content_job)
            connection.execute(
                """
                INSERT INTO ingest_jobs(
                    id, document_id, idempotency_key, status, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.document_id,
                    idempotency_key,
                    job.status.value,
                    job.error,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO ingest_idempotency_keys(idempotency_key, document_id, job_id)
                VALUES (?, ?, ?)
                """,
                (idempotency_key, job.document_id, job.id),
            )
        return job

    def get_job(self, job_id: str) -> IngestJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, document_id, status, error, created_at, updated_at
                FROM ingest_jobs WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        return None if row is None else _job_from_row(row)

    def update_job(self, job_id: str, *, status: JobStatus, error: str | None = None) -> IngestJob:
        updated_at = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE ingest_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status.value, error, updated_at.isoformat(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown ingestion job: {job_id}")
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError("updated ingestion job disappeared")
        return job

    def bind_workspace(self, session_id: str, workspace: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_workspaces(session_id, workspace, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET workspace = excluded.workspace
                """,
                (session_id, str(workspace), datetime.now(UTC).isoformat()),
            )

    def get_workspace(self, session_id: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workspace FROM session_workspaces WHERE session_id = ?", (session_id,)
            ).fetchone()
        return None if row is None else Path(row[0])

    def _connect(self) -> sqlite3.Connection:
        return connect_wal(self._path, foreign_keys=True)

    def ping(self) -> bool:
        with self._connect() as connection:
            return bool(connection.execute("SELECT 1").fetchone()[0])


class SQLiteSessionStore:
    """Narrow SessionStore view over SQLiteApplicationStore."""

    def __init__(self, store: SQLiteApplicationStore) -> None:
        self._store = store

    async def create(self) -> Session:
        return await asyncio.to_thread(self._store.create)

    async def get(self, session_id: str) -> Session | None:
        return await asyncio.to_thread(self._store.get_session, session_id)

    async def add_message(self, message: ChatMessage) -> None:
        await asyncio.to_thread(self._store.add_message, message)

    async def messages(self, session_id: str, *, limit: int = 20) -> tuple[ChatMessage, ...]:
        return await asyncio.to_thread(self._store.messages, session_id, limit=limit)

    async def ping(self) -> bool:
        return await asyncio.to_thread(self._store.ping)


class PostgresSessionStore:
    """Async PostgreSQL session store with the same application protocol as SQLite."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any | None = None
        self._init_lock = asyncio.Lock()

    async def create(self) -> Session:
        session = Session()
        pool = await self._get_pool()
        await pool.execute(
            "INSERT INTO sessions(id, created_at) VALUES($1, $2)",
            session.id,
            session.created_at,
        )
        return session

    async def get(self, session_id: str) -> Session | None:
        pool = await self._get_pool()
        row = await pool.fetchrow("SELECT id, created_at FROM sessions WHERE id = $1", session_id)
        return None if row is None else Session(id=row["id"], created_at=row["created_at"])

    async def add_message(self, message: ChatMessage) -> None:
        pool = await self._get_pool()
        try:
            await pool.execute(
                """
                INSERT INTO chat_messages(id, session_id, role, parts_json, created_at)
                VALUES($1, $2, $3, $4::jsonb, $5)
                """,
                message.id,
                message.session_id,
                message.role,
                json.dumps([part.model_dump(mode="json") for part in message.parts]),
                message.created_at,
            )
        except Exception as exc:
            if exc.__class__.__name__ == "ForeignKeyViolationError":
                raise ValueError(f"unknown session: {message.session_id}") from exc
            raise

    async def messages(self, session_id: str, *, limit: int = 20) -> tuple[ChatMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("message limit must be between 1 and 100")
        pool = await self._get_pool()
        rows = await pool.fetch(
            """
            SELECT id, session_id, role, parts_json, created_at
            FROM (
                SELECT id, session_id, role, parts_json, created_at
                FROM chat_messages WHERE session_id = $1
                ORDER BY created_at DESC, id DESC LIMIT $2
            ) recent ORDER BY created_at ASC, id ASC
            """,
            session_id,
            limit,
        )
        return tuple(
            ChatMessage(
                id=row["id"],
                session_id=row["session_id"],
                role=row["role"],
                parts=tuple(
                    ContentPart.model_validate(item) for item in _postgres_json(row["parts_json"])
                ),
                created_at=row["created_at"],
            )
            for row in rows
        )

    async def ping(self) -> bool:
        pool = await self._get_pool()
        return bool(await pool.fetchval("SELECT TRUE"))

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._init_lock:
            if self._pool is None:
                try:
                    asyncpg = cast(Any, importlib.import_module("asyncpg"))
                except ImportError as exc:
                    raise RuntimeError(
                        "PostgreSQL mode requires the 'platform' dependencies"
                    ) from exc
                pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
                await pool.execute(_POSTGRES_SESSION_SCHEMA)
                self._pool = pool
        if self._pool is None:
            raise RuntimeError("PostgreSQL pool initialization failed")
        return self._pool


def _postgres_json(value: object) -> list[object]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list):
        raise RuntimeError("stored message parts must be a JSON array")
    return parsed


_POSTGRES_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    parts_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_session_created
ON chat_messages(session_id, created_at, id);
"""


def _job_from_row(row: tuple[str, str, str, str | None, str, str]) -> IngestJob:
    return IngestJob(
        id=row[0],
        document_id=row[1],
        status=JobStatus(row[2]),
        error=row[3],
        created_at=datetime.fromisoformat(row[4]),
        updated_at=datetime.fromisoformat(row[5]),
    )
