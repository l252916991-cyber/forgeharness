"""Exact, expiring, one-shot approval grants."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import Field

from forgeharness.domain.models import FrozenModel, RunContext, RunResult, RunStatus, ToolCall
from forgeharness.state.checkpoint import CheckpointStore, SQLiteCheckpointStore
from forgeharness.state.sqlite import connect_wal


class ApprovalError(RuntimeError):
    """An approval grant is invalid for the requested action."""


class ApprovalGrant(FrozenModel):
    """Authorization bound to one task and canonical tool call."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    call_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    granted_by: str = Field(min_length=1, max_length=200)
    expires_at: datetime
    binding_digest: str = ""
    approver_context: RunContext | None = None


class ApprovalLedger(Protocol):
    """Issue and atomically consume approval grants."""

    def issue(
        self,
        *,
        task_id: str,
        call: ToolCall,
        granted_by: str,
        ttl: timedelta = timedelta(minutes=10),
    ) -> ApprovalGrant:
        """Create a grant for one exact normalized action."""
        ...

    def consume(self, grant: ApprovalGrant, *, task_id: str, call: ToolCall) -> None:
        """Verify and mark a grant consumed in one operation."""
        ...


def call_fingerprint(task_id: str, call: ToolCall) -> str:
    """Hash a task-bound canonical representation of a tool call."""
    canonical = json.dumps(
        {"task_id": task_id, "call_id": call.id, "name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class InMemoryApprovalLedger:
    """One-process approval ledger for tests and local non-durable runs."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._issued: dict[str, ApprovalGrant] = {}
        self._consumed: set[str] = set()
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(
        self,
        *,
        task_id: str,
        call: ToolCall,
        granted_by: str,
        ttl: timedelta = timedelta(minutes=10),
    ) -> ApprovalGrant:
        """Create and retain a task-bound grant with a positive lifetime."""
        if ttl <= timedelta(0):
            raise ValueError("approval ttl must be positive")
        grant = ApprovalGrant(
            task_id=task_id,
            call_fingerprint=call_fingerprint(task_id, call),
            granted_by=granted_by,
            expires_at=self._clock() + ttl,
        )
        self._issued[grant.id] = grant
        return grant

    def consume(self, grant: ApprovalGrant, *, task_id: str, call: ToolCall) -> None:
        """Reject forged, expired, mismatched, or previously consumed grants."""
        issued = self._issued.get(grant.id)
        if issued is None or issued != grant:
            raise ApprovalError("approval grant was not issued by this ledger")
        if grant.id in self._consumed:
            raise ApprovalError("approval grant has already been consumed")
        if self._clock() >= grant.expires_at:
            raise ApprovalError("approval grant has expired")
        expected = call_fingerprint(task_id, call)
        if grant.task_id != task_id or not hmac.compare_digest(grant.call_fingerprint, expected):
            raise ApprovalError("approval grant does not match this tool call")
        self._consumed.add(grant.id)


@runtime_checkable
class AtomicApprovalLedger(Protocol):
    """Optional transactional approval/checkpoint capability."""

    def consume_and_checkpoint(
        self,
        grant: ApprovalGrant,
        *,
        task_id: str,
        call: ToolCall,
        result: RunResult,
        checkpoint_store: CheckpointStore | None,
    ) -> RunResult: ...


class SQLiteApprovalLedger:
    """Durable one-shot grants; atomic admission when checkpoints share this DB."""

    def __init__(
        self, path: Path, *, binding: str = "", clock: Callable[[], datetime] | None = None
    ) -> None:
        self._path = path.resolve()
        self._binding = binding
        self._clock = clock or (lambda: datetime.now(UTC))
        path.parent.mkdir(parents=True, exist_ok=True)
        with connect_wal(self._path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS approval_grants "
                "(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                "payload TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"
            )

    def issue(
        self,
        *,
        task_id: str,
        call: ToolCall,
        granted_by: str,
        ttl: timedelta = timedelta(minutes=10),
        approver_context: RunContext | None = None,
    ) -> ApprovalGrant:
        """Bind an exact call and trusted execution context."""
        if ttl <= timedelta(0):
            raise ValueError("approval ttl must be positive")
        grant = ApprovalGrant(
            task_id=task_id,
            call_fingerprint=call_fingerprint(task_id, call),
            granted_by=granted_by,
            expires_at=self._clock() + ttl,
            binding_digest=self._binding,
            approver_context=approver_context,
        )
        with connect_wal(self._path) as connection:
            connection.execute(
                "INSERT INTO approval_grants(id, task_id, payload) VALUES(?,?,?)",
                (grant.id, task_id, grant.model_dump_json()),
            )
        return grant

    def _consume(
        self, connection: sqlite3.Connection, grant: ApprovalGrant, task_id: str, call: ToolCall
    ) -> None:
        row = connection.execute(
            "SELECT payload, consumed FROM approval_grants WHERE id=?", (grant.id,)
        ).fetchone()
        if row is None or ApprovalGrant.model_validate_json(row[0]) != grant:
            raise ApprovalError("approval grant was not issued by this ledger")
        if row[1]:
            raise ApprovalError("approval grant has already been consumed")
        if self._clock() >= grant.expires_at:
            raise ApprovalError("approval grant has expired")
        if (
            grant.task_id != task_id
            or not hmac.compare_digest(grant.call_fingerprint, call_fingerprint(task_id, call))
            or not hmac.compare_digest(grant.binding_digest, self._binding)
        ):
            raise ApprovalError("approval grant does not match call or trusted binding")
        connection.execute("UPDATE approval_grants SET consumed=1 WHERE id=?", (grant.id,))

    def consume(self, grant: ApprovalGrant, *, task_id: str, call: ToolCall) -> None:
        """Consume a grant without admission; use atomic method for durable execution."""
        with connect_wal(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._consume(connection, grant, task_id, call)

    def consume_in_transaction(
        self, connection: sqlite3.Connection, grant: ApprovalGrant, *, task_id: str, call: ToolCall
    ) -> None:
        """Compose the same exact grant validation with R2 atomic node admission."""
        database = connection.execute("PRAGMA database_list").fetchone()[2]
        if Path(database).resolve() != self._path:
            raise ApprovalError("approval transaction must use the ledger database")
        self._consume(connection, grant, task_id, call)

    def consume_and_checkpoint(
        self,
        grant: ApprovalGrant,
        *,
        task_id: str,
        call: ToolCall,
        result: RunResult,
        checkpoint_store: CheckpointStore | None,
    ) -> RunResult:
        """Consume and admit one call atomically, or roll back both changes."""
        if not isinstance(checkpoint_store, SQLiteCheckpointStore) or (
            checkpoint_store.path.resolve() != self._path
        ):
            raise ApprovalError("atomic approval requires checkpoints in the same SQLite DB")
        with connect_wal(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM run_checkpoints WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise ApprovalError("approval requires an existing suspended checkpoint")
            current = RunResult.model_validate_json(row[0])
            if (
                result.task_id != task_id
                or result.status is not RunStatus.RUNNING
                or result.authorized_call_id != call.id
                or result.pending_approval is not None
                or current.status is not RunStatus.AWAITING_APPROVAL
                or current.pending_approval is None
                or current.pending_approval.call != call
                or current.run_context != result.run_context
                or current.messages != result.messages
                or current.usage != result.usage
                or current.tool_evidence != result.tool_evidence
            ):
                raise ApprovalError("checkpoint is not an exact approval admission")
            self._consume(connection, grant, task_id, call)
            return checkpoint_store.save_in_transaction(connection, result)
