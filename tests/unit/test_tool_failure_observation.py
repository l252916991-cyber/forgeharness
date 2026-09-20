"""Structured tool-failure observations: classification and context hygiene."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from forgeharness.coding.tools import ReadFileTool
from forgeharness.domain.models import (
    FinalAction,
    ModelResult,
    ModelUsage,
    ToolAction,
    ToolCall,
    TraceEvent,
)
from forgeharness.observability.steps import project_events
from forgeharness.observability.trace import InMemoryTrace
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.tools.base import (
    RiskLevel,
    ToolContext,
    ToolErrorCode,
    ToolFailure,
    ToolOutput,
    ToolSpec,
)
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import PolicyDecision, PolicyDecisionType
from forgeharness.tools.registry import ToolRegistry


class _AllowAll:
    def evaluate(self, *, task_id: str, spec: object, call: object) -> PolicyDecision:
        del task_id, spec, call
        return PolicyDecision(type=PolicyDecisionType.ALLOW, reason="test")


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ExplodingTool:
    """A tool that raises whatever the test asks for."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.input_model = _NoArgs
        self.spec = ToolSpec(
            name="exploding",
            description="Always raises.",
            input_schema=_NoArgs.model_json_schema(),
            risk=RiskLevel.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        raise self._error


def _failure_payload(observation: str) -> dict[str, object]:
    parsed = json.loads(observation)
    assert parsed["ok"] is False
    return parsed["error"]


async def test_missing_path_becomes_a_structured_recoverable_error(tmp_path: Path) -> None:
    """A host exception must not reach the model; a classified failure must."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())

    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="c1", name="read_file", arguments={"path": "./runtime/config.py"}),
        ToolContext(task_id="t", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.error is not None
    assert result.output.error.code is ToolErrorCode.PATH_NOT_FOUND
    assert result.output.error.recoverable is True
    assert result.output.error.details["path"] == "./runtime/config.py"
    # The model gets an actionable instruction, not a traceback.
    observation = result.output.content
    assert "FileNotFoundError" not in observation
    assert "Traceback" not in observation
    assert "No such file" not in observation
    # No host location may leak into the model's context.
    assert str(tmp_path) not in observation and "/Users/" not in observation


async def test_failure_observation_is_deterministic(tmp_path: Path) -> None:
    """Identical failures must render byte-identically.

    Progress detection and replay compare observations, so a varying rendering
    would make the same failure look like new information.
    """
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    call = ToolCall(id="c1", name="read_file", arguments={"path": "missing.py"})
    first = await ToolDispatcher(registry).dispatch(
        call, ToolContext(task_id="t", workspace=tmp_path)
    )
    second = await ToolDispatcher(registry).dispatch(
        call, ToolContext(task_id="t", workspace=tmp_path)
    )
    assert first.output.content == second.output.content


async def test_unknown_exception_falls_back_to_unclassified_and_not_recoverable(
    tmp_path: Path,
) -> None:
    """An unclassified failure must not be promised as retryable, nor leak its text."""
    registry = ToolRegistry()
    registry.register(_ExplodingTool(RuntimeError("something unexpected happened")))

    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="c1", name="exploding"),
        ToolContext(task_id="t", workspace=tmp_path),
    )

    assert result.output.error is not None
    assert result.output.error.code is ToolErrorCode.TOOL_EXECUTION_ERROR
    assert result.output.error.recoverable is False
    assert "something unexpected happened" not in result.output.content
    # The class is still recorded for the harness, just not shown to the model.
    assert result.output.metadata["exception_class"] == "RuntimeError"


async def test_workspace_escape_is_classified_as_invalid_arguments(tmp_path: Path) -> None:
    """A rejected path is an argument problem the model can correct."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())

    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="c1", name="read_file", arguments={"path": "/etc/passwd"}),
        ToolContext(task_id="t", workspace=tmp_path),
    )

    assert result.output.error is not None
    assert result.output.error.code is ToolErrorCode.INVALID_ARGUMENTS
    assert result.output.error.recoverable is True


async def test_timeout_is_classified_and_recoverable(tmp_path: Path) -> None:
    import asyncio

    class _Slow:
        input_model = _NoArgs
        spec = ToolSpec(
            name="slow",
            description="Sleeps.",
            input_schema=_NoArgs.model_json_schema(),
            risk=RiskLevel.READ,
        )

        async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
            del arguments, context
            await asyncio.sleep(0.05)
            return ToolOutput(ok=True, content="late")

    registry = ToolRegistry()
    registry.register(_Slow())
    result = await ToolDispatcher(registry, timeout_seconds=0.001).dispatch(
        ToolCall(id="c1", name="slow"), ToolContext(task_id="t", workspace=tmp_path)
    )
    assert result.output.error is not None
    assert result.output.error.code is ToolErrorCode.TIMEOUT
    assert result.output.error.recoverable is True


def test_trace_and_observation_are_separate_layers() -> None:
    """The model-facing and harness-facing views carry different information."""
    failure = ToolFailure(
        code=ToolErrorCode.PATH_NOT_FOUND,
        message="Requested path does not exist.",
        recoverable=True,
        details={"path": "a.py"},
        suggestion="Inspect available paths.",
    )
    observation = failure.as_observation()
    # Model-facing: the taxonomy, recoverability and what to do next.
    assert '"code":"path_not_found"' in observation
    assert "suggestion" in observation and "recoverable" in observation
    # Harness-facing: the exception class, which must not be in the model's copy.
    assert "FileNotFoundError" not in observation
    assert "Traceback" not in observation


async def test_failed_call_is_still_an_attempt_and_a_logical_call(tmp_path: Path) -> None:
    """A failing tool ran: it must count as an attempt, not be erased."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    trace = InMemoryTrace("t")
    runtime = AgentRuntime(
        model=_Scripted(
            (ToolAction(call=ToolCall(id="c1", name="read_file", arguments={"path": "nope.py"})),)
        ),
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=_AllowAll(),
        trace=trace,
    )
    result = await runtime.run(task_id="t", task="read", workspace=tmp_path)

    assert result.usage.tool_calls == 1
    projection = project_events(trace.events)
    assert projection.tool_attempts == 1
    step = next(s for s in projection.steps if s.action_kind.value == "tool")
    assert step.tool_ok is False
    assert step.error_type == "path_not_found"


class _Scripted:
    def __init__(self, actions: tuple[object, ...]) -> None:
        self._actions = list(actions)

    async def decide(self, request: object) -> ModelResult:
        del request
        action = self._actions.pop(0) if self._actions else FinalAction(content="done")
        return ModelResult(action=action, usage=ModelUsage(input_tokens=5, output_tokens=2))


def test_tool_output_rejects_a_malformed_failure_observation() -> None:
    """The observation is a contract, so malformed JSON would be a harness bug."""
    failure = ToolFailure(
        code=ToolErrorCode.TIMEOUT, message="late", recoverable=True, suggestion="narrow it"
    )
    parsed = json.loads(failure.as_observation())
    assert set(parsed) == {"ok", "error"}
    assert set(parsed["error"]) <= {"code", "recoverable", "message", "details", "suggestion"}


def test_trace_event_carries_the_error_code() -> None:
    """A harness reader must see the classification without parsing the observation."""
    event = TraceEvent(
        task_id="t",
        sequence=1,
        type="tool.completed",
        payload={"call_id": "c1", "tool": "read_file", "ok": False, "error_code": "path_not_found"},
    )
    projection = project_events([event])
    step = projection.steps[0]
    assert step.error_type == "path_not_found"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError("nope"), ToolErrorCode.PATH_NOT_FOUND),
        (NotADirectoryError("not a dir"), ToolErrorCode.PATH_NOT_FOUND),
        (RuntimeError("boom"), ToolErrorCode.TOOL_EXECUTION_ERROR),
        (ValueError("odd"), ToolErrorCode.TOOL_EXECUTION_ERROR),
    ],
)
async def test_exception_taxonomy(
    error: BaseException, expected: ToolErrorCode, tmp_path: Path
) -> None:
    registry = ToolRegistry()
    registry.register(_ExplodingTool(error))
    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="c1", name="exploding"), ToolContext(task_id="t", workspace=tmp_path)
    )
    assert result.output.error is not None
    assert result.output.error.code is expected
