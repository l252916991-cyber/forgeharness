"""Tests for schema registration and failure-safe dispatch."""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from forgeharness.domain.models import ToolCall
from forgeharness.tools.base import RiskLevel, ToolContext, ToolOutput, ToolSpec
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


async def test_dispatcher_returns_validation_error_as_observation(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="call-1", name="echo", arguments={"unexpected": True}),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.content.startswith("invalid tool arguments:")


async def test_dispatcher_reports_unknown_tool(tmp_path: Path) -> None:
    result = await ToolDispatcher(ToolRegistry()).dispatch(
        ToolCall(id="call-1", name="missing", arguments={}),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.content == "unknown tool: missing"


async def test_dispatcher_converts_plugin_failure_to_observation(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())

    result = await ToolDispatcher(registry).dispatch(
        ToolCall(id="call-1", name="failing"),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.content == "tool execution failed: OSError: plugin exploded"


async def test_dispatcher_enforces_timeout(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(SlowTool())

    result = await ToolDispatcher(registry, timeout_seconds=0.001).dispatch(
        ToolCall(id="call-1", name="slow"),
        ToolContext(task_id="task-1", workspace=tmp_path),
    )

    assert result.output.ok is False
    assert result.output.content == "tool execution timed out"
