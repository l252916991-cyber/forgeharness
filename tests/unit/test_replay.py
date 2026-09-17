"""Deterministic replay: reproduction, recovery, and bounded violations."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeharness.coding.tools import SearchCodeTool
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
)
from forgeharness.evaluation.bad_case import BadCase, ReplayStep
from forgeharness.evaluation.replay import (
    RecordedModel,
    ReplayExhausted,
    _AllowAllPolicy,
    registry_from_tools,
    replay_bad_case,
)
from forgeharness.models.base import ModelProtocolError, ModelRequest
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.registry import ToolRegistry


def _tool_step(query: str) -> ReplayStep:
    return ReplayStep.from_result(
        ModelResult(
            action=ToolAction(
                call=ToolCall(id=f"call-{query}", name="search_code", arguments={"query": query})
            ),
            usage=ModelUsage(input_tokens=100, output_tokens=10),
        )
    )


def _final_step(text: str = "done") -> ReplayStep:
    return ReplayStep.from_result(
        ModelResult(
            action=FinalAction(content=text), usage=ModelUsage(input_tokens=50, output_tokens=5)
        )
    )


def _violation(*, recoverable: bool | None = None) -> ReplayStep:
    """The recorded protocol violation, optionally with its recoverability stated."""
    return ReplayStep.from_error(
        "ModelProtocolError",
        "parallel tool calls are not enabled for this runtime",
        code="multiple_tool_calls_not_allowed",
        received_tool_calls=2,
        recoverable=recoverable,
        usage=ModelUsage(input_tokens=80, output_tokens=12),
    )


def _case(script: tuple[ReplayStep, ...]) -> BadCase:
    return BadCase(
        case_id="c",
        arm="react",
        revision="a" * 40,
        profile="equal",
        model="fake",
        sample_id=0,
        trace_path="traces/c.jsonl",
        trace_hash="0" * 64,
        expected_status="succeeded",
        actual_status="failed",
        steps=0,
        logical_tool_calls=0,
        tool_attempts=0,
        failure_class="protocol_violation",
        failure_point="model_adapter",
        diagnosis="test",
        replay_mode="recorded",
        executor_script=script,
    )


async def _replay(case: BadCase, tmp_path: Path) -> tuple[RunResult, object]:
    result, projection = await replay_bad_case(
        case,
        workspace=tmp_path,
        task="search twice",
        build_registry=registry_from_tools((SearchCodeTool(),)),
    )
    return result, projection


async def _run_with_budget(
    script: tuple[ReplayStep, ...], tmp_path: Path, budget: RunBudget
) -> tuple[RunResult, InMemoryTrace]:
    trace = InMemoryTrace("c")
    registry = ToolRegistry()
    registry.register(SearchCodeTool())
    runtime = AgentRuntime(
        model=RecordedModel(script),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAllPolicy(),
        trace=trace,
        budget=budget,
    )
    result = await runtime.run(task_id="c", task="t", workspace=tmp_path)
    return result, trace


async def test_non_recoverable_error_still_terminates_the_run(tmp_path: Path) -> None:
    """An error the runtime cannot act on must keep failing terminally."""
    script = (ReplayStep.from_error("RuntimeError", "transport exploded"), _final_step())
    result, _ = await _replay(_case(script), tmp_path)
    assert result.status is RunStatus.FAILED
    assert result.usage.steps == 0
    assert "transport exploded" in (result.error or "")


async def test_recoverable_violation_is_fed_back_without_executing_anything(
    tmp_path: Path,
) -> None:
    """Recovery must not admit or dispatch the rejected calls.

    The harness gives the model another chance; it never chooses an action on the
    model's behalf.
    """
    script = (_violation(recoverable=True), _final_step())
    result, projection = await _replay(_case(script), tmp_path)

    assert result.status is RunStatus.SUCCEEDED
    # The rejected inference is charged, so cost accounting stays honest.
    assert result.usage.steps == 2
    assert result.usage.input_tokens == 80 + 50
    assert result.usage.output_tokens == 12 + 5
    # Nothing was admitted or executed for the rejected response.
    assert result.usage.tool_calls == 0
    assert projection.tool_attempts == 0
    assert projection.tool_sequence == ()


async def test_recovery_allows_the_model_to_correct_itself(tmp_path: Path) -> None:
    script = (_violation(recoverable=True), _tool_step("a"), _tool_step("b"), _final_step())
    result, projection = await _replay(_case(script), tmp_path)

    assert result.status is RunStatus.SUCCEEDED
    assert projection.tool_sequence == ("search_code", "search_code")
    assert result.usage.tool_calls == 2
    assert projection.tool_attempts == 2
    # One step for the rejected inference plus three decisions.
    assert result.usage.steps == 4


async def test_a_record_may_still_end_clearly_when_the_script_is_exhausted(
    tmp_path: Path,
) -> None:
    """If the recorded model never answers again, the replay ends clearly."""
    result, _ = await _replay(_case((_violation(recoverable=True),)), tmp_path)
    assert result.status is RunStatus.FAILED
    assert "ReplayExhausted" in (result.error or "")
    # The violation was still charged, so the stop is attributable.
    assert result.usage.steps == 1


async def test_repeated_violations_fail_safely_within_budget(tmp_path: Path) -> None:
    """A model that never complies must terminate rather than loop forever."""
    budget = RunBudget(max_protocol_violations=2)
    script = tuple(_violation(recoverable=True) for _ in range(6))
    result, _ = await _run_with_budget(script, tmp_path, budget)

    assert result.status is RunStatus.FAILED
    assert "repeated_protocol_violation" in (result.error or "")
    # Bounded by the budget, not by the script length.
    assert result.usage.steps == budget.max_protocol_violations + 1
    assert result.usage.tool_calls == 0


async def test_zero_violation_budget_fails_on_the_first_violation(tmp_path: Path) -> None:
    result, _ = await _run_with_budget(
        (_violation(recoverable=True),), tmp_path, RunBudget(max_protocol_violations=0)
    )
    assert result.status is RunStatus.FAILED
    assert "repeated_protocol_violation" in (result.error or "")


async def test_violation_appears_in_the_trace(tmp_path: Path) -> None:
    """Recovery must be auditable, not silent."""
    result, trace = await _run_with_budget(
        (_violation(recoverable=True), _final_step()), tmp_path, RunBudget()
    )
    assert result.status is RunStatus.SUCCEEDED
    types = [event.type for event in trace.events]
    assert "model.failed" in types
    assert "protocol.violation" in types
    violation = next(e for e in trace.events if e.type == "protocol.violation")
    assert violation.payload["error_code"] == "multiple_tool_calls_not_allowed"
    assert violation.payload["occurrence"] == 1
    # The tokens of the rejected attempt are recorded, not lost.
    failed = next(e for e in trace.events if e.type == "model.failed")
    assert failed.payload["usage"] == {"input_tokens": 80, "output_tokens": 12}


async def test_recorded_violation_is_rejudged_by_the_current_runtime(tmp_path: Path) -> None:
    """Recoverability is a runtime property, which is what lets replay prove a fix.

    A record captured before the fix carries no recoverability flag. Replay must
    re-judge it under today's rules rather than replaying a stale verdict —
    otherwise the same recorded failure could never turn green.
    """
    script = (_violation(recoverable=None), _final_step())
    result, projection = await _replay(_case(script), tmp_path)
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage.steps == 2
    assert projection.tool_attempts == 0


async def test_recorded_model_raises_a_real_protocol_error() -> None:
    """The double must raise the typed error so the runtime branches as it did."""
    model = RecordedModel((_violation(recoverable=True),))
    with pytest.raises(ModelProtocolError) as excinfo:
        await model.decide(ModelRequest(task_id="c", messages=(), tools=()))
    assert excinfo.value.code == "multiple_tool_calls_not_allowed"
    assert excinfo.value.received_tool_calls == 2
    assert excinfo.value.recoverable is True
    assert excinfo.value.usage is not None


async def test_recorded_model_reports_script_exhaustion() -> None:
    model = RecordedModel(())
    with pytest.raises(ReplayExhausted):
        await model.decide(ModelRequest(task_id="c", messages=(), tools=()))


async def test_recorded_model_consumes_its_script_in_order() -> None:
    model = RecordedModel((_tool_step("a"), _tool_step("b")))
    request = ModelRequest(task_id="c", messages=(), tools=())
    first = await model.decide(request)
    second = await model.decide(request)
    assert first.action.kind == "tool"
    assert second.action.kind == "tool"
    assert first.action.call.arguments == {"query": "a"}
    assert second.action.call.arguments == {"query": "b"}
    assert model.remaining == 0
    assert len(model.requests) == 2


def test_recoverability_follows_the_code_and_allows_override() -> None:
    """Recoverability follows a shared code set, so widening it is deliberate."""
    assert ModelProtocolError("x", code="multiple_tool_calls_not_allowed").recoverable is True
    assert ModelProtocolError("x", code="invalid_tool_call").recoverable is False
    assert ModelProtocolError("x").recoverable is False
    assert ModelProtocolError("x", code="invalid_tool_call", recoverable=True).recoverable is True
    assert (
        ModelProtocolError(
            "x", code="multiple_tool_calls_not_allowed", recoverable=False
        ).recoverable
        is False
    )
