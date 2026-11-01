"""R2 node facts, atomic checkpoint projection and transactional event outbox."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from forgeharness.domain.models import FrozenModel, RunResult, RunStatus, Usage
from forgeharness.state.approval import ApprovalGrant, SQLiteApprovalLedger
from forgeharness.state.checkpoint import CheckpointConflict, SQLiteCheckpointStore
from forgeharness.state.sqlite import connect_wal

NodeStatus = Literal[
    "pending", "awaiting_approval", "admitted", "unknown", "completed", "failed", "cancelled"
]
R2_RECOVERY_VERSION = 5  # v4 is the old serial R2 protocol; retain its original wheel.
TERMINAL = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.EXHAUSTED, RunStatus.CANCELLED}
)


class NodeProgress(FrozenModel):
    id: str
    status: NodeStatus = "pending"
    input_digest: str | None = None
    output: dict[str, Any] | None = None
    used: Usage = Usage()
    reservation: Usage = Usage()
    grant: ApprovalGrant | None = None
    error: str | None = None
    evidence_refs: tuple[str, ...] = ()
    invocation_id: str | None = None
    dispatch_phase: Literal[
        "not_started", "queued", "reserved", "sending", "unknown", "settled"
    ] = "not_started"
    prepared_payload_sha256: str | None = None
    sent_at: datetime | None = None
    queued_at: datetime | None = None
    capacity_granted_at: datetime | None = None
    completed_at: datetime | None = None


class OrchestrationState(FrozenModel):
    """Version rejection is intentional: do not infer missing historical facts."""

    schema_version: Literal[3] = 3
    definition_version: Literal[1] = 1
    identity: str
    owner: str
    revision: int = Field(default=0, ge=0)
    started_at: datetime
    observed_at: datetime
    deadline: datetime | None = None
    cancel_requested: bool = False
    nodes: tuple[NodeProgress, ...]
    result: RunResult

    @model_validator(mode="after")
    def current_projection(self) -> OrchestrationState:
        if self.result.state_version != 2 or (
            self.result.recovery_semantics_version != R2_RECOVERY_VERSION
        ):
            raise ValueError("R2 requires checkpoint v2 and recovery v5 (orchestration schema 3)")
        return self


def add_usage(*values: Usage) -> Usage:
    return Usage(
        **{key: sum(getattr(value, key) for value in values) for key in Usage.model_fields}
    )


class OrchestrationStore:
    """One SQLite transaction for graph, approval admission, checkpoint and outbox.

    Owner acquisition requires the Harness's OS lock; a token is fencing against
    stale in-process state, not permission to steal a live worker's lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.checkpoints = SQLiteCheckpointStore(path)
        with connect_wal(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS orchestration_runs "
                "(task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, revision INTEGER NOT NULL, "
                "cancel_requested INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS orchestration_outbox "
                "(task_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, "
                "published INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task_id, revision))"
            )

    def load(self, task_id: str) -> OrchestrationState | None:
        with connect_wal(self.path) as connection:
            row = connection.execute(
                "SELECT payload, cancel_requested, owner FROM orchestration_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return OrchestrationState.model_validate_json(row[0]).model_copy(
            update={"cancel_requested": bool(row[1]), "owner": str(row[2])}
        )

    def take_ownership(self, state: OrchestrationState, owner: str) -> OrchestrationState:
        """Called only after acquiring the non-expiring OS operation lock."""
        with connect_wal(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE orchestration_runs SET owner=? WHERE task_id=? AND revision=? AND owner=?",
                (owner, state.result.task_id, state.revision, state.owner),
            )
            if cursor.rowcount != 1:
                raise CheckpointConflict("R2 owner or revision changed")
        return state.model_copy(update={"owner": owner})

    def save(
        self,
        state: OrchestrationState,
        *,
        event: str,
        ledger: SQLiteApprovalLedger | None = None,
        grant: ApprovalGrant | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> OrchestrationState:
        if connection is not None:
            return self._save(connection, state, event=event, ledger=ledger, grant=grant)
        with connect_wal(self.path) as transaction:
            transaction.execute("BEGIN IMMEDIATE")
            return self._save(transaction, state, event=event, ledger=ledger, grant=grant)

    def _save(
        self,
        connection: sqlite3.Connection,
        state: OrchestrationState,
        *,
        event: str,
        ledger: SQLiteApprovalLedger | None,
        grant: ApprovalGrant | None,
    ) -> OrchestrationState:
        key = state.result.task_id
        row = connection.execute(
            "SELECT owner, revision, payload, cancel_requested FROM orchestration_runs "
            "WHERE task_id=?",
            (key,),
        ).fetchone()
        if row is None:
            if state.revision != 0 or self.checkpoints.load(key) is not None:
                raise CheckpointConflict("R2 create conflicts with existing state")
        else:
            if (row[0], row[1]) != (state.owner, state.revision):
                raise CheckpointConflict("R2 stale owner or revision")
            previous = OrchestrationState.model_validate_json(row[2])
            if previous.result.status in TERMINAL:
                raise CheckpointConflict("R2 terminal state is immutable")
            if previous.identity != state.identity:
                raise CheckpointConflict("R2 identity is immutable")
            state = state.model_copy(update={"cancel_requested": bool(row[3])})
        if grant is not None:
            if ledger is None:
                raise ValueError("approval admission requires the shared ledger")
            admitted = [n for n in state.nodes if n.grant == grant and n.status == "admitted"]
            if len(admitted) != 1 or row is None:
                raise ValueError("approval must admit exactly one suspended node")
            old = next(n for n in previous.nodes if n.id == admitted[0].id)
            if old.status != "awaiting_approval" or old.input_digest != admitted[0].input_digest:
                raise CheckpointConflict("approval no longer matches pending action")
            from forgeharness.domain.models import ToolCall

            call = ToolCall.model_validate(admitted[0].output)
            ledger.consume_in_transaction(connection, grant, task_id=key, call=call)
        result = state.result
        if state.cancel_requested and result.status in TERMINAL:
            result = result.model_copy(
                update={"status": RunStatus.CANCELLED, "final_output": None, "error": "cancelled"}
            )
        result = self.checkpoints.save_in_transaction(connection, result)
        saved = state.model_copy(update={"revision": state.revision + 1, "result": result})
        connection.execute(
            "INSERT INTO orchestration_runs(task_id, owner, revision, cancel_requested, payload) "
            "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
            "revision=excluded.revision, payload=excluded.payload",
            (
                key,
                saved.owner,
                saved.revision,
                int(saved.cancel_requested),
                saved.model_dump_json(),
            ),
        )
        import json

        connection.execute(
            "INSERT INTO orchestration_outbox(task_id, revision, payload) VALUES(?,?,?)",
            (key, saved.revision, json.dumps({"event": event, "status": result.status.value})),
        )
        return saved

    def request_cancel(self, task_id: str) -> bool:
        with connect_wal(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM orchestration_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise CheckpointConflict("R2 run does not exist")
            if OrchestrationState.model_validate_json(row[0]).result.status in TERMINAL:
                return False
            connection.execute(
                "UPDATE orchestration_runs SET cancel_requested=1 WHERE task_id=?", (task_id,)
            )
        return True

    def events(self, task_id: str) -> tuple[tuple[int, str], ...]:
        with connect_wal(self.path) as connection:
            rows = connection.execute(
                "SELECT revision, payload FROM orchestration_outbox "
                "WHERE task_id=? AND published=0 ORDER BY revision",
                (task_id,),
            ).fetchall()
        return tuple((int(row[0]), str(row[1])) for row in rows)

    def acknowledge(self, task_id: str, revision: int) -> None:
        with connect_wal(self.path) as connection:
            connection.execute(
                "UPDATE orchestration_outbox SET published=1 WHERE task_id=? AND revision=?",
                (task_id, revision),
            )
