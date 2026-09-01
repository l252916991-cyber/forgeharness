"""Tests for reviewed memory and tamper-evident redacted traces."""

import json
from pathlib import Path

import pytest

from forgeharness.observability.hash_chain import HashChainedJSONLTrace, verify_trace
from forgeharness.state.memory import MemoryStatus, MemoryStoreError, SQLiteMemoryStore


def test_memory_requires_review_before_retrieval(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    candidate = store.propose(
        task_id="task-1",
        content="Run the focused parser test after editing grammar rules.",
        evidence_ref="trace://task-1/17",
        tags=("parser", "tests"),
    )

    assert store.search("parser") == ()
    approved = store.review(candidate.id, approve=True)

    assert approved.status == MemoryStatus.APPROVED
    assert store.search("parser") == (approved,)
    assert store.get(candidate.id) == approved
    with pytest.raises(MemoryStoreError, match="already approved"):
        store.review(candidate.id, approve=False)


def test_memory_rejects_invalid_queries_and_unknown_review(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    with pytest.raises(ValueError, match="must not be empty"):
        store.search(" ")
    with pytest.raises(ValueError, match="between 1 and 100"):
        store.search("x", limit=0)
    with pytest.raises(MemoryStoreError, match="unknown memory"):
        store.review("missing", approve=True)


def test_trace_redacts_secrets_and_verifies_hash_chain(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    trace = HashChainedJSONLTrace(path, "task-1")
    event = trace.append(
        "model.request",
        {
            "api_key": "sk-do-not-store-this",
            "nested": {"Authorization": "Bearer credential", "safe": "kept"},
            "message": "token sk-abcdefgh12345678 appeared",
            "input_tokens": 42,
        },
    )
    trace.append("run.finished", {"status": "succeeded"})

    assert event.payload["api_key"] == "[REDACTED]"
    assert event.payload["nested"]["Authorization"] == "[REDACTED]"
    assert event.payload["nested"]["safe"] == "kept"
    assert event.payload["input_tokens"] == 42
    trace_text = path.read_text()
    assert "sk-do-not-store-this" not in trace_text
    assert "sk-abcdefgh12345678" not in trace_text
    assert verify_trace(path).valid is True


def test_trace_detects_tampering_and_task_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    trace = HashChainedJSONLTrace(path, "task-1")
    trace.append("run.started", {})
    original = json.loads(path.read_text().splitlines()[0])
    original["event"]["payload"] = {"tampered": True}
    path.write_text(json.dumps(original) + "\n")

    verification = verify_trace(path)
    assert verification.valid is False
    assert verification.error == "event hash mismatch"
    with pytest.raises(ValueError, match="cannot append to invalid trace"):
        HashChainedJSONLTrace(path, "task-1")
