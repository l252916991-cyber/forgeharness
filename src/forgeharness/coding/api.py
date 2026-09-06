"""API lifecycle for workspace-bound, approval-gated CodingAgent runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx

from forgeharness.coding.agent import CodingAgent
from forgeharness.domain.models import RunResult, RunStatus
from forgeharness.knowledge.models import AgentResponse, Intent
from forgeharness.knowledge.storage import SQLiteApplicationStore
from forgeharness.models.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleModel,
)
from forgeharness.observability.hash_chain import HashChainedJSONLTrace
from forgeharness.state.approval import InMemoryApprovalLedger
from forgeharness.state.checkpoint import SQLiteCheckpointStore


@dataclass
class _ActiveRun:
    agent: CodingAgent
    workspace: Path
    client: httpx.AsyncClient


class APICodingHandler:
    """Keep only live approval capabilities in memory; checkpoints remain durable."""

    def __init__(
        self,
        *,
        data_dir: Path,
        bindings: SQLiteApplicationStore,
        base_url: str,
        model_name: str,
        api_key: str | None,
        timeout_seconds: float,
    ) -> None:
        self._data_dir = data_dir
        self._bindings = bindings
        self._base_url = base_url
        self._model_name = model_name
        self._api_key = api_key or "omlx-local"
        self._timeout = timeout_seconds
        self._checkpoints = SQLiteCheckpointStore(data_dir / "runs.sqlite3")
        self._active: dict[str, _ActiveRun] = {}

    async def handle(self, *, session_id: str, task: str) -> AgentResponse:
        workspace = self._bindings.get_workspace(session_id)
        if workspace is None:
            from forgeharness.knowledge.conversation import UnavailableCodingHandler

            return await UnavailableCodingHandler().handle(session_id=session_id, task=task)
        task_id = f"coding-{uuid4().hex}"
        client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        provider = OpenAICompatibleModel(
            OpenAICompatibleConfig(
                base_url=self._base_url,
                api_key=self._api_key,
                model=self._model_name,
                timeout_seconds=self._timeout,
                max_tokens=4_096,
                enable_thinking=False,
            ),
            client=client,
        )
        ledger = InMemoryApprovalLedger()
        agent = CodingAgent(
            model=provider,
            trace=HashChainedJSONLTrace(self._data_dir / "traces" / f"{task_id}.jsonl", task_id),
            approval_ledger=ledger,
            checkpoint_store=self._checkpoints,
            test_command=("python", "-m", "pytest", "-q"),
        )
        try:
            result = await agent.start(task_id=task_id, issue=task, workspace=workspace)
        except Exception:
            await client.aclose()
            raise
        if result.status == RunStatus.AWAITING_APPROVAL:
            self._active[task_id] = _ActiveRun(agent=agent, workspace=workspace, client=client)
        else:
            await client.aclose()
        return _agent_response(result, self._model_name)

    async def approve(self, task_id: str, *, granted_by: str) -> RunResult:
        result = self._checkpoints.load(task_id)
        if result is None:
            raise KeyError("run not found")
        if result.pending_approval is None:
            raise ValueError("run has no pending approval")
        active = self._active.get(task_id)
        if active is None:
            raise RuntimeError(
                "approval capability was lost after process restart; restart the coding task"
            )
        grant = active.agent.approve(result, granted_by=granted_by)
        resumed = await active.agent.resume(
            suspended=result,
            grant=grant,
            workspace=active.workspace,
        )
        if resumed.status != RunStatus.AWAITING_APPROVAL:
            await active.client.aclose()
            self._active.pop(task_id, None)
        return resumed

    async def close(self) -> None:
        for active in tuple(self._active.values()):
            await active.client.aclose()
        self._active.clear()


def _agent_response(result: RunResult, model: str) -> AgentResponse:
    if result.status == RunStatus.AWAITING_APPROVAL and result.pending_approval is not None:
        answer = (
            f"代码任务已暂停, 等待审批工具 {result.pending_approval.call.name} 的精确参数。"
            f"run_id={result.task_id}"
        )
    elif result.final_output:
        answer = result.final_output
    elif result.error:
        answer = f"代码任务失败: {result.error}"
    else:
        answer = f"代码任务状态: {result.status.value}"
    return AgentResponse(
        intent=Intent.CODING_TASK,
        answer=answer,
        run_id=result.task_id,
        trace_id=result.task_id,
        latency_ms=0,
        model=model,
    )
