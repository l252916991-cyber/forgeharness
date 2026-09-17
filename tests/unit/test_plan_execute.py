"""Tests for the explicit plan-then-execute runtime."""

from pathlib import Path

import pytest

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


def _invalid(content: str) -> ModelResult:
    """A planner response that breaks the plan protocol."""
    return ModelResult(
        action=FinalAction(content=content),
        usage=ModelUsage(input_tokens=11, output_tokens=5),
    )


def _valid(steps: list[str] | None = None) -> ModelResult:
    import json as _json

    return ModelResult(
        action=FinalAction(content=_json.dumps({"steps": steps or ["inspect", "test"]})),
        usage=ModelUsage(input_tokens=13, output_tokens=6),
    )


def _verified() -> ModelResult:
    return ModelResult(action=FinalAction(content="verified"))


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
    # The planner request precedes the plan decision so its latency is derivable.
    assert [event.type for event in trace.events][:2] == ["model.request", "plan.created"]
    assert trace.events[0].payload["phase"] == "planner"
    plan_event = trace.events[1]
    assert plan_event.payload["input_tokens"] == 11
    assert plan_event.payload["model_name"] == "unknown"


async def test_plan_execute_rejects_invalid_plan(tmp_path: Path) -> None:
    """An empty plan is still invalid, and a persistent violator still fails."""
    planner = ScriptedModel([_invalid('{"steps":[]}'), _invalid('{"steps":[]}')])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert "repeated_planner_protocol_violation" in result.error


async def test_plan_execute_rejects_planner_tool_call(tmp_path: Path) -> None:
    """A planner tool call is a protocol violation, corrected the same way."""
    planner = ScriptedModel(
        [
            ModelResult(action=ToolAction(call=ToolCall(id="call", name="echo"))),
            ModelResult(action=ToolAction(call=ToolCall(id="call", name="echo"))),
        ]
    )
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert "repeated_planner_protocol_violation" in result.error
    codes = [e.payload["error_code"] for e in trace.events if e.type == "plan.failed"]
    assert codes == ["planner_called_tool", "planner_called_tool"]


async def test_plan_execute_converts_planner_failure(tmp_path: Path) -> None:
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=ScriptedModel([]), executor=executor(ScriptedModel([]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.error == "planner failed: scripted model has no response remaining"
    # A model-boundary failure is not a protocol violation, so no correction is attempted.
    assert not [e for e in trace.events if e.type == "plan.failed" and e.payload.get("recoverable")]


async def test_plan_execute_accepts_a_fenced_json_plan(tmp_path: Path) -> None:
    """A fenced JSON block is a formatting habit, not a planning failure.

    Rejecting it would score the model on markdown rather than on orchestration
    and would bias a runtime comparison, so the wrapper is tolerated while the
    plan itself is still strictly validated.
    """
    planner = ScriptedModel(
        [
            ModelResult(
                action=FinalAction(
                    content='```json\n{"steps": ["list pkg", "read the util module"]}\n```'
                ),
                usage=ModelUsage(input_tokens=11, output_tokens=5),
            )
        ]
    )
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner,
        executor=executor(
            ScriptedModel([ModelResult(action=FinalAction(content="verified"))]), trace
        ),
        trace=trace,
    )

    result = await runtime.run(task_id="task-1", task="inspect pkg", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    plan_events = [event for event in trace.events if event.type == "plan.created"]
    assert len(plan_events) == 1
    assert plan_events[0].payload["steps"] == ["list pkg", "read the util module"]


async def test_plan_execute_accepts_a_bare_fence(tmp_path: Path) -> None:
    planner = ScriptedModel(
        [
            ModelResult(
                action=FinalAction(content='```\n{"steps": ["only step"]}\n```'),
                usage=ModelUsage(input_tokens=3, output_tokens=2),
            )
        ]
    )
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner,
        executor=executor(
            ScriptedModel([ModelResult(action=FinalAction(content="verified"))]), trace
        ),
        trace=trace,
    )
    result = await runtime.run(task_id="task-1", task="inspect", workspace=tmp_path)
    assert result.status == RunStatus.SUCCEEDED


async def test_plan_execute_still_rejects_a_fenced_invalid_plan(tmp_path: Path) -> None:
    """Tolerating the fence must not tolerate an empty or malformed plan."""
    bad = ModelResult(action=FinalAction(content='```json\n{"steps": []}\n```'))
    planner = ScriptedModel([bad, bad])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )
    result = await runtime.run(task_id="task-1", task="inspect", workspace=tmp_path)
    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert "repeated_planner_protocol_violation" in result.error
    codes = [e.payload["error_code"] for e in trace.events if e.type == "plan.failed"]
    assert codes == ["plan_schema_violation", "plan_schema_violation"]


async def test_plan_execute_accepts_a_trailing_backtick(tmp_path: Path) -> None:
    """Observed from a live model: valid JSON followed by one stray backtick.

    A single trailing backtick is formatting noise. Rejecting the plan over it
    would fail Plan-Execute for markdown rather than for orchestration, so the
    JSON object is extracted and then judged on its own merits.
    """
    planner = ScriptedModel(
        [
            ModelResult(
                action=FinalAction(content='{"steps": ["do the thing"]}`'),
                usage=ModelUsage(input_tokens=5, output_tokens=5),
            )
        ]
    )
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner,
        executor=executor(ScriptedModel([ModelResult(action=FinalAction(content="ok"))]), trace),
        trace=trace,
    )

    result = await runtime.run(task_id="task-1", task="inspect", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    plan_events = [event for event in trace.events if event.type == "plan.created"]
    assert len(plan_events) == 1
    assert plan_events[0].payload["steps"] == ["do the thing"]


async def test_plan_execute_accepts_json_padded_with_prose(tmp_path: Path) -> None:
    planner = ScriptedModel(
        [
            ModelResult(
                action=FinalAction(
                    content=(
                        'Here is the plan:\n```json\n{"steps": ["one step"]}\n```\nHope that helps.'
                    )
                )
            )
        ]
    )
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner,
        executor=executor(ScriptedModel([ModelResult(action=FinalAction(content="ok"))]), trace),
        trace=trace,
    )
    result = await runtime.run(task_id="task-1", task="inspect", workspace=tmp_path)
    assert result.status == RunStatus.SUCCEEDED


async def test_plan_execute_rejects_a_response_with_no_json(tmp_path: Path) -> None:
    """Tolerating decoration must not tolerate a missing plan."""
    xml = ModelResult(action=FinalAction(content="<start_marker>Reading app.py...</start_marker>"))
    planner = ScriptedModel([xml, xml])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )
    result = await runtime.run(task_id="task-1", task="inspect", workspace=tmp_path)
    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert "repeated_planner_protocol_violation" in result.error
    codes = [e.payload["error_code"] for e in trace.events if e.type == "plan.failed"]
    assert codes == ["missing_plan_json", "missing_plan_json"]


async def test_plan_execute_rejects_prose_that_is_not_a_plan(tmp_path: Path) -> None:
    """A JSON object of the wrong shape is still rejected."""
    wrong = ModelResult(action=FinalAction(content='{"goal": "inspect", "notes": "no steps"}'))
    planner = ScriptedModel([wrong, wrong])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )
    result = await runtime.run(task_id="task-1", task="inspect", workspace=tmp_path)
    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert "repeated_planner_protocol_violation" in result.error


async def test_plan_execute_rejects_truncated_json(tmp_path: Path) -> None:
    """A response cut off mid-object is a planning failure, not a partial plan."""
    cut = ModelResult(action=FinalAction(content='{"steps": ["Search the workspace for the litera'))
    planner = ScriptedModel([cut, cut])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([]), trace), trace=trace
    )
    result = await runtime.run(task_id="task-1", task="inspect", workspace=tmp_path)
    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert "repeated_planner_protocol_violation" in result.error
    codes = [e.payload["error_code"] for e in trace.events if e.type == "plan.failed"]
    assert codes == ["invalid_plan_json", "invalid_plan_json"]


async def test_planner_recovers_after_one_protocol_violation(tmp_path: Path) -> None:
    """Red → green: an invalid plan earns one correction attempt, then succeeds."""
    planner = ScriptedModel([_invalid("<task>inspect</task>"), _valid(["inspect", "test"])])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([_verified()]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    events = [event.type for event in trace.events]
    # The evidence chain records the rejection, the corrected request, then the plan.
    assert events[:4] == ["model.request", "plan.failed", "model.request", "plan.created"]
    assert trace.events[0].payload["phase"] == "planner"
    assert trace.events[0].payload["correction"] is False
    assert trace.events[1].payload["error_code"] == "missing_plan_json"
    assert trace.events[1].payload["recoverable"] is True
    assert trace.events[1].payload["occurrence"] == 1
    assert trace.events[2].payload["phase"] == "planner"
    assert trace.events[2].payload["correction"] is True


async def test_planner_retry_costs_are_fully_charged(tmp_path: Path) -> None:
    """Correction is not free: the failed attempt's steps and tokens are billed."""
    planner = ScriptedModel([_invalid("no json here"), _valid()])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([_verified()]), trace), trace=trace
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    # Two planner attempts (11+5 and 13+6 tokens) plus one executor step.
    assert result.usage.steps == 3
    assert result.usage.input_tokens == 11 + 13
    assert result.usage.output_tokens == 5 + 6
    # Planning never touches tool accounting.
    assert result.usage.tool_calls == 0


async def test_planner_correction_message_is_actionable_not_a_stack_trace(
    tmp_path: Path,
) -> None:
    """The feedback must be usable, and must not recycle raw exception text.

    Forwarding a decode traceback is the defect this project fixes elsewhere; it
    must not reappear on the planner path.
    """
    planner = ScriptedModel([_invalid('{"steps": ["ok"'), _valid()])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([_verified()]), trace), trace=trace
    )

    await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    correction = planner.requests[1].messages[-1].content or ""
    assert "Harness protocol error" in correction
    assert "steps" in correction
    # No raw parser diagnostics leaked into the model's context.
    assert "JSONDecodeError" not in correction
    assert "Expecting" not in correction
    assert "Traceback" not in correction
    assert "line 1 column" not in correction


async def test_planner_retry_budget_is_bounded(tmp_path: Path) -> None:
    """A planner that never complies must fail without executing any tool."""
    planner = ScriptedModel([_invalid("nope") for _ in range(5)])
    trace = InMemoryTrace("task-1")
    worker = ScriptedModel([_verified()])
    runtime = PlanExecuteRuntime(planner=planner, executor=executor(worker, trace), trace=trace)

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert "repeated_planner_protocol_violation" in result.error
    # Bounded: default allows one correction, so two attempts total.
    assert result.usage.steps == 2
    assert result.usage.tool_calls == 0
    # The executor never ran, so no tool decision was ever requested from it.
    assert worker.requests == []


async def test_planner_retries_can_be_disabled(tmp_path: Path) -> None:
    """A zero retry budget must fail on the first violation, as before the fix."""
    planner = ScriptedModel([_invalid("nope"), _valid()])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner,
        executor=executor(ScriptedModel([]), trace),
        trace=trace,
        max_planner_retries=0,
    )

    result = await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)

    assert result.status == RunStatus.FAILED
    assert result.usage.steps == 1
    assert not [e for e in trace.events if e.type == "plan.created"]


async def test_plan_reports_planner_attempts(tmp_path: Path) -> None:
    """The accepted plan records how many attempts it cost."""
    planner = ScriptedModel([_invalid("x"), _valid()])
    trace = InMemoryTrace("task-1")
    runtime = PlanExecuteRuntime(
        planner=planner, executor=executor(ScriptedModel([_verified()]), trace), trace=trace
    )
    await runtime.run(task_id="task-1", task="fix", workspace=tmp_path)
    created = next(e for e in trace.events if e.type == "plan.created")
    assert created.payload["planner_attempts"] == 2


def test_planner_retry_budget_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        PlanExecuteRuntime(
            planner=ScriptedModel([]),
            executor=executor(ScriptedModel([]), InMemoryTrace("t")),
            trace=InMemoryTrace("t"),
            max_planner_retries=-1,
        )
