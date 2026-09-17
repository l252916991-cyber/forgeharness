"""Whole-run deadline (M10-C).

The property that matters is not "the run stops" but *where* it stops: the deadline
is checked between steps, so a tool that has already begun is allowed to finish and
be journaled. Aborting a side effect mid-write would leave exactly the ambiguous
state the invocation journal exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from forgeharness.coding.tools import ReadFileTool
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.invocation_journal import InvocationState, SQLiteInvocationJournal
from forgeharness.tools.base import (
    RiskLevel,
    ToolContext,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
)
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.registry import ToolRegistry


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "x"


class _SlowTool:
    """A side-effecting tool that advances the injected clock while it runs."""

    def __init__(self, clock: _Clock, *, seconds: float = 60.0) -> None:
        self._clock = clock
        self._seconds = seconds
        self._executions = 0
        self.input_model = _Input
        self.spec = ToolSpec(
            name="slow_write",
            description="Slow side-effecting tool.",
            input_schema=_Input.model_json_schema(),
            risk=RiskLevel.WRITE,
            effect_class=ToolEffectClass.NON_IDEMPOTENT,
        )

    @property
    def executions(self) -> int:
        return self._executions

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        self._executions += 1
        # The work takes real time; the clock is injected so tests need not wait.
        self._clock.advance(self._seconds)
        return ToolOutput(ok=True, content="wrote")


class _Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _AllowAll:
    def evaluate(self, *, task_id: str, spec: object, call: object) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test")


class _Scripted:
    def __init__(self, actions: list[object]) -> None:
        self._actions = list(actions)

    async def decide(self, request: object) -> ModelResult:
        del request
        action = self._actions.pop(0) if self._actions else FinalAction(content="done")
        return ModelResult(action=action, usage=ModelUsage(input_tokens=5, output_tokens=2))


async def _run(
    tmp_path: Path,
    *,
    actions: list[object],
    budget: RunBudget,
    tool: object | None = None,
):
    registry = ToolRegistry()
    registry.register(tool if tool is not None else ReadFileTool())
    journal = SQLiteInvocationJournal(tmp_path / "runs.sqlite3")
    clock = _Clock()
    trace = InMemoryTrace("task")
    runtime = AgentRuntime(
        model=_Scripted(actions),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
        budget=budget,
        invocation_journal=journal,
        clock_fn=clock,
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    return result, trace, journal, clock


async def test_no_deadline_preserves_previous_behaviour(tmp_path: Path) -> None:
    """None means unbounded: the default must not change existing runs."""
    result, _, _, _ = await _run(
        tmp_path,
        actions=[
            ToolAction(call=ToolCall(id="c1", name="read_file", arguments={"path": "app.py"})),
            FinalAction(content="done"),
        ],
        budget=RunBudget(max_wall_seconds=None),
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage.tool_calls == 1


async def test_a_run_within_its_deadline_is_unaffected(tmp_path: Path) -> None:
    clock_result, _, _, _ = await _run(
        tmp_path,
        actions=[FinalAction(content="done")],
        budget=RunBudget(max_wall_seconds=600),
    )
    assert clock_result.status is RunStatus.SUCCEEDED
    assert clock_result.error is None


async def test_exceeding_the_deadline_fails_with_a_machine_readable_type(
    tmp_path: Path,
) -> None:
    """A run that passed its ceiling must fail, and say why in a stable field.

    The tool advances the clock past the deadline while it runs, so the next
    loop-top check trips.
    """
    clock = _Clock()
    slow = _SlowTool(clock, seconds=60.0)
    registry = ToolRegistry()
    registry.register(slow)
    journal = SQLiteInvocationJournal(tmp_path / "runs.sqlite3")
    trace = InMemoryTrace("task")
    runtime = AgentRuntime(
        model=_Scripted(
            [
                ToolAction(call=ToolCall(id="c1", name="slow_write", arguments={"value": "x"})),
                FinalAction(content="will not be reached"),
            ]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
        budget=RunBudget(max_wall_seconds=30),
        invocation_journal=journal,
        clock_fn=clock,
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)

    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.startswith("deadline_exceeded")
    finished = next(e for e in trace.events if e.type == "run.finished")
    assert finished.payload["error_type"] == "deadline_exceeded"
    # Never reported as success, even though the model had a final answer queued.
    assert result.final_output is None


async def test_the_deadline_does_not_tear_an_in_flight_tool(tmp_path: Path) -> None:
    """The safety property: a tool that started is allowed to finish and be journaled.

    Cancelling it mid-write would leave an unknown side-effect extent, which is the
    one state recovery cannot resolve. So the deadline stops new work instead.
    """
    clock = _Clock()
    slow = _SlowTool(clock, seconds=60.0)
    registry = ToolRegistry()
    registry.register(slow)
    journal = SQLiteInvocationJournal(tmp_path / "runs.sqlite3")
    trace = InMemoryTrace("task")
    runtime = AgentRuntime(
        model=_Scripted(
            [
                ToolAction(call=ToolCall(id="c1", name="slow_write", arguments={"value": "x"})),
                FinalAction(content="unreached"),
            ]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
        budget=RunBudget(max_wall_seconds=30),
        invocation_journal=journal,
        clock_fn=clock,
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)

    # The tool ran to completion, exactly once, and its effect is unambiguous.
    assert slow.executions == 1
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.COMPLETED
    assert record.result is not None
    # The run still ends at the deadline rather than continuing.
    assert result.status is RunStatus.FAILED
    assert "deadline_exceeded" in (result.error or "")


async def test_a_tool_still_in_flight_is_never_cancelled_before_it_records(
    tmp_path: Path,
) -> None:
    """Ordering: started → execute → settle, all before the deadline is re-checked."""
    clock = _Clock()
    slow = _SlowTool(clock, seconds=1_000.0)
    registry = ToolRegistry()
    registry.register(slow)
    journal = SQLiteInvocationJournal(tmp_path / "runs.sqlite3")
    runtime = AgentRuntime(
        model=_Scripted(
            [ToolAction(call=ToolCall(id="c1", name="slow_write", arguments={"value": "x"}))]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
        budget=RunBudget(max_wall_seconds=1),
        invocation_journal=journal,
        clock_fn=clock,
    )
    await runtime.run(task_id="task", task="do it", workspace=tmp_path)

    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    # Settled, not left at `started`: the deadline did not interrupt the journal.
    assert record.state is InvocationState.COMPLETED
    assert len(journal.attempts_for(record.invocation_id)) == 1


async def test_deadline_applies_even_when_steps_remain(tmp_path: Path) -> None:
    """A wall-clock ceiling is independent of the step budget."""
    clock = _Clock()
    slow = _SlowTool(clock, seconds=500.0)
    registry = ToolRegistry()
    registry.register(slow)
    runtime = AgentRuntime(
        model=_Scripted(
            [
                ToolAction(call=ToolCall(id="c1", name="slow_write", arguments={"value": "x"})),
                FinalAction(content="unreached"),
            ]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
        # Steps would allow 12 decisions; the deadline stops it after one.
        budget=RunBudget(max_steps=12, max_wall_seconds=100),
        clock_fn=clock,
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    assert result.status is RunStatus.FAILED
    assert result.usage.steps == 1
    assert "deadline_exceeded" in (result.error or "")
