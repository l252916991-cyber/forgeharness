"""A distinct planning phase followed by the controlled tool loop."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, ValidationError

from forgeharness.domain.models import (
    FinalAction,
    FrozenModel,
    Message,
    MessageRole,
    RunResult,
    RunStatus,
    Usage,
)
from forgeharness.models.base import Model, ModelRequest
from forgeharness.observability.trace import TraceRecorder
from forgeharness.runtime.loop import AgentRuntime


class ExecutionPlan(FrozenModel):
    """Structured steps generated before tool execution begins."""

    steps: tuple[str, ...] = Field(min_length=1, max_length=20)


class PlanExecuteRuntime:
    """Use one bounded model decision for planning, then run an executor loop."""

    def __init__(self, *, planner: Model, executor: AgentRuntime, trace: TraceRecorder) -> None:
        self._planner = planner
        self._executor = executor
        self._trace = trace

    async def run(self, *, task_id: str, task: str, workspace: Path) -> RunResult:
        """Create a validated JSON plan and execute it under the normal Harness controls."""
        planning_messages = (
            Message(
                role=MessageRole.SYSTEM,
                content=(
                    'Return only JSON matching {"steps":["step"]}. '
                    "Plan concrete verification steps; do not call tools."
                ),
            ),
            Message(role=MessageRole.USER, content=task),
        )
        try:
            response = await self._planner.decide(
                ModelRequest(task_id=task_id, messages=planning_messages, tools=())
            )
        except Exception as exc:
            return self._planning_failure(task_id, planning_messages, f"planner failed: {exc}")
        usage = Usage(
            steps=1,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        if not isinstance(response.action, FinalAction):
            return self._planning_failure(
                task_id,
                planning_messages,
                "planner returned a tool call instead of a structured plan",
                usage,
            )
        try:
            plan = ExecutionPlan.model_validate_json(response.action.content)
        except ValidationError as exc:
            return self._planning_failure(
                task_id, planning_messages, f"planner returned an invalid plan: {exc}", usage
            )
        self._trace.append("plan.created", {"steps": list(plan.steps)})
        instruction = Message(
            role=MessageRole.SYSTEM,
            content=(
                "Follow this Harness-validated plan. Re-evaluate after every observation and "
                f"finish only with verification evidence. Plan: {plan.model_dump_json()}"
            ),
        )
        return await self._executor.run(
            task_id=task_id,
            task=task,
            workspace=workspace,
            instructions=(instruction,),
            initial_usage=usage,
        )

    def _planning_failure(
        self,
        task_id: str,
        messages: tuple[Message, ...],
        error: str,
        usage: Usage | None = None,
    ) -> RunResult:
        self._trace.append("plan.failed", {"error": error})
        return RunResult(
            task_id=task_id,
            status=RunStatus.FAILED,
            messages=messages,
            usage=usage or Usage(),
            error=error,
        )
