"""Global task registry: where a task's recovery sources live.

Answers exactly one question:

    given a task id, which workspace holds this task's checkpoint and journal?

The checkpoints and the invocation journal are workspace-local, so after a service
restart there is no way to find a leftover run's durable state without knowing its
workspace. Without this mapping a `task_id` is a dead end.

It deliberately does **not** track run status. Status has one authority, the
checkpoint; a second copy here would immediately be able to disagree with it. The
registry is a locator and never an authority, including for authorization: the
stored path is re-validated against the configured workspace root on every read,
so a corrupted registry cannot point recovery at an arbitrary directory.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import Field

from forgeharness.domain.models import FrozenModel
from forgeharness.state.sqlite import connect_wal

# The default global registry location. Kept outside any workspace because its
# whole purpose is to find workspaces.
DEFAULT_REGISTRY_PATH = Path.home() / ".forgeharness" / "task-registry.sqlite"


class TaskBinding(FrozenModel):
    """Where one task's durable state lives."""

    task_id: str = Field(min_length=1)
    # Relative to the configured workspace root, not absolute: the root is
    # configuration, and storing an absolute path would freeze one machine's layout
    # into durable state. Resolved and re-validated on every read.
    workspace: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime


class TaskRegistry(Protocol):
    """Durable task-to-workspace index."""

    def bind(self, *, task_id: str, workspace: str) -> TaskBinding:
        """Record where a task's state lives, inserting or updating one row."""
        ...

    def get(self, task_id: str) -> TaskBinding | None:
        """Return a task's binding, or None when it was never bound."""
        ...

    def all_bindings(self) -> tuple[TaskBinding, ...]:
        """Return every binding, ordered by task id."""
        ...


class SQLiteTaskRegistry:
    """SQLite-backed locator, written next to no workspace in particular."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_bindings (
                    task_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def bind(self, *, task_id: str, workspace: str) -> TaskBinding:
        """Insert or update a binding, preserving the original creation time."""
        now = datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM task_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
            created_at = datetime.fromisoformat(str(row[0])) if row else now
            connection.execute(
                """
                INSERT INTO task_bindings (task_id, workspace, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    workspace = excluded.workspace,
                    updated_at = excluded.updated_at
                """,
                (task_id, workspace, created_at.isoformat(), now.isoformat()),
            )
            connection.commit()
        return TaskBinding(
            task_id=task_id, workspace=workspace, created_at=created_at, updated_at=now
        )

    def get(self, task_id: str) -> TaskBinding | None:
        """Return one binding, or None when the task was never bound."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task_id, workspace, created_at, updated_at FROM task_bindings "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return None if row is None else _row_to_binding(row)

    def all_bindings(self) -> tuple[TaskBinding, ...]:
        """Return every binding. The registry holds one row per task by design."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT task_id, workspace, created_at, updated_at FROM task_bindings "
                "ORDER BY task_id"
            ).fetchall()
        return tuple(_row_to_binding(row) for row in rows)

    def _connect(self) -> sqlite3.Connection:
        connection = connect_wal(self._path, foreign_keys=True)
        connection.row_factory = sqlite3.Row
        return connection


def _row_to_binding(row: sqlite3.Row) -> TaskBinding:
    """Rebuild a binding from a row, by column name."""
    return TaskBinding(
        task_id=str(row["task_id"]),
        workspace=str(row["workspace"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )
