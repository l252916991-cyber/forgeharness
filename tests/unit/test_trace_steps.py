"""Step projection: identity semantics, phase attribution, and determinism."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeharness.coding.tools import ReadFileTool
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import HashChainedJSONLTrace
from forgeharness.observability.steps import (
    StepActionKind,
    StepPhase,
    StepStatus,
    TraceProjectionError,
    project_events,
    project_trace_path,
)
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.plan_execute import PlanExecuteRuntime
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.registry import ToolRegistry


class _AllowAll:
    def evaluate(self, *, task_id: str, spec: object, call: object) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test")


class _DenyAll:
    def evaluate(self, *, task_id: str, spec: object, call: object) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.DENY, reason="test deny")


def _runtime(
    responses: list[ModelResult],
    trace: InMemoryTrace,
    budget: RunBudget | None = None,
    policy: object | None = None,
) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentRuntime(
        model=ScriptedModel(responses),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=policy or _AllowAll(),
        trace=trace,
        budget=budget or RunBudget(),
    )


def _tool(name: str, call_id: str, **arguments: object) -> ModelResult:
    return ModelResult(
        action=ToolAction(call=ToolCall(id=call_id, name=name, arguments=dict(arguments))),
        usage=ModelUsage(input_tokens=10, output_tokens=5),
        model_name="scripted",
    )


def _final(text: str = "done") -> ModelResult:
    return ModelResult(
        action=FinalAction(content=text),
        usage=ModelUsage(input_tokens=7, output_tokens=3),
        model_name="scripted",
    )


async def test_react_run_projects_model_and_tool_steps(tmp_path: Path) -> None:
    """Model → tool → model → finish projects into ordered, linked steps."""
    trace = InMemoryTrace("react-1")
    runtime = _runtime([_tool("echo", "c1", text="hi"), _final()], trace)

    await runtime.run(task_id="react-1", task="echo hi", workspace=tmp_path)
    projection = project_events(trace.events)

    kinds = [step.action_kind for step in projection.steps]
    assert kinds == [
        StepActionKind.MODEL,
        StepActionKind.TOOL,
        StepActionKind.MODEL,
        StepActionKind.FINISH,
    ]
    assert projection.final_status is StepStatus.COMPLETED
    assert projection.tool_sequence == ("echo",)
    assert projection.executed_sequence == ("echo",)
    assert projection.tool_attempts == 1
    assert projection.incomplete_calls == 0
    # The tool step is keyed to the decision that owed it.
    decision, execution = projection.steps[0], projection.steps[1]
    assert decision.phase is StepPhase.EXECUTOR
    assert decision.logical_call_id == "c1"
    assert decision.attempt_id is None
    assert execution.logical_call_id == "c1"
    assert execution.attempt_id == "c1#1"
    assert execution.tool_ok is True
    assert execution.tool_args == {"text": "hi"}
    assert execution.observation == "hi"
    assert execution.tool_latency_ms is not None
    # Latency is explicitly a derived figure, never presented as SDK-reported.
    assert decision.model_latency_ms_derived is not None


async def test_step_ids_are_sequential_and_unique(tmp_path: Path) -> None:
    trace = InMemoryTrace("react-2")
    runtime = _runtime(
        [_tool("echo", "c1", text="a"), _tool("echo", "c2", text="b"), _final()], trace
    )
    await runtime.run(task_id="react-2", task="echo", workspace=tmp_path)
    projection = project_events(trace.events)
    assert [step.step_id for step in projection.steps] == list(range(1, len(projection.steps) + 1))


async def test_plan_execute_projects_explicit_planner_phase(tmp_path: Path) -> None:
    """`plan.created` becomes a planner step, not an anonymous extra step."""
    trace = InMemoryTrace("plan-1")
    executor = _runtime([_tool("echo", "c1", text="a"), _final()], trace)
    runtime = PlanExecuteRuntime(
        planner=ScriptedModel(
            [
                ModelResult(
                    action=FinalAction(content=json.dumps({"steps": ["echo twice"]})),
                    usage=ModelUsage(input_tokens=11, output_tokens=4),
                    model_name="planner",
                )
            ]
        ),
        executor=executor,
        trace=trace,
    )

    await runtime.run(task_id="plan-1", task="echo", workspace=tmp_path)
    projection = project_events(trace.events)

    planner_steps = [step for step in projection.steps if step.phase is StepPhase.PLANNER]
    executor_steps = [step for step in projection.steps if step.phase is StepPhase.EXECUTOR]
    assert len(planner_steps) == 1
    assert projection.plan_events == 1
    # Planner cost is attributable, and its latency is derived from its own
    # request rather than inferred from a step-count difference.
    assert planner_steps[0].model_input_tokens == 11
    assert planner_steps[0].model_latency_ms_derived is not None
    assert executor_steps and executor_steps[0].phase is StepPhase.EXECUTOR
    # Every projected step belongs to exactly one phase.
    assert len(planner_steps) + len(executor_steps) == len(projection.steps)


async def test_budget_refused_decision_does_not_fabricate_tool_step(
    tmp_path: Path,
) -> None:
    """A decision stopped at a budget gate is reported, never faked as executed."""
    trace = InMemoryTrace("budget-1")
    runtime = _runtime([_tool("echo", "c1", text="a")], trace, budget=RunBudget(max_tool_calls=0))

    result = await runtime.run(task_id="budget-1", task="echo", workspace=tmp_path)
    projection = project_events(trace.events)

    assert result.status is RunStatus.EXHAUSTED
    # The decision is visible ...
    assert projection.tool_sequence == ("echo",)
    assert projection.model_decisions == 1
    # ... but it was never executed, and no tool step claims otherwise.
    assert projection.executed_sequence == ()
    assert projection.tool_attempts == 0
    assert projection.incomplete_calls == 1
    tool_steps = projection.steps_for(StepActionKind.TOOL)
    assert len(tool_steps) == 1
    assert tool_steps[0].status is StepStatus.INCOMPLETE
    assert tool_steps[0].tool_ok is None
    assert tool_steps[0].attempt_id == "c1#1"


async def test_denied_policy_call_is_marked_failed_not_executed(tmp_path: Path) -> None:
    trace = InMemoryTrace("deny-1")
    runtime = _runtime([_tool("echo", "c1", text="a"), _final()], trace, policy=_DenyAll())

    await runtime.run(task_id="deny-1", task="echo", workspace=tmp_path)
    projection = project_events(trace.events)

    tool_steps = projection.steps_for(StepActionKind.TOOL)
    assert len(tool_steps) == 1
    assert tool_steps[0].status is StepStatus.FAILED
    assert tool_steps[0].error_type == "policy_denied"
    assert tool_steps[0].tool_ok is False
    assert projection.tool_attempts == 0
    # The decision is still reported: it happened, it just did not execute.
    assert projection.tool_sequence == ("echo",)
    assert projection.executed_sequence == ()


async def test_tool_failure_lands_on_its_logical_call(tmp_path: Path) -> None:
    """A failing tool records ok=false, a reason, and the observation."""
    trace = InMemoryTrace("fail-1")
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    runtime = AgentRuntime(
        model=ScriptedModel(
            [
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(id="c1", name="read_file", arguments={"path": "missing.py"})
                    ),
                    usage=ModelUsage(input_tokens=10, output_tokens=5),
                    model_name="scripted",
                )
            ],
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
    )
    await runtime.run(task_id="fail-1", task="read it", workspace=tmp_path)

    projection = project_events(trace.events)
    tool_steps = projection.steps_for(StepActionKind.TOOL)
    assert len(tool_steps) == 1
    assert tool_steps[0].logical_call_id == "c1"
    assert tool_steps[0].tool_ok is False
    assert tool_steps[0].status is StepStatus.FAILED
    assert tool_steps[0].observation
    assert tool_steps[0].attempt_id == "c1#1"


def test_suspended_call_projects_awaiting_approval() -> None:
    """A call held for approval is pending, not an unaccounted-for gap."""
    trace = InMemoryTrace("approval-1")
    trace.append("run.started", {"workspace": "/tmp"})
    trace.append("model.request", {"phase": "executor", "task_id": "approval-1", "tool_count": 1})
    trace.append(
        "model.action",
        {
            "kind": "tool",
            "action": {
                "kind": "tool",
                "call": {"id": "c1", "name": "write_file", "arguments": {"path": "a"}},
            },
            "input_tokens": 5,
            "output_tokens": 2,
        },
    )
    trace.append(
        "policy.decided",
        {"call_id": "c1", "tool": "write_file", "decision": "require_approval", "reason": "review"},
    )
    trace.append("run.awaiting_approval", {"call_id": "c1", "tool": "write_file"})
    trace.append("run.finished", {"status": "awaiting_approval", "error": None})

    projection = project_events(trace.events)
    assert projection.final_status is StepStatus.AWAITING_APPROVAL
    tool_steps = projection.steps_for(StepActionKind.TOOL)
    assert len(tool_steps) == 1
    assert tool_steps[0].status is StepStatus.AWAITING_APPROVAL
    # Suspended is a known state, not an unresolved one.
    assert projection.incomplete_calls == 0
    assert projection.tool_attempts == 0


def test_allow_decision_does_not_close_the_owed_step() -> None:
    """`policy.decided` fires for every call; only a denial resolves it early."""
    trace = InMemoryTrace("allow-1")
    trace.append("model.request", {"phase": "executor", "task_id": "allow-1", "tool_count": 1})
    trace.append(
        "model.action",
        {
            "kind": "tool",
            "action": {"kind": "tool", "call": {"id": "c1", "name": "echo", "arguments": {}}},
            "input_tokens": 1,
            "output_tokens": 1,
        },
    )
    trace.append(
        "policy.decided",
        {"call_id": "c1", "tool": "echo", "decision": "allow", "reason": "read-only"},
    )
    projection = project_events(trace.events)
    tool_steps = projection.steps_for(StepActionKind.TOOL)
    assert tool_steps[0].status is StepStatus.INCOMPLETE
    # Execution evidence never arrived, so the call stays unresolved.
    assert projection.incomplete_calls == 1


async def test_failed_run_projects_terminal_status(tmp_path: Path) -> None:
    trace = InMemoryTrace("fail-2")
    registry = ToolRegistry()
    registry.register(EchoTool())
    runtime = AgentRuntime(
        model=ScriptedModel([]),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
    )
    await runtime.run(task_id="fail-2", task="echo", workspace=tmp_path)
    projection = project_events(trace.events)
    assert projection.final_status is StepStatus.FAILED
    assert projection.steps[-1].action_kind is StepActionKind.FINISH
    assert projection.steps[-1].status is StepStatus.FAILED


async def test_unclosed_trace_projects_incomplete_not_success(tmp_path: Path) -> None:
    """An opened call with no completion must never read as success."""
    trace = InMemoryTrace("unclosed-1")
    events = list(trace.events)
    events.append(
        trace.append(
            "model.request", {"phase": "executor", "task_id": "unclosed-1", "tool_count": 1}
        )
    )
    events.append(
        trace.append(
            "model.action",
            {
                "kind": "tool",
                "action": {
                    "kind": "tool",
                    "call": {"id": "c1", "name": "echo", "arguments": {"text": "a"}},
                },
                "input_tokens": 10,
                "output_tokens": 5,
            },
        )
    )
    # The process dies here: no tool.completed, no run.finished.
    projection = project_events(events)

    assert projection.incomplete_calls == 1
    assert projection.final_status is StepStatus.INCOMPLETE
    tool_steps = projection.steps_for(StepActionKind.TOOL)
    assert len(tool_steps) == 1
    assert tool_steps[0].status is StepStatus.INCOMPLETE
    assert tool_steps[0].tool_ok is None
    assert projection.tool_attempts == 0
    # Nothing silently upgrades an unresolved call to a completed one.
    assert all(
        step.status is not StepStatus.COMPLETED
        for step in projection.steps
        if step.action_kind is StepActionKind.TOOL
    )


def test_projection_is_deterministic_across_runs() -> None:
    """The same events must always project identically."""
    trace = InMemoryTrace("det-1")
    trace.append("run.started", {"workspace": "/tmp"})
    trace.append("model.request", {"phase": "executor", "task_id": "det-1", "tool_count": 1})
    trace.append(
        "model.action",
        {
            "kind": "tool",
            "action": {"kind": "tool", "call": {"id": "c1", "name": "echo", "arguments": {"t": 1}}},
            "input_tokens": 3,
            "output_tokens": 2,
            "budget": {"steps": 5, "tool_calls": 4, "input_tokens": 100, "output_tokens": 100},
        },
    )
    trace.append(
        "tool.completed",
        {
            "call_id": "c1",
            "tool": "echo",
            "ok": True,
            "elapsed_ms": 4,
            "observation": "x",
            "budget": {"steps": 5, "tool_calls": 3, "input_tokens": 100, "output_tokens": 100},
        },
    )
    trace.append("run.finished", {"status": "succeeded", "error": None})

    first = project_events(trace.events)
    second = project_events(list(trace.events))
    assert first == second
    # Structurally equivalent and byte-equivalent when serialized.
    assert first.model_dump_json() == second.model_dump_json()


def test_budget_snapshot_explains_why_a_run_stopped() -> None:
    """The remainder after each step makes an exhaustion diagnosable."""
    trace = InMemoryTrace("budget-2")
    trace.append("model.request", {"phase": "executor", "task_id": "budget-2", "tool_count": 1})
    trace.append(
        "model.action",
        {
            "kind": "tool",
            "action": {"kind": "tool", "call": {"id": "c1", "name": "echo", "arguments": {}}},
            "input_tokens": 1,
            "output_tokens": 1,
            "budget": {"steps": 0, "tool_calls": 0, "input_tokens": 10, "output_tokens": 10},
        },
    )
    projection = project_events(trace.events)
    snapshot = projection.steps[0].budget_snapshot
    assert snapshot == {"steps": 0, "tool_calls": 0, "input_tokens": 10, "output_tokens": 10}


def test_malformed_tool_action_is_rejected() -> None:
    trace = InMemoryTrace("bad-1")
    trace.append("model.action", {"kind": "tool", "action": {"kind": "tool"}})
    with pytest.raises(TraceProjectionError, match="no call payload"):
        project_events(trace.events)


async def test_trace_path_projection_verifies_chain(tmp_path: Path) -> None:
    """A persisted trace projects, and a tampered one is refused."""
    trace = HashChainedJSONLTrace(tmp_path / "task.jsonl", "path-1")
    trace.append("run.started", {"workspace": str(tmp_path)})
    trace.append("model.request", {"phase": "executor", "task_id": "path-1", "tool_count": 1})
    trace.append(
        "model.action",
        {
            "kind": "tool",
            "action": {"kind": "tool", "call": {"id": "c1", "name": "echo", "arguments": {}}},
            "input_tokens": 1,
            "output_tokens": 1,
        },
    )
    trace.append("tool.completed", {"call_id": "c1", "tool": "echo", "ok": True, "elapsed_ms": 2})
    trace.append("run.finished", {"status": "succeeded", "error": None})
    projection = project_trace_path(tmp_path / "task.jsonl")
    assert projection.tool_sequence == ("echo",)
    assert projection.final_status is StepStatus.COMPLETED

    payload = json.loads((tmp_path / "task.jsonl").read_text(encoding="utf-8").splitlines()[0])
    payload["event"]["payload"] = {"workspace": "tampered"}
    lines = (tmp_path / "task.jsonl").read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps(payload)
    (tmp_path / "task.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(TraceProjectionError, match="cannot project invalid trace"):
        project_trace_path(tmp_path / "task.jsonl")


async def test_projection_agrees_with_evaluator_step_facts(tmp_path: Path) -> None:
    """The evaluator and a diagnostic view share one projection, so they agree."""
    from forgeharness.evaluation.agent import ObservedRun, _observed

    trace = InMemoryTrace("agree-1")
    runtime = _runtime([_tool("echo", "c1", text="a"), _final()], trace)
    result = await runtime.run(task_id="agree-1", task="echo", workspace=tmp_path)

    observed: ObservedRun = _observed(result, trace, latency_ms=1.0)
    projection = project_events(trace.events)
    assert observed.tool_sequence == projection.tool_sequence
    assert observed.tool_arguments == projection.tool_arguments
    assert observed.model_decisions == projection.model_decisions
    assert observed.tool_attempts == projection.tool_attempts
    assert observed.plan_events == projection.plan_events


def test_unknown_event_types_are_ignored() -> None:
    """A newer event type must not break projection of an older reader."""
    trace = InMemoryTrace("unknown-1")
    trace.append("run.started", {})
    trace.append("some.future.event", {"payload": 1})
    trace.append("run.finished", {"status": "succeeded", "error": None})
    projection = project_events(trace.events)
    assert projection.final_status is StepStatus.COMPLETED


def test_checkpoint_revision_is_attached_to_steps() -> None:
    """Each step carries the checkpoint revision current when it happened."""
    trace = InMemoryTrace("rev-1")
    trace.append("checkpoint.saved", {"revision": 3, "status": "running"})
    trace.append("model.request", {"phase": "executor", "task_id": "rev-1", "tool_count": 0})
    trace.append(
        "model.action",
        {"kind": "final", "action": {"kind": "final", "content": "x"}, "input_tokens": 1},
    )
    projection = project_events(trace.events)
    assert projection.steps[0].checkpoint_revision == 3


def test_checkpoint_without_integer_revision_is_ignored() -> None:
    trace = InMemoryTrace("rev-2")
    trace.append("checkpoint.saved", {"revision": "not-an-int"})
    trace.append("model.request", {"phase": "executor", "task_id": "rev-2", "tool_count": 0})
    trace.append(
        "model.action",
        {"kind": "final", "action": {"kind": "final", "content": "x"}},
    )
    projection = project_events(trace.events)
    assert projection.steps[0].checkpoint_revision is None


def test_model_action_without_action_payload_is_rejected() -> None:
    trace = InMemoryTrace("bad-2")
    trace.append("model.action", {"kind": "tool"})
    with pytest.raises(TraceProjectionError, match="no action payload"):
        project_events(trace.events)


def test_completion_without_matching_decision_projects_as_its_own_step() -> None:
    """A resumed call's decision lives in an earlier trace, so link what exists."""
    trace = InMemoryTrace("resume-1")
    trace.append(
        "tool.completed",
        {
            "call_id": "c9",
            "tool": "write_file",
            "ok": True,
            "elapsed_ms": 7,
            "observation": "wrote",
        },
    )
    trace.append("run.finished", {"status": "succeeded", "error": None})
    projection = project_events(trace.events)
    tool_steps = projection.steps_for(StepActionKind.TOOL)
    assert len(tool_steps) == 1
    assert tool_steps[0].logical_call_id == "c9"
    assert tool_steps[0].status is StepStatus.COMPLETED
    assert projection.tool_attempts == 1
    assert projection.model_decisions == 0


def test_rejections_without_a_call_id_are_ignored() -> None:
    """Pre-call-id traces must not crash the projection."""
    trace = InMemoryTrace("legacy-1")
    trace.append("tool.unknown", {"tool": "missing"})
    trace.append("policy.decided", {"tool": "x", "decision": "deny", "reason": "nope"})
    trace.append("run.finished", {"status": "succeeded", "error": None})
    projection = project_events(trace.events)
    # Nothing was owed, so no tool step is invented.
    assert projection.steps_for(StepActionKind.TOOL) == ()


def test_rejection_for_an_unknown_call_id_is_ignored() -> None:
    trace = InMemoryTrace("orphan-1")
    trace.append("tool.unknown", {"call_id": "never-decided", "tool": "missing"})
    trace.append("run.finished", {"status": "succeeded", "error": None})
    projection = project_events(trace.events)
    assert projection.steps_for(StepActionKind.TOOL) == ()


def test_awaiting_approval_without_a_call_id_is_ignored() -> None:
    trace = InMemoryTrace("approval-2")
    trace.append("run.awaiting_approval", {"tool": "write_file"})
    trace.append("run.finished", {"status": "awaiting_approval", "error": None})
    projection = project_events(trace.events)
    assert projection.steps_for(StepActionKind.TOOL) == ()
    assert projection.final_status is StepStatus.AWAITING_APPROVAL


def test_awaiting_approval_for_unknown_call_id_is_ignored() -> None:
    trace = InMemoryTrace("approval-3")
    trace.append("run.awaiting_approval", {"call_id": "ghost", "tool": "write_file"})
    trace.append("run.finished", {"status": "awaiting_approval", "error": None})
    projection = project_events(trace.events)
    assert projection.steps_for(StepActionKind.TOOL) == ()


def test_blank_lines_in_a_trace_file_are_skipped(tmp_path: Path) -> None:
    """A trailing or interior blank line must not abort projection."""
    trace = HashChainedJSONLTrace(tmp_path / "t.jsonl", "blank-1")
    trace.append("run.started", {"workspace": str(tmp_path)})
    trace.append("run.finished", {"status": "succeeded", "error": None})
    text = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    lines = text.splitlines()
    (tmp_path / "t.jsonl").write_text(lines[0] + "\n\n" + lines[1] + "\n", encoding="utf-8")
    projection = project_trace_path(tmp_path / "t.jsonl")
    assert projection.final_status is StepStatus.COMPLETED


def test_numeric_coercion_handles_floats_bools_and_missing_values() -> None:
    """Trace payload numbers arrive as JSON scalars and must coerce safely."""
    from forgeharness.observability.steps import _as_int

    assert _as_int(5) == 5
    assert _as_int(5.9) == 5
    assert _as_int(True) == 0
    assert _as_int(None) == 0
    assert _as_int("12") == 0


def test_float_token_counts_are_coerced() -> None:
    trace = InMemoryTrace("float-1")
    trace.append("model.request", {"phase": "executor", "task_id": "float-1", "tool_count": 0})
    trace.append(
        "model.action",
        {"kind": "final", "action": {"kind": "final", "content": "x"}, "input_tokens": 7.5},
    )
    projection = project_events(trace.events)
    assert projection.steps[0].model_input_tokens == 7
