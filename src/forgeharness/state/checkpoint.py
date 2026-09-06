"""Optimistic, revisioned run checkpoints."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from forgeharness.domain.models import RunResult
from forgeharness.state.sqlite import connect_wal


class CheckpointConflict(RuntimeError):
    """A writer attempted to replace a checkpoint revision it did not read."""


class CheckpointStore(Protocol):
    """Persist the latest revision of a run snapshot."""

    def save(self, result: RunResult) -> RunResult:
        """Persist `result` if its revision is current and return the next revision."""
        ...

    def load(self, task_id: str) -> RunResult | None:
        """Load the latest snapshot for a task."""
        ...


class SQLiteCheckpointStore:
    """Store immutable JSON snapshots with optimistic revision checks."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    task_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    payload TEXT NOT NULL
                )
                """
            )

    def save(self, result: RunResult) -> RunResult:
        """Insert or compare-and-swap a task snapshot."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM run_checkpoints WHERE task_id = ?", (result.task_id,)
            ).fetchone()
            current = 0 if row is None else int(row[0])
            if current != result.checkpoint_revision:
                raise CheckpointConflict(
                    f"checkpoint revision conflict for {result.task_id}: "
                    f"expected {result.checkpoint_revision}, found {current}"
                )
            saved = result.model_copy(update={"checkpoint_revision": current + 1})
            if row is None:
                connection.execute(
                    "INSERT INTO run_checkpoints(task_id, revision, payload) VALUES (?, ?, ?)",
                    (saved.task_id, saved.checkpoint_revision, saved.model_dump_json()),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE run_checkpoints
                    SET revision = ?, payload = ?
                    WHERE task_id = ? AND revision = ?
                    """,
                    (
                        saved.checkpoint_revision,
                        saved.model_dump_json(),
                        saved.task_id,
                        current,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CheckpointConflict(
                        f"checkpoint changed while saving task {result.task_id}"
                    )
            return saved

    def load(self, task_id: str) -> RunResult | None:
        """Return the current snapshot or `None` when a task is unknown."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM run_checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row is None else RunResult.model_validate_json(row[0])

    def _connect(self) -> sqlite3.Connection:
        return connect_wal(self._path, foreign_keys=True)
