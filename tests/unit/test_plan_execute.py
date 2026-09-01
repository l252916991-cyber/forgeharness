"""Tests for the explicit plan-then-execute runtime."""

from pathlib import Path

from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.plan_execute import PlanExecuteRuntime
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry


def executor(model: ScriptedModel, trace: InMemoryTrace) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentRuntime(
        model=model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=trace,
    )


async def test_plan_execute_accounts_for_planner_and_injects_plan(tmp_path: Path) -> None:
    planner = ScriptedModel(
        [
            ModelResult(
                action=FinalAction(content='{"steps":["inspect","test"]}'),
                usage=ModelUsage(input_tokens=11, output_tokens=5),
            )
        ]
    )
    worker = ScriptedModel([ModelResult(action=FinalAction(content="verified"))])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(planner=planner, executor=executor(worker, trace), trace=trace)

    result = await runtime.run(task_id="task-1", task="fix a defect", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    assert result.usage.steps == 2
    assert result.usage.input_tokens == 11
    assert worker.requests[0].messages[0].role.value == "system"
    assert '"inspect"' in (worker.requests[0].messages[0].content or "")
    assert trace.events[0].type == "plan.created"


async def test_plan_execute_rejects_invalid_plan(tmp_path: Path) -> None:
    planner = ScriptedModel([ModelResult(action=FinalAction(content='{"steps":[]}'))])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("planner returned an invalid plan")
    assert trace.events[-1].type == "plan.failed"


async def test_plan_execute_rejects_planner_tool_call(tmp_path: Path) -> None:
    planner = ScriptedModel([ModelResult(action=ToolAction(call=ToolCall(id="call", name="echo")))])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.error == "planner returned a tool call instead of a structured plan"


async def test_plan_execute_converts_planner_failure(tmp_path: Path) -> None:
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=ScriptedModel([]), executor=executor(ScriptedModel([]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.error == "planner failed: scripted model has no response remaining"
