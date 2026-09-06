"""Source-linked long-term lessons that require review before retrieval."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.state.sqlite import connect_wal


class MemoryStatus(StrEnum):
    """Review lifecycle for a proposed long-term lesson."""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELETED = "deleted"


class MemoryRecord(FrozenModel):
    """A lesson whose evidence remains inspectable."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    content: str = Field(min_length=1, max_length=10_000)
    evidence_ref: str = Field(min_length=1, max_length=2_000)
    tags: tuple[str, ...] = ()
    status: MemoryStatus = MemoryStatus.CANDIDATE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryStoreError(RuntimeError):
    """A requested memory transition is invalid or references an unknown record."""


class SQLiteMemoryStore:
    """Persist reviewable lessons and retrieve only approved content."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_indexes (
                    memory_id TEXT PRIMARY KEY,
                    indexed_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                )
                """
            )

    def propose(
        self, *, task_id: str, content: str, evidence_ref: str, tags: tuple[str, ...] = ()
    ) -> MemoryRecord:
        """Store a candidate that is ineligible for model retrieval."""
        record = MemoryRecord(
            task_id=task_id,
            content=content,
            evidence_ref=evidence_ref,
            tags=tags,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories(id, task_id, content, evidence_ref, tags, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.task_id,
                    record.content,
                    record.evidence_ref,
                    "\n".join(record.tags),
                    record.status.value,
                    record.created_at.isoformat(),
                ),
            )
        return record

    def review(self, record_id: str, *, approve: bool) -> MemoryRecord:
        """Move a candidate exactly once to approved or rejected."""
        record = self.get(record_id)
        if record is None:
            raise MemoryStoreError(f"unknown memory record: {record_id}")
        if record.status != MemoryStatus.CANDIDATE:
            raise MemoryStoreError(f"memory record is already {record.status.value}")
        status = MemoryStatus.APPROVED if approve else MemoryStatus.REJECTED
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE memories SET status = ? WHERE id = ? AND status = ?",
                (status.value, record_id, MemoryStatus.CANDIDATE.value),
            )
            if cursor.rowcount != 1:
                raise MemoryStoreError("memory changed while it was being reviewed")
        return record.model_copy(update={"status": status})

    def search(self, query: str, *, limit: int = 10) -> tuple[MemoryRecord, ...]:
        """Return approved lessons whose content or tags contain a literal query."""
        if not query.strip():
            raise ValueError("memory query must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("memory search limit must be between 1 and 100")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, content, evidence_ref, tags, status, created_at
                FROM memories
                WHERE status = ? AND (content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')
                ORDER BY created_at DESC, id ASC
                LIMIT ?
                """,
                (MemoryStatus.APPROVED.value, pattern, pattern, limit),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def revoke(self, record_id: str, *, deleted: bool = False) -> MemoryRecord:
        """Durably revoke retrieval before attempting external index cleanup.

        Soft deletion preserves the local audit record; it is not a secure erasure API.
        Repeating the transition is safe when vector cleanup needs a retry.
        """
        with self._connect() as connection:
            state = MemoryStatus.DELETED if deleted else MemoryStatus.REJECTED
            cursor = connection.execute(
                "UPDATE memories SET status = ? WHERE id = ? AND status != ?",
                (state.value, record_id, MemoryStatus.DELETED.value),
            )
            connection.execute("DELETE FROM memory_indexes WHERE memory_id = ?", (record_id,))
            if cursor.rowcount == 0 and self.get(record_id) is None:
                raise MemoryStoreError(f"unknown memory record: {record_id}")
        record = self.get(record_id)
        if record is None:
            raise MemoryStoreError(f"unknown memory record: {record_id}")
        return record

    def get(self, record_id: str) -> MemoryRecord | None:
        """Load a record regardless of review state for audit and approval UI."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, task_id, content, evidence_ref, tags, status, created_at
                FROM memories WHERE id = ?
                """,
                (record_id,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def is_indexed(self, record_id: str) -> bool:
        """Return whether an approved record completed semantic indexing."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM memory_indexes WHERE memory_id = ?", (record_id,)
            ).fetchone()
        return row is not None

    def mark_indexed(self, record_id: str) -> None:
        """Persist the completion marker after both indexes accepted the record."""
        record = self.get(record_id)
        if record is None:
            raise MemoryStoreError(f"unknown memory record: {record_id}")
        if record.status != MemoryStatus.APPROVED:
            raise MemoryStoreError("only approved memories can be marked indexed")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_indexes(memory_id, indexed_at)
                VALUES (?, ?)
                """,
                (record_id, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    def _from_row(row: tuple[str, str, str, str, str, str, str]) -> MemoryRecord:
        return MemoryRecord(
            id=row[0],
            task_id=row[1],
            content=row[2],
            evidence_ref=row[3],
            tags=tuple(filter(None, row[4].splitlines())),
            status=MemoryStatus(row[5]),
            created_at=datetime.fromisoformat(row[6]),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_wal(self._path, foreign_keys=True)
