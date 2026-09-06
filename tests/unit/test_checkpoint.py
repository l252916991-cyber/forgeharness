"""Tests for revisioned SQLite run checkpoints."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from forgeharness.domain.models import Message, MessageRole, RunResult, RunStatus, Usage
from forgeharness.state.checkpoint import CheckpointConflict, SQLiteCheckpointStore


def result(task_id: str = "task-1") -> RunResult:
    return RunResult(
        task_id=task_id,
        status=RunStatus.RUNNING,
        messages=(Message(role=MessageRole.USER, content="task"),),
        usage=Usage(),
    )


def test_store_round_trips_and_increments_revisions(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state" / "runs.sqlite3")

    first = store.save(result())
    second = store.save(first.model_copy(update={"status": RunStatus.SUCCEEDED}))

    assert first.checkpoint_revision == 1
    assert second.checkpoint_revision == 2
    assert store.load("task-1") == second
    assert store.load("missing") is None


def test_store_rejects_stale_and_duplicate_writers(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    original = result()
    saved = store.save(original)

    with pytest.raises(CheckpointConflict, match="expected 0, found 1"):
        store.save(original)
    store.save(saved)
    with pytest.raises(CheckpointConflict, match="expected 1, found 2"):
        store.save(saved)


def test_store_allows_concurrent_process_style_initialization(tmp_path: Path) -> None:
    path = tmp_path / "shared" / "runs.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = tuple(pool.map(lambda _: SQLiteCheckpointStore(path), range(16)))
    assert all(store.load("missing") is None for store in stores)
