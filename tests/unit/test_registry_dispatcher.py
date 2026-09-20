"""Tests for schema registration and failure-safe dispatch."""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from forgeharness.domain.models import ToolCall
from forgeharness.tools.base import RiskLevel, ToolContext, ToolErrorCode, ToolOutput, ToolSpec
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.registry import ToolRegistry


class EmptyInput(BaseModel):
    """No-argument test tool input."""


class FailingTool:
    """Test tool that raises a plugin-owned error."""

    @property
    def input_model(self) -> type[BaseModel]:
        return EmptyInput

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="failing",
            description="Always fail.",
            input_schema=EmptyInput.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        raise OSError("plugin exploded")


class SlowTool(FailingTool):
    """Test tool that exceeds the dispatcher deadline."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="slow",
            description="Sleep past a deadline.",
            input_schema=EmptyInput.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        await asyncio.sleep(0.05)
        return ToolOutput(ok=True, content="late")


class InvalidSchemaTool(FailingTool):
    """Test tool whose advertised schema is inconsistent."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="invalid_schema",
            description="Advertise the wrong schema.",
            input_schema={"type": "object"},
            risk=RiskLevel.READ,
        )


class LongOutputTool(FailingTool):
    """Return content larger than the dispatcher output contract."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="long_output",
            description="Return excessive output.",
            input_schema=EmptyInput.model_json_schema(),
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        return ToolOutput(ok=True, content="x" * 500, metadata={"source": "test"})


def test_registry_rejects_duplicate_tool() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    try:
        registry.register(EchoTool())
    except ValueError as exc:
        assert str(exc) == "tool already registered: echo"
    else:
        raise AssertionError("duplicate registration should fail")


def test_registry_rejects_schema_mismatch() -> None:
    with pytest.raises(ValueError, match="tool schema does not match"):
        ToolRegistry().register(InvalidSchemaTool())


def test_dispatcher_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ToolDispatcher(ToolRegistry(), timeout_seconds=0)
    with pytest.raises(ValueError, match="at least 100"):
        ToolDispatcher(ToolRegistry(), max_output_chars=99)


async def test_dispatcher_returns_validation_error_as_observation(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="call-1", name="echo", arguments={"unexpected": True}),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.error is not None
    assert result.output.error.code is ToolErrorCode.INVALID_ARGUMENTS
    assert result.output.error.recoverable is True
    # The validator's prose never reaches the model.
    assert "unexpected" not in result.output.content


async def test_dispatcher_reports_unknown_tool(tmp_path: Path) -> None:
    result = await ToolDispatcher(ToolRegistry()).dispatch(
        ToolCall(id="call-1", name="missing", arguments={}),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.error is not None
    assert result.output.error.code is ToolErrorCode.INVALID_ARGUMENTS
    assert result.output.content.startswith('{"error":')


async def test_dispatcher_converts_plugin_failure_to_observation(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())

    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="call-1", name="failing"),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.error is not None
    # An unclassified failure is not promised to be recoverable.
    assert result.output.error.code is ToolErrorCode.TOOL_EXECUTION_ERROR
    assert result.output.error.recoverable is False
    # The trace keeps the class; the model does not see the raw exception.
    assert result.output.metadata["exception_class"] == "OSError"
    assert "plugin exploded" not in result.output.content


async def test_dispatcher_enforces_timeout(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(SlowTool())

    result = await ToolDispatcher(registry, timeout_seconds=0.001).dispatch(
        ToolCall(id="call-1", name="slow"),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.error is not None
    assert result.output.error.code is ToolErrorCode.TIMEOUT
    assert result.output.error.recoverable is True


async def test_dispatcher_propagates_task_cancellation(tmp_path: Path) -> None:
    entered = asyncio.Event()

    class BlockingTool(FailingTool):
        async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
            del arguments, context
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    registry = ToolRegistry()
    registry.register(BlockingTool())
    task = asyncio.create_task(
        ToolDispatcher(registry).dispatch(
            ToolCall(id="call-1", name="failing"),
            ToolContext(task_id="task-1", workspace=tmp_path),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


async def test_dispatcher_bounds_all_tool_output(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(LongOutputTool())

    result = await ToolDispatcher(registry, max_output_chars=100).dispatch(
        ToolCall(id="call-1", name="long_output"),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert len(result.output.content) == 100
    assert result.output.content.endswith("[tool output truncated]")
    assert result.output.metadata == {"source": "test", "output_truncated": True}
