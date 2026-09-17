"""Retry in the runtime: attempt accounting, key stability, and safety (M10-B).

Each test targets a boundary rather than a happy path. The invariants under test:

* one logical call may produce several attempts, but only one result;
* every attempt of a call carries the **same** idempotency key;
* a non-idempotent tool that may have already acted is never retried;
* an agent-recoverable failure is not automatically retried by the runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    Usage,
)
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.invocation_journal import (
    AttemptOutcome,
    InvocationState,
    SQLiteInvocationJournal,
)
from forgeharness.tools.base import (
    RiskLevel,
    Tool,
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


class _FlakyTool:
    """Fails with a chosen transient error until its failing attempts are spent."""

    def __init__(
        self,
        *,
        effect_class: ToolEffectClass,
        fail_times: int = 1,
        timeout: bool = True,
        keys: list[str] | None = None,
        name: str = "flaky",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._effect_class = effect_class
        self._remaining_failures = fail_times
        self._timeout = timeout
        self._keys = keys
        self._calls = 0
        self.input_model = _Input
        self.spec = ToolSpec(
            name=name,
            description="Flaky test tool.",
            input_schema=_Input.model_json_schema(),
            risk=RiskLevel.WRITE,
            effect_class=effect_class,
            timeout_seconds=timeout_seconds,
        )

    @property
    def calls(self) -> int:
        """How many times the tool body was entered."""
        return self._calls

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments
        self._calls += 1
        if self._keys is not None:
            self._keys.append(context.idempotency_key or "")
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            if self._timeout:
                raise TimeoutError("transient timeout")
            raise RuntimeError("transient failure")
        return ToolOutput(ok=True, content="ok")


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
    tool: Tool,
    tmp_path: Path,
    *,
    budget: RunBudget | None = None,
    actions: list[object] | None = None,
) -> tuple[Any, InMemoryTrace, SQLiteInvocationJournal]:
    db = tmp_path / "runs.sqlite3"
    journal = SQLiteInvocationJournal(db)
    registry = ToolRegistry()
    registry.register(tool)
    trace = InMemoryTrace("task")

    async def never_sleep(seconds: float) -> None:
        del seconds

    runtime = AgentRuntime(
        model=_Scripted(
            actions
            or [
                ToolAction(call=ToolCall(id="c1", name=tool.spec.name, arguments={"value": "x"})),
                FinalAction(content="done"),
            ]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
        budget=budget,
        invocation_journal=journal,
        sleep_fn=never_sleep,
    )
    result = await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    return result, trace, journal


async def test_read_only_transient_failure_is_retried_to_success(tmp_path: Path) -> None:
    """Attempt accounting: one logical call, two attempts, one result."""
    tool = _FlakyTool(effect_class=ToolEffectClass.READ_ONLY, fail_times=1)
    result, _trace, journal = await _run(tool, tmp_path)

    assert result.status is RunStatus.SUCCEEDED
    # The frozen metric contract: one decision, two physical attempts.
    assert result.usage.tool_calls == 1
    assert tool.calls == 2
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.COMPLETED
    assert record.attempt_count == 2
    attempts = journal.attempts_for(record.invocation_id)
    assert [a.attempt_no for a in attempts] == [1, 2]
    assert attempts[0].outcome is AttemptOutcome.TIMED_OUT
    assert attempts[1].outcome is AttemptOutcome.COMPLETED
    assert attempts[1].backoff_before_ms == 100


async def test_every_attempt_uses_the_same_idempotency_key(tmp_path: Path) -> None:
    """Key stability is what makes 'an idempotent tool may be replayed' true.

    A key regenerated per attempt would make the claim false, so this is asserted
    directly rather than inferred from a successful retry.
    """
    keys: list[str] = []
    tool = _FlakyTool(effect_class=ToolEffectClass.IDEMPOTENT, fail_times=2, keys=keys)
    result, _, journal = await _run(tool, tmp_path)

    assert result.status is RunStatus.SUCCEEDED
    assert tool.calls == 3
    assert len(set(keys)) == 1
    assert keys[0] == "task:c1"
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.attempt_count == 3


async def test_non_idempotent_timeout_is_never_retried(tmp_path: Path) -> None:
    """The headline safety invariant of M10-B.

    The tool body ran, so the write may have landed; a timeout leaves that unknown.
    The runtime must not try again however much budget remains, and the invocation
    must be `indeterminate` rather than a clean failure.
    """
    tool = _FlakyTool(effect_class=ToolEffectClass.NON_IDEMPOTENT, fail_times=1)
    result, trace, journal = await _run(tool, tmp_path, budget=RunBudget(max_attempts_per_call=5))

    assert tool.calls == 1
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.INDETERMINATE
    assert record.attempt_count == 1
    decisions = [e for e in trace.events if e.type == "tool.retry.decided"]
    assert len(decisions) == 1
    assert decisions[0].payload["decision"] == "indeterminate"
    # The run still reaches a terminal state rather than hanging.
    assert result.status is RunStatus.SUCCEEDED


async def test_retry_budget_is_a_hard_ceiling(tmp_path: Path) -> None:
    """Exhausting the budget executes exactly `max_attempts` times, never more."""
    tool = _FlakyTool(effect_class=ToolEffectClass.READ_ONLY, fail_times=99)
    budget = RunBudget(max_attempts_per_call=3)
    _, _, journal = await _run(tool, tmp_path, budget=budget)

    assert tool.calls == 3
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.FAILED
    assert record.attempt_count == 3
    attempts = journal.attempts_for(record.invocation_id)
    # Deterministic exponential backoff, capped: 100 then 200.
    assert [a.backoff_before_ms for a in attempts] == [0, 100, 200]


async def test_agent_recoverable_error_is_not_runtime_retried(tmp_path: Path) -> None:
    """`path_not_found` is recoverable by the model, not retryable by the runtime.

    Re-running the identical call would fail identically, so the runtime returns the
    structured observation once and lets the model choose differently. This is the
    test that keeps the two meanings apart.
    """
    from forgeharness.coding.tools import ReadFileTool

    registry = ToolRegistry()
    registry.register(ReadFileTool())
    db = tmp_path / "runs.sqlite3"
    journal = SQLiteInvocationJournal(db)
    trace = InMemoryTrace("task")
    runtime = AgentRuntime(
        model=_Scripted(
            [
                ToolAction(call=ToolCall(id="c1", name="read_file", arguments={"path": "nope.py"})),
                FinalAction(content="gave up"),
            ]
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
        invocation_journal=journal,
    )
    await runtime.run(task_id="task", task="read it", workspace=tmp_path)

    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    # One attempt only: no automatic re-run of the same bad path.
    assert record.attempt_count == 1
    assert record.state is InvocationState.FAILED
    decisions = [e for e in trace.events if e.type == "tool.retry.decided"]
    assert decisions[0].payload["error_code"] == "path_not_found"
    assert decisions[0].payload["decision"] == "fail"
    # The agent still receives the structured observation it can act on.
    tool_messages = [e for e in trace.events if e.type == "tool.completed"]
    assert tool_messages and '"code":"path_not_found"' in tool_messages[0].payload["observation"]


async def test_a_retried_call_reports_one_logical_call_and_several_attempts(
    tmp_path: Path,
) -> None:
    """The frozen metric contract, verified on a real retry."""
    tool = _FlakyTool(effect_class=ToolEffectClass.READ_ONLY, fail_times=2)
    result, _, journal = await _run(tool, tmp_path)

    assert result.usage.tool_calls == 1  # logical
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert len(journal.attempts_for(record.invocation_id)) == 3  # physical


async def test_tool_spec_timeout_is_used_per_call(tmp_path: Path) -> None:
    """A tool's own ceiling applies, so a slow tool owns its number."""
    tool = _FlakyTool(effect_class=ToolEffectClass.READ_ONLY, fail_times=1, timeout_seconds=12.5)
    _, trace, _ = await _run(tool, tmp_path)
    started = next(e for e in trace.events if e.type == "tool.attempt.started")
    assert started.payload["timeout_seconds"] == 12.5


async def test_retry_events_explain_why_a_tool_ran_twice(tmp_path: Path) -> None:
    """The trace must answer 'why did this run twice?' without reading code."""
    tool = _FlakyTool(effect_class=ToolEffectClass.READ_ONLY, fail_times=1)
    _, trace, _ = await _run(tool, tmp_path)

    types = [e.type for e in trace.events]
    for expected in (
        "tool.attempt.started",
        "tool.attempt.failed",
        "tool.retry.decided",
        "tool.retry.backoff",
        "tool.attempt.completed",
        "tool.invocation.completed",
    ):
        assert expected in types, expected
    decided = next(e for e in trace.events if e.type == "tool.retry.decided")
    assert decided.payload["decision"] == "retry"
    assert decided.payload["attempt_no"] == 1
    assert decided.payload["effect_class"] == "read_only"
    backoff = next(e for e in trace.events if e.type == "tool.retry.backoff")
    assert backoff.payload["delay_ms"] == 100


async def test_attempts_are_recorded_before_the_next_one_starts(tmp_path: Path) -> None:
    """Attempt rows are unique per (invocation, attempt_no), so no attempt is lost."""
    tool = _FlakyTool(effect_class=ToolEffectClass.READ_ONLY, fail_times=2)
    _, _, journal = await _run(tool, tmp_path)
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    attempts = journal.attempts_for(record.invocation_id)
    assert sorted(a.attempt_no for a in attempts) == [1, 2, 3]
    assert all(a.outcome is not None for a in attempts)
    assert all(a.finished_at is not None for a in attempts)


# --------------------------------------------------------------------------------------
# Composition: a retried call that succeeds, then a crash, then a restart.
# --------------------------------------------------------------------------------------


async def test_restart_reuses_the_successful_retry_without_a_third_attempt(
    tmp_path: Path,
) -> None:
    """Ties M10-A and M10-B together.

    attempt 1 fails transiently, attempt 2 succeeds, then the process dies before the
    checkpoint records the result. Recovery must reuse the journaled result — and in
    particular must not start attempt 3, which is what re-dispatching would do.
    """

    from forgeharness.domain.models import CURRENT_RECOVERY_SEMANTICS_VERSION, Message, MessageRole
    from forgeharness.state.checkpoint import SQLiteCheckpointStore

    runs_db = tmp_path / "runs.sqlite3"
    journal = SQLiteInvocationJournal(runs_db)
    checkpoints = SQLiteCheckpointStore(runs_db)
    call = ToolCall(id="c1", name="flaky", arguments={"value": "x"})

    # --- First process: fail once, succeed, then die before the checkpoint. ---
    tool = _FlakyTool(effect_class=ToolEffectClass.READ_ONLY, fail_times=1)

    async def never_sleep(seconds: float) -> None:
        del seconds

    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(
        model=_Scripted([ToolAction(call=call), FinalAction(content="done")]),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=InMemoryTrace("task"),
        invocation_journal=journal,
        sleep_fn=never_sleep,
    )
    await runtime.run(task_id="task", task="do it", workspace=tmp_path)
    assert tool.calls == 2
    record = journal.load(run_id="task", logical_call_id="c1")
    assert record is not None
    assert record.state is InvocationState.COMPLETED
    assert record.attempt_count == 2

    # The checkpoint is the world before the tool result was written.
    interrupted = checkpoints.save(
        RunResult(
            task_id="task",
            status=RunStatus.RUNNING,
            messages=(
                Message(role=MessageRole.USER, content="do it"),
                Message(role=MessageRole.ASSISTANT, content=None, tool_calls=(call,)),
            ),
            usage=Usage(steps=1),
            checkpoint_revision=0,
            recovery_semantics_version=CURRENT_RECOVERY_SEMANTICS_VERSION,
        )
    )

    # --- Second process: recover. The tool must not run again. ---
    resumed = await runtime.resume_running(previous=interrupted, workspace=tmp_path)

    assert resumed.status is RunStatus.SUCCEEDED
    # Still 2: recovery reused the journaled result instead of dispatching attempt 3.
    assert tool.calls == 2
    assert len(journal.attempts_for(record.invocation_id)) == 2
    tool_messages = [m for m in resumed.messages if m.role.value == "tool"]
    assert any(m.content == "ok" for m in tool_messages)
