"""Tests for scoped delegation and parent usage accounting."""

from pathlib import Path

import pytest
from pydantic import BaseModel

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
from forgeharness.runtime.subagent import SubAgentSpec, SubAgentTool
from forgeharness.tools.base import RiskLevel, ToolContext, ToolOutput, ToolSpec
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry


class EmptyInput(BaseModel):
    """No-argument test tool input."""


class WriteTool:
    """A write-risk tool that must not enter a read-only child."""

    input_model = EmptyInput
    spec = ToolSpec(
        name="writer",
        description="Write something.",
        input_schema=EmptyInput.model_json_schema(),
        risk=RiskLevel.WRITE,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        del arguments, context
        return ToolOutput(ok=True, content="wrote")


async def test_subagent_has_reduced_tools_and_charges_parent_usage(tmp_path: Path) -> None:
    child_model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(id="child-tool", name="echo", arguments={"text": "reviewed"})
                ),
                usage=ModelUsage(input_tokens=7, output_tokens=2),
            ),
            ModelResult(
                action=FinalAction(content="review passed"),
                usage=ModelUsage(input_tokens=5, output_tokens=2),
            ),
        ]
    )
    delegate = SubAgentTool(
        spec=SubAgentSpec(
            name="reviewer",
            description="Review a proposed change.",
            instructions="Inspect evidence and return a concise verdict.",
        ),
        model=child_model,
        tools=(EchoTool(),),
        policy=RiskBasedPolicy(),
    )
    parent_model = ScriptedModel(
        [
            ModelResult(
                action=ToolAction(
                    call=ToolCall(
                        id="delegate", name="delegate_reviewer", arguments={"task": "review"}
                    )
                ),
                usage=ModelUsage(input_tokens=3, output_tokens=1),
            ),
            ModelResult(
                action=FinalAction(content="parent accepted review"),
                usage=ModelUsage(input_tokens=4, output_tokens=1),
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(delegate)
    trace = InMemoryTrace("parent")
    runtime = AgentRuntime(
        model=parent_model,
        registry=registry,
        dispatcher=ToolDispatcher(registry),
        policy=RiskBasedPolicy(),
        trace=trace,
    )

    result = await runtime.run(task_id="parent", task="delegate review", workspace=tmp_path)

    assert result.status == RunStatus.SUCCEEDED
    assert result.usage.steps == 4
    assert result.usage.tool_calls == 2
    assert result.usage.input_tokens == 19
    assert result.usage.output_tokens == 6
    assert parent_model.requests[1].messages[-1].content == "review passed"
    delegate_event = next(
        event
        for event in trace.events
        if event.type == "tool.completed" and event.payload["tool"] == "delegate_reviewer"
    )
    assert delegate_event.payload["metadata"]["child_task_id"] == "parent.reviewer"
    assert [spec.name for spec in child_model.requests[0].tools] == ["echo"]


def test_subagent_rejects_tools_outside_declared_risk_scope() -> None:
    with pytest.raises(ValueError, match="outside its risk scope"):
        SubAgentTool(
            spec=SubAgentSpec(
                name="reviewer",
                description="Read-only review.",
                instructions="Review only.",
            ),
            model=ScriptedModel([]),
            tools=(WriteTool(),),
            policy=RiskBasedPolicy(),
        )
