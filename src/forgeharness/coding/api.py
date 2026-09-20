"""API lifecycle for workspace-bound, approval-gated CodingAgent runs."""

from __future__ import annotations

import asyncio
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
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.verification import CodingVerifier, Verifier
from forgeharness.state.approval import InMemoryApprovalLedger
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.state.invocation_journal import SQLiteInvocationJournal
from forgeharness.state.task_registry import TaskRegistry

# A soft deadline for service-backed runs, independent of step count. It stops the
# run from starting further steps once the ceiling passes; it does **not** preempt a
# tool already executing, so a run may exceed this by up to one tool timeout. A local
# run is already bounded by `max_steps` times the per-call timeouts, so this only
# trips on pathological cases; its purpose is a duration bound that does not move
# when step or timeout defaults change.
CODING_RUN_DEADLINE_SECONDS = 1_800.0


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
        verifier: Verifier | None = None,
        task_registry: TaskRegistry | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._bindings = bindings
        self._base_url = base_url
        self._model_name = model_name
        self._api_key = api_key or "omlx-local"
        self._timeout = timeout_seconds
        self._verifier = verifier or CodingVerifier()
        self._task_registry = task_registry
        self._workspace_root = workspace_root
        runs_db = data_dir / "runs.sqlite3"
        self._checkpoints = SQLiteCheckpointStore(runs_db)
        # Shares the database with the checkpoints: the journal is the side-effect
        # authority and the checkpoint is the control-flow authority.
        self._journal = SQLiteInvocationJournal(runs_db)
        self._active: dict[str, _ActiveRun] = {}
        self._approval_locks: dict[str, asyncio.Lock] = {}

    async def handle(self, *, session_id: str, task: str) -> AgentResponse:
        workspace = self._bindings.get_workspace(session_id)
        if workspace is None:
            from forgeharness.knowledge.conversation import UnavailableCodingHandler

            return await UnavailableCodingHandler().handle(session_id=session_id, task=task)
        task_id = f"coding-{uuid4().hex}"
        # Record where this task's durable state lives before any work starts, so a
        # later startup scan can find the workspace after a restart. The stored value
        # is relative to the configured root and is re-validated on read.
        if self._task_registry is not None and self._workspace_root is not None:
            try:
                relative = str(workspace.relative_to(self._workspace_root))
            except ValueError:
                relative = None
            if relative is not None:
                self._task_registry.bind(task_id=task_id, workspace=relative)
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
            invocation_journal=self._journal,
            test_command=("python", "-m", "pytest", "-q"),
            verifier=self._verifier,
            budget=RunBudget(max_wall_seconds=CODING_RUN_DEADLINE_SECONDS),
        )
        try:
            result = await agent.start(task_id=task_id, issue=task, workspace=workspace)
        except BaseException:
            await client.aclose()
            raise
        if result.status == RunStatus.AWAITING_APPROVAL:
            self._active[task_id] = _ActiveRun(agent=agent, workspace=workspace, client=client)
        else:
            await client.aclose()
        return _agent_response(result, self._model_name)

    async def approve(
        self, task_id: str, *, granted_by: str, checkpoint_revision: int
    ) -> RunResult:
        lock = self._approval_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            result = self._checkpoints.load(task_id)
            if result is None:
                raise KeyError("run not found")
            if result.checkpoint_revision != checkpoint_revision:
                raise ValueError("approval checkpoint changed; review the current action again")
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
        checkpoint_revision=result.checkpoint_revision,
    )
