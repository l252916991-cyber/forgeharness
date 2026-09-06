"""Durable knowledge answers and real hash-chained retrieval/model evidence."""

from __future__ import annotations

from pathlib import Path

from forgeharness.knowledge.models import AgentResponse
from forgeharness.observability.hash_chain import HashChainedJSONLTrace
from forgeharness.state.sqlite import connect_wal


class KnowledgeRunStore:
    """Keep application responses separate from CodingAgent checkpoint state."""

    def __init__(self, path: Path, traces: Path) -> None:
        self._path = path
        self._traces = traces
        path.parent.mkdir(parents=True, exist_ok=True)
        with connect_wal(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_runs "
                "(id TEXT PRIMARY KEY, response_json TEXT NOT NULL)"
            )

    def trace(self, run_id: str) -> HashChainedJSONLTrace:
        return HashChainedJSONLTrace(self._traces / f"{run_id}.jsonl", run_id)

    def save(self, response: AgentResponse) -> None:
        with connect_wal(self._path) as connection:
            connection.execute(
                "INSERT INTO knowledge_runs(id, response_json) VALUES (?, ?)",
                (response.run_id, response.model_dump_json()),
            )

    def get(self, run_id: str) -> AgentResponse | None:
        with connect_wal(self._path) as connection:
            row = connection.execute(
                "SELECT response_json FROM knowledge_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return AgentResponse.model_validate_json(row[0]) if row else None
